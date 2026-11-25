"""Pure functions that extract canonical tag values from a MusicBrainz recording
payload. No network, no DB — easy to test against hand-crafted JSON fixtures.

The "payload" passed around here is what `RealMbClient.get_recording` returns:
MusicBrainz's JSON recording lookup (`inc=artists`) with a "releases" key
holding every Release that carries the Recording, browsed page by page with
`inc=labels+media+release-groups` (date, country, status, release group with
its primary and secondary types, medium formats, label names and Catalog
numbers).
"""

from __future__ import annotations

_LATE_SENTINEL = (9999, 99, 99)

_PROPER_TYPES = frozenset({"Single", "EP", "Album"})
_SIDELINED_SECONDARY_TYPES = frozenset({"Compilation", "DJ-mix"})


def parse_partial_date(
    date_str: str | None,
) -> tuple[int | None, int | None, int | None]:
    """Parse 'YYYY[-MM[-DD]]' into a (year, month, day) tuple of optional ints."""
    if not date_str:
        return (None, None, None)
    parts = date_str.split("-")
    try:
        year = int(parts[0]) if parts[0] else None
        month = int(parts[1]) if len(parts) > 1 and parts[1] else None
        day = int(parts[2]) if len(parts) > 2 and parts[2] else None
    except ValueError:
        return (None, None, None)
    return (year, month, day)


def _date_sort_key(date_str: str | None) -> tuple[int, int, int]:
    y, m, d = parse_partial_date(date_str)
    if y is None:
        return _LATE_SENTINEL
    # A vaguer date must not win by accident: "2009" sorts after "2009-12-31".
    return (y, m or 99, d or 99)


def _release_sort_key(release: dict) -> tuple[tuple[int, int, int], bool, str]:
    """Earliest date, then a Release with a Catalog number, then ID, so the same
    input always gives the same pick."""
    return (
        _date_sort_key(release.get("date")),
        not _has_catalog_number(release),
        release.get("id") or "",
    )


def artist_credit_join(credit: list) -> str:
    """Concat credited artist names verbatim, preserving MB's joinphrases."""
    parts: list[str] = []
    for item in credit:
        if isinstance(item, str):
            parts.append(item)
            continue
        name = item.get("name") or item.get("artist", {}).get("name", "")
        parts.append(name)
        join = item.get("joinphrase", "")
        if join:
            parts.append(join)
    return "".join(parts)


def _label_info(release: dict) -> list[dict]:
    return release.get("label-info") or []


def _catalog_number(label_info: dict) -> str | None:
    catno = label_info.get("catalog-number")
    # MusicBrainz's own placeholder for "this Release has no Catalog number"
    if not catno or catno == "[none]":
        return None
    return catno


def _has_catalog_number(release: dict) -> bool:
    return any(_catalog_number(li) for li in _label_info(release))


def _releases(payload: dict) -> list[dict]:
    return payload.get("releases") or []


def _is_single_ep_or_album(release: dict) -> bool:
    group = release.get("release-group") or {}
    secondary = set(group.get("secondary-types") or [])
    return (
        group.get("primary-type") in _PROPER_TYPES
        and not secondary & _SIDELINED_SECONDARY_TYPES
    )


def earliest_release(payload: dict) -> dict | None:
    """The Earliest release, as CONTEXT.md defines it; None when no Release
    carrying the Recording is official."""
    official = [r for r in _releases(payload) if r.get("status") == "Official"]
    pool = [r for r in official if _is_single_ep_or_album(r)] or official
    if not pool:
        return None
    return min(pool, key=_release_sort_key)


def _label_name(label_info: dict) -> str | None:
    return (label_info.get("label") or {}).get("name") or None


def _label(release: dict) -> str | None:
    """The first label entry that has both a name and a Catalog number, so the
    label matches the number printed with it; otherwise the first name."""
    named = [li for li in _label_info(release) if _label_name(li)]
    for li in named:
        if _catalog_number(li):
            return _label_name(li)
    return _label_name(named[0]) if named else None


def payload_to_tags(
    payload: dict,
    release: dict | None,
    *,
    earliest_mbid: str | None = None,
) -> dict[str, str | None]:
    """Map the recording+release pair to our taggable-field dict.

    Only returns fields we can confidently derive from MB. Track number, genre,
    key, and bpm are never proposed here — the file's existing values win.
    Without a release only the Recording's own fields are proposed.
    """
    release = release or {}
    y, m, d = parse_partial_date(release.get("date"))
    artist = artist_credit_join(payload.get("artist-credit") or [])
    return {
        "title": payload.get("title"),
        "artist": artist or None,
        "album": release.get("title"),
        "release_year": str(y) if y is not None else None,
        "release_month": str(m) if m is not None else None,
        "release_day": str(d) if d is not None else None,
        "label": _label(release),
        "recording_mbid": payload.get("id"),
        "release_mbid": release.get("id"),
        "earliest_mbid": earliest_mbid,
    }
