"""Pure functions that extract canonical tag values from a MusicBrainz recording
payload. No network, no DB — easy to test against hand-crafted JSON fixtures.

The "payload" passed around here is the `recording` dict returned by
`musicbrainzngs.get_recording_by_id(id, includes=["artists", "releases",
"release-groups"])` — i.e., the value of `response["recording"]`.
"""

from __future__ import annotations

from typing import Literal

_LATE_SENTINEL = (9999, 99, 99)


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


def _release_sort_key(release: dict) -> tuple[int, int, int]:
    y, m, d = parse_partial_date(release.get("date"))
    if y is None:
        return _LATE_SENTINEL
    return (y, m or 0, d or 0)


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


def _releases(payload: dict) -> list[dict]:
    return payload.get("release-list") or payload.get("releases") or []


def earliest_release(payload: dict) -> dict | None:
    rels = _releases(payload)
    if not rels:
        return None
    return min(rels, key=_release_sort_key)


def pick_release(
    payload: dict,
    *,
    prefer: Literal["earliest", "acquired"],
    acquired_mbid: str | None = None,
) -> dict | None:
    """Pick which release drives tag values. Falls back to earliest when
    `prefer="acquired"` but the acquired MBID isn't in the release list."""
    rels = _releases(payload)
    if not rels:
        return None
    if prefer == "acquired" and acquired_mbid:
        for r in rels:
            if r.get("id") == acquired_mbid:
                return r
    return earliest_release(payload)


def _label(release: dict) -> str | None:
    labels = release.get("label-info-list") or release.get("label-info") or []
    for li in labels:
        label = li.get("label") or {}
        name = label.get("name")
        if name:
            return name
    return None


def payload_to_tags(
    payload: dict,
    release: dict,
    *,
    earliest_mbid: str | None = None,
) -> dict[str, str | None]:
    """Map the recording+release pair to our taggable-field dict.

    Only returns fields we can confidently derive from MB. Track number, genre,
    key, and bpm are never proposed here — the file's existing values win.
    """
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
