"""MusicBrainz + AcoustID lookup orchestration.

Flow per track:
  1. If `tags_reviewed_at` is set on the DB row → skip entirely.
  2. Else if the file already has a `recording_mbid` and the stored
     `mb_confidence` meets the skip threshold → identified, no identity search.
  3. Else search MB by the file's artist/title (cache-first) and take the first
     candidate that clears the `match` guards.
  4. Else, when an AcoustID key is configured, fingerprint with fpcalc, call
     AcoustID and pick the best match. Either way the score is persisted.
  5. When identified: pull the entry and its Releases from MB (cache-first),
     then search MB (cached) on the entry's own artist/title and pool every
     other entry with that artist/title whose length is within 3 s of the file.
     Take the Earliest release across the pool and propose the entry carrying
     it, via `mb_filter.payload_to_tags`. A failed pool request skips the Track.
     With no official Release only the Recording's fields are proposed and the
     notes say so. When not identified: no MB tags, and the enrichers and art
     sources carry the diff on their own.
  6. Merge whatever was proposed over the file's values, build a TrackDiff.
  7. Decision/confidence are derived from the identity score bands; an
     unidentified track is always "review".
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from crater.db.repo import SourceCacheRepo, TrackRepo
from crater.tags.art import ArtStager, read_art
from crater.tags.enrich import (
    MB_RPS,
    Fetcher,
    MbGenreFetcher,
    MbSource,
    get_json,
)
from crater.tags.errors import MBLookupError
from crater.tags.io import read_duration, read_tags
from crater.tags.match import (
    artists_match,
    duration_close,
    house_style_title,
    normalize,
    text_match_ok,
    titles_match_without_featuring,
)
from crater.tags.mb_filter import (
    artist_credit_join,
    earliest_release,
    payload_to_tags,
)
from crater.tags.merge import (
    DEFAULT_FIELD_SOURCES,
    MetadataSource,
    StaticSource,
    TrackContext,
    merge_proposals,
)
from crater.tags.models import (
    TAGGABLE_FIELDS,
    ArtChange,
    Confidence,
    Decision,
    TrackDiff,
)
from crater.tags.ratelimit import RateLimiter
from crater.tags.values import current_str

ACOUSTID_RPS = 3.0
MB_SEARCH_LIMIT = 100
MB_WS = "https://musicbrainz.org/ws/2"
MB_BROWSE_LIMIT = 100  # the most MB returns per browse page
# MB sheds load with 503s and expects clients to retry; the fetcher's rate
# limiter already spaces attempts at least a second apart.
MB_ATTEMPTS = 4
# P1 cached the musicbrainzngs payload, which has no Release data, under
# "mb-recording"; a new source name makes those entries miss.
MB_RECORDING_CACHE = "mb-recording-releases"
# Searches cached under "mb-search" asked for 10 hits, too few to reach a
# length-matching hit ranked low by a differing credit; a new name makes them miss.
MB_SEARCH_CACHE = "mb-search-100"


@dataclass
class AcoustIdMatch:
    score: float
    recording_id: str | None


@dataclass
class RecordingMatch:
    """One MB text-search candidate. Fields are JSON-safe: they go in the cache."""

    score: float
    recording_id: str
    artist: str | None
    title: str | None
    duration_s: float | None


class MbClient(Protocol):
    """Narrow interface for the external APIs. Stubbed in tests."""

    def search_recording(self, artist: str, title: str) -> list[RecordingMatch]: ...
    def fingerprint_and_lookup(self, path: Path) -> list[AcoustIdMatch]: ...
    def get_recording(self, recording_mbid: str) -> dict[str, Any]: ...


@dataclass
class LookupConfig:
    preserve_if_set: tuple[str, ...] = ("bpm",)
    acoustid_skip: float = 0.90
    acoustid_high: float = 0.90
    acoustid_medium: float = 0.75
    fingerprint_enabled: bool = True
    replace_art: bool = False
    field_sources: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: dict(DEFAULT_FIELD_SOURCES)
    )


# ---------------------------------------------------------------------------
# Real client (import-guarded so tests can run without the dependencies)
# ---------------------------------------------------------------------------

_useragent_set = False


def _set_useragent() -> None:
    """MB rejects requests from the default useragent; set it once per process."""
    global _useragent_set
    if _useragent_set:
        return
    import musicbrainzngs

    musicbrainzngs.set_useragent("crater", "0.1.0", "https://github.com/jydiw/crater")
    _useragent_set = True


def _parse_search_entry(entry: dict[str, Any]) -> RecordingMatch | None:
    """Build a candidate from one MB search hit; None when the hit is malformed."""
    try:
        recording_id = entry["id"]
        score = int(entry["ext:score"]) / 100
        length = entry.get("length")
        duration_s = int(length) / 1000 if length is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    return RecordingMatch(
        score=score,
        recording_id=recording_id,
        artist=artist_credit_join(entry.get("artist-credit") or []) or None,
        title=entry.get("title"),
        duration_s=duration_s,
    )


@dataclass
class RealMbClient:
    acoustid_key: str | None = None
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)
    fetch: Fetcher = get_json

    def search_recording(self, artist: str, title: str) -> list[RecordingMatch]:
        import musicbrainzngs

        _set_useragent()
        self.rate_limiter.acquire("musicbrainz", MB_RPS)
        try:
            response = musicbrainzngs.search_recordings(
                artist=artist, recording=title, limit=MB_SEARCH_LIMIT
            )
        except musicbrainzngs.MusicBrainzError as e:
            raise MBLookupError(f"MusicBrainz API error: {e}") from e

        parsed = (_parse_search_entry(x) for x in response.get("recording-list", []))
        return [m for m in parsed if m is not None]

    def fingerprint_and_lookup(self, path: Path) -> list[AcoustIdMatch]:
        if self.acoustid_key is None:
            raise MBLookupError("no AcoustID key configured")

        import acoustid  # local import so tests don't need the package

        self.rate_limiter.acquire("acoustid", ACOUSTID_RPS)
        try:
            duration, fp = acoustid.fingerprint_file(str(path))
            response = acoustid.lookup(self.acoustid_key, fp, duration)
        except acoustid.FingerprintGenerationError as e:
            raise MBLookupError(f"fingerprint failed: {e}") from e
        except acoustid.NoBackendError as e:
            raise MBLookupError("fpcalc not installed; brew install chromaprint") from e
        except acoustid.WebServiceError as e:
            raise MBLookupError(f"AcoustID API error: {e}") from e

        matches: list[AcoustIdMatch] = []
        for result in response.get("results", []):
            score = float(result.get("score", 0.0))
            for rec in result.get("recordings", []) or [{"id": None}]:
                matches.append(AcoustIdMatch(score=score, recording_id=rec.get("id")))
        return matches

    def get_recording(self, recording_mbid: str) -> dict[str, Any]:
        """The Recording plus every Release that carries it, in MB's JSON shape.

        A recording lookup cannot include labels or release groups, so the
        Releases come from a paged browse. Both requests take the plain JSON path
        genres use, so the payload has one shape: MB's JSON, not musicbrainzngs's.
        """
        recording = self._get(
            f"{MB_WS}/recording/{recording_mbid}?inc=artists&fmt=json"
        )
        releases: list[Any] = []
        while True:
            page = self._get(
                f"{MB_WS}/release?recording={recording_mbid}"
                f"&inc=labels+media+release-groups&limit={MB_BROWSE_LIMIT}"
                f"&offset={len(releases)}&fmt=json"
            )
            batch = page.get("releases")
            if not isinstance(batch, list):
                raise MBLookupError("MusicBrainz release browse: no release list")
            releases.extend(batch)
            # Without a count, only an empty page proves the list is complete.
            count = page.get("release-count")
            if not batch or (count is not None and len(releases) >= count):
                break
        return {**recording, "releases": releases}

    def _get(self, url: str) -> dict[str, Any]:
        for _ in range(MB_ATTEMPTS):
            payload = self.fetch(
                url, limiter=self.rate_limiter, bucket="musicbrainz", rps=MB_RPS
            )
            if isinstance(payload, dict):
                return payload
        raise MBLookupError(
            f"MusicBrainz request failed after {MB_ATTEMPTS} attempts: {url}"
        )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _current_str_dict(path: Path) -> dict[str, str | None]:
    tags = read_tags(path)
    return {f: current_str(tags, f) for f in TAGGABLE_FIELDS}


def _score_to_decision(score: float, cfg: LookupConfig) -> tuple[Decision, Confidence]:
    if score >= cfg.acoustid_high:
        return ("accept", "high")
    if score >= cfg.acoustid_medium:
        return ("review", "medium")
    return ("review", "low")


@dataclass
class _Identity:
    recording_mbid: str
    score: float
    via: str


def _parse_matches(cached: list[Any]) -> list[RecordingMatch]:
    return [RecordingMatch(**entry) for entry in cached]


def _cached_search(
    conn: sqlite3.Connection, client: MbClient, artist: str, title: str
) -> list[RecordingMatch]:
    """MB text search, cache-first. Raises MBLookupError when MB fails."""
    cache = SourceCacheRepo(conn)
    cache_key = f"{normalize(artist)}|{normalize(title)}"
    cached = cache.get(MB_SEARCH_CACHE, cache_key)
    if cached is None:
        found = client.search_recording(artist, title)
        cached = [asdict(m) for m in found]
        cache.put(MB_SEARCH_CACHE, cache_key, cached)
    assert isinstance(cached, list)  # the cache is shared with dict-shaped sources
    return _parse_matches(cached)


def _search_identity(
    conn: sqlite3.Connection,
    client: MbClient,
    track_row: sqlite3.Row,
) -> _Identity | None:
    """Identify by MB text search over the file's own artist/title."""
    artist = track_row["artist"]
    title = track_row["title"]
    if not artist or not title:
        return None

    try:
        matches = _cached_search(conn, client, artist, title)
    except MBLookupError:
        return None

    duration_s = read_duration(Path(track_row["path"]))
    # A length is evidence a no-length hit cannot give, so every hit with one is
    # tried first; the sort is stable, keeping MB's order within each group.
    for match in sorted(matches, key=lambda m: m.duration_s is None):
        ok = text_match_ok(
            match.score,
            normalize(match.artist or "") == normalize(artist),
            normalize(match.title or "") == normalize(title),
            artists_match(match.artist or "", artist)
            and titles_match_without_featuring(match.title or "", title),
            duration_close(duration_s, match.duration_s),
        )
        if ok:
            TrackRepo(conn).set_mb_confidence(track_row["id"], match.score)
            return _Identity(match.recording_id, match.score, "mb-search")
    return None


def _fingerprint_identity(
    conn: sqlite3.Connection,
    client: MbClient,
    track_row: sqlite3.Row,
) -> _Identity | None:
    try:
        matches = client.fingerprint_and_lookup(Path(track_row["path"]))
    except MBLookupError:
        return None
    matches = [m for m in matches if m.recording_id]
    if not matches:
        return None
    best = max(matches, key=lambda m: m.score)
    assert best.recording_id is not None
    TrackRepo(conn).set_mb_confidence(track_row["id"], best.score)
    return _Identity(best.recording_id, best.score, "acoustid")


def _resolve_identity(
    conn: sqlite3.Connection,
    client: MbClient,
    track_row: sqlite3.Row,
    cfg: LookupConfig,
) -> _Identity | None:
    """Settle on a recording MBID: a confident stored one, else MB text search,
    else a fingerprint when a key is configured."""
    existing_mbid = track_row["recording_mbid"]
    existing_score = track_row["mb_confidence"]
    if (
        existing_mbid
        and existing_score is not None
        and existing_score >= cfg.acoustid_skip
    ):
        return _Identity(existing_mbid, float(existing_score), "stored")

    searched = _search_identity(conn, client, track_row)
    if searched is not None:
        return searched

    if not cfg.fingerprint_enabled:
        return None
    return _fingerprint_identity(conn, client, track_row)


def _cached_recording(
    conn: sqlite3.Connection, client: MbClient, recording_mbid: str
) -> dict[str, Any]:
    """The Recording and its Releases, cache-first. Raises MBLookupError."""
    cache = SourceCacheRepo(conn)
    payload = cache.get(MB_RECORDING_CACHE, recording_mbid)
    if payload is None:
        payload = client.get_recording(recording_mbid)
        cache.put(MB_RECORDING_CACHE, recording_mbid, payload)
    assert isinstance(payload, dict)  # the cache is shared with list-shaped sources
    return payload


def _pooled_entries(
    conn: sqlite3.Connection,
    client: MbClient,
    matched_mbid: str,
    matched: dict[str, Any],
    duration_s: float | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Every MB entry that is the matched Recording by name and length
    (ADR 0001), matched first. Raises MBLookupError."""
    pool = [(matched_mbid, matched)]
    artist = artist_credit_join(matched.get("artist-credit") or [])
    title = matched.get("title") or ""
    if duration_s is None or not artist or not title:
        return pool
    for hit in _cached_search(conn, client, artist, title):
        if (
            hit.recording_id != matched_mbid
            and artists_match(hit.artist or "", artist)
            and titles_match_without_featuring(hit.title or "", title)
            and duration_close(duration_s, hit.duration_s) is True
        ):
            payload = _cached_recording(conn, client, hit.recording_id)
            pool.append((hit.recording_id, payload))
    return pool


def _carrier(
    pool: list[tuple[str, dict[str, Any]]], release: dict[str, Any] | None
) -> tuple[str, dict[str, Any]]:
    """The pooled entry on `release`, lowest ID first so the pick never depends
    on which entry matched; the matched entry when there is no Release."""
    if release is None:
        return pool[0]
    release_id = release.get("id")
    carriers = [
        entry
        for entry in pool
        if any(r.get("id") == release_id for r in entry[1].get("releases") or [])
    ]
    return min(carriers, key=lambda entry: entry[0])


def _propose_art(
    ctx: TrackContext,
    stager: ArtStager | None,
    cfg: LookupConfig,
) -> ArtChange | None:
    """Skip-if-present: a file that already has a picture costs no art request
    unless the user asked for a replacement."""
    if stager is None:
        return None
    had_art = read_art(ctx.path) is not None
    if had_art and not cfg.replace_art:
        return None
    staged = stager.stage(ctx)
    if staged is None:
        return None
    return ArtChange(staged.path, had_art=had_art, source=staged.source)


def lookup_track(
    conn: sqlite3.Connection,
    client: MbClient,
    track_row: sqlite3.Row,
    *,
    lookup_cfg: LookupConfig,
    enrichers: dict[str, MetadataSource] | None = None,
    mb_genre_fetcher: MbGenreFetcher | None = None,
    art_stager: ArtStager | None = None,
) -> TrackDiff | None:
    """Look up MB tags for one track. Returns a TrackDiff or None when skipped
    (already reviewed, file gone, nothing to change or report).

    `enrichers` are extra sources (Deezer, iTunes) merged per
    `lookup_cfg.field_sources`; without them the diff is MB-only.
    """
    if track_row["tags_reviewed_at"] is not None:
        return None

    path = Path(track_row["path"])
    track_id = track_row["id"]
    if not path.exists():
        return None

    identity = _resolve_identity(conn, client, track_row, lookup_cfg)

    # An unidentified track still gets enriched and staged: this library is
    # mostly extended mixes, which MB largely doesn't catalog, but Deezer,
    # iTunes and the art sources search on the file's own artist/title.
    mb_tags: dict[str, str | None] = {}
    release: dict[str, Any] | None = None
    recording_mbid: str | None = None
    duration_s = read_duration(path)
    if identity is not None:
        try:
            payload = _cached_recording(conn, client, identity.recording_mbid)
            pool = _pooled_entries(
                conn, client, identity.recording_mbid, payload, duration_s
            )
        except MBLookupError:
            return None

        release = earliest_release(
            {"releases": [r for _, p in pool for r in p.get("releases") or []]}
        )
        recording_mbid, payload = _carrier(pool, release)
        earliest_mbid = release.get("id") if release else None
        mb_tags = payload_to_tags(payload, release, earliest_mbid=earliest_mbid)

    before = _current_str_dict(path)
    if mb_title := mb_tags.get("title"):
        mb_tags["title"] = house_style_title(mb_title, before["title"])
    ctx = TrackContext(
        path=path,
        before=before,
        duration_s=duration_s,
        recording_mbid=recording_mbid,
        release_mbid=release.get("id") if release else None,
    )
    if identity is None:
        ctx.notes.append("identity: none")
    elif identity.via != "stored":
        ctx.notes.append(f"identity: {identity.via} {identity.score:.2f}")
    no_release = identity is not None and release is None
    if no_release:
        ctx.notes.append("release: no official release")
    sources: dict[str, MetadataSource] = {
        "musicbrainz": MbSource(StaticSource("musicbrainz", mb_tags), mb_genre_fetcher),
        **(enrichers or {}),
    }
    merged = merge_proposals(
        ctx,
        lookup_cfg.field_sources,
        sources,
        preserve_if_set=tuple(lookup_cfg.preserve_if_set),
    )

    art = _propose_art(ctx, art_stager, lookup_cfg)

    # Art alone is a diff worth reviewing, even when every text field agrees;
    # so is a missing official Release, which the user has to pin by hand.
    if before == merged.after and art is None and not no_release:
        return None

    decision, confidence = (
        _score_to_decision(identity.score, lookup_cfg)
        if identity is not None
        else ("review", "review")
    )
    return TrackDiff(
        track_id=track_id,
        path=path,
        decision=decision,
        confidence=confidence,
        before=before,
        after=merged.after,
        notes="; ".join(ctx.notes),
        sources=merged.winners,
        art=art,
    )
