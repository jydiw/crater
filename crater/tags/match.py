"""Guards for accepting a MusicBrainz text-search hit as a track's identity.

A search score alone is not evidence: MB scores how well the *query* matched,
not whether this is the same recording. Duration is the cheap independent
check, so a close duration lets a good score through and a far one vetoes it.
When duration is unknown on either side, the artist and title strings have to
agree exactly (normalized) on top of a near-perfect score.

The enrichment guards at the bottom (`artist_overlaps`, `titles_match`) are
looser than that on purpose — a wrong genre costs less than a wrong identity,
and this library's extended mixes and multi-artist credits fail exact equality
against the right hit.
"""

from __future__ import annotations

import re

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

_SCORE_WITH_DURATION = 0.90
_SCORE_WITHOUT_DURATION = 0.95

# Closed allowlist: these name the same recording as the bare title. A remix or
# an instrumental is a different recording with its own release and artwork, so
# nothing may be added here that isn't just a packaging label.
_MIX_DESCRIPTORS = frozenset(
    {
        "extended",
        "extended mix",
        "extended version",
        "original",
        "original mix",
        "original version",
        "radio",
        "radio edit",
        "radio mix",
        "radio version",
    }
)

_TRAILING_GROUP_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]\s*$")

# Whole words only, so "feature", "defeat", "soft" and "lift" are left alone.
_FEATURING = r"\b(?:feat|featuring|ft)\b"
_ARTIST_FEATURING_RE = re.compile(rf"\s*{_FEATURING}.*", re.IGNORECASE)
# Any other bracketed part is matched as `keep` so a featuring word inside it,
# such as "(X Remix feat. Y)", is not taken for an unbracketed phrase.
_TITLE_FEATURING_RE = re.compile(
    rf"[(\[]{_FEATURING}[^()\[\]]*[)\]]"
    r"|(?P<keep>[(\[][^()\[\]]*[)\]])"
    rf"|{_FEATURING}[^()\[\]]*",
    re.IGNORECASE,
)

# House style, not matching: unrelated to _MIX_DESCRIPTORS above.
_HOUSE_STYLE_WORDS = frozenset(
    {
        "remix",
        "mix",
        "edit",
        "dub",
        "version",
        "instrumental",
        "rework",
        "bootleg",
        "original",
        "extended",
        "radio",
        "club",
        "vocal",
    }
)
_BRACKET_GROUP_RE = re.compile(r"[(\[][^()\[\]]*[)\]]")
_WORD_RE = re.compile(r"\w+(?:-\w+)*")
_ORIGINAL_MIX_RE = re.compile(r"[(\[]original mix[)\]]\s*$", re.IGNORECASE)


def normalize(s: str) -> str:
    """Casefolded, punctuation-free form for comparing artist/title strings."""
    return _WS_RE.sub(" ", _PUNCT_RE.sub("", s.casefold())).strip()


def duration_close(
    a_s: float | None, b_s: float | None, *, tolerance_s: float = 3.0
) -> bool | None:
    """None when either duration is unknown — "don't know" is not "doesn't match"."""
    if a_s is None or b_s is None:
        return None
    return abs(a_s - b_s) <= tolerance_s


def text_match_ok(
    score: float,
    artist_eq: bool,
    title_eq: bool,
    names_match: bool,
    dur: bool | None,
) -> bool:
    """`names_match` is the looser featuring-blind comparison: with the length
    agreeing, it stands in for a score that a differing credit drags down."""
    if dur is False:
        return False
    if dur is None:
        return score >= _SCORE_WITHOUT_DURATION and artist_eq and title_eq
    return score >= _SCORE_WITH_DURATION or names_match


def strip_mix_suffix(title: str) -> str:
    """Drop one trailing `(...)`/`[...]` group, and only if it's a descriptor."""
    match = _TRAILING_GROUP_RE.search(title)
    if match is None or normalize(match.group(1)) not in _MIX_DESCRIPTORS:
        return title
    return title[: match.start()].rstrip()


def titles_match(a: str, b: str) -> bool:
    """Equal once packaging labels are dropped. Empty is never a match."""
    left, right = normalize(strip_mix_suffix(a)), normalize(strip_mix_suffix(b))
    return bool(left) and left == right


def artist_overlaps(a: str, b: str) -> bool:
    """A file tag credits every artist; a search hit often credits only one."""
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return False
    return left in right or right in left


def drop_featuring_from_artist(artist: str) -> str:
    """The credit before any featuring phrase; MB often credits guests apart."""
    return _ARTIST_FEATURING_RE.sub("", artist)


def drop_featuring_from_title(title: str) -> str:
    """The title without its featuring phrases, wherever the file put them."""
    dropped = _TITLE_FEATURING_RE.sub(lambda m: m.group("keep") or " ", title)
    return _WS_RE.sub(" ", dropped).strip()


def artists_match(a: str, b: str) -> bool:
    """Same credit for identity, once guests are dropped from both sides: one
    contains the other as a run of whole words. Empty is never a match."""
    left = normalize(drop_featuring_from_artist(a))
    right = normalize(drop_featuring_from_artist(b))
    if not left or not right:
        return False
    # normalize leaves single spaces and no punctuation, so padding with spaces
    # makes substring containment line up on word boundaries.
    return f" {left} " in f" {right} " or f" {right} " in f" {left} "


def titles_match_without_featuring(a: str, b: str) -> bool:
    return titles_match(drop_featuring_from_title(a), drop_featuring_from_title(b))


def _capitalise_word(match: re.Match[str]) -> str:
    parts = match.group().split("-")
    if not any(part.casefold() in _HOUSE_STYLE_WORDS for part in parts):
        return match.group()
    return "-".join(part[0].upper() + part[1:] for part in parts)


def _capitalise_group(match: re.Match[str]) -> str:
    return _WORD_RE.sub(_capitalise_word, match.group())


def _has_mix_descriptor(title: str) -> bool:
    return any(
        part.casefold() in _HOUSE_STYLE_WORDS
        for group in _BRACKET_GROUP_RE.findall(title)
        for word in _WORD_RE.findall(group)
        for part in word.split("-")
    )


def house_style_title(source: str, current: str | None) -> str:
    """The Source title with crater's House style applied.

    Sources drop `(Original Mix)`, but a DJ library keeps it to tell the
    original apart from remixes, so a file that already has it keeps it.
    """
    styled = _BRACKET_GROUP_RE.sub(_capitalise_group, source)
    if (
        current is not None
        and _ORIGINAL_MIX_RE.search(current)
        and not _has_mix_descriptor(source)
    ):
        return f"{styled} (Original Mix)"
    return styled
