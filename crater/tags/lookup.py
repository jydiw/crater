"""MusicBrainz + AcoustID lookup orchestration.

Flow per track:
  1. If `tags_reviewed_at` is set on the DB row → skip entirely.
  2. Else if the file already has a `recording_mbid` and the stored
     `mb_confidence` meets the skip threshold → go straight to MB (cache-first).
  3. Else search MB by the file's artist/title (cache-first) and take the first
     candidate that clears the `match` guards.
  4. Else, when an AcoustID key is configured, fingerprint with fpcalc, call
     AcoustID and pick the best match. Either way the score is persisted.
  5. When identified: pull the recording from MB (cache-first), pick a release
     per `prefer`, run `mb_filter.payload_to_tags`. When not: no MB tags, and
     the enrichers and art sources carry the diff on their own.
  6. Merge whatever was proposed over the file's values, build a TrackDiff.
  7. Decision/confidence are derived from the identity score bands; an
     unidentified track is always "review".
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from crater.db.repo import SourceCacheRepo, TrackRepo
from crater.tags.art import ArtStager, read_art
from crater.tags.enrich import MB_RPS, MbGenreFetcher, MbSource
from crater.tags.errors import MBLookupError
from crater.tags.io import read_duration, read_tags
from crater.tags.match import duration_close, normalize, text_match_ok
from crater.tags.mb_filter import (
    artist_credit_join,
    earliest_release,
    payload_to_tags,
    pick_release,
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
MB_SEARCH_LIMIT = 10


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
    prefer: Literal["earliest", "acquired"] = "earliest"
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
        import musicbrainzngs

        _set_useragent()
        self.rate_limiter.acquire("musicbrainz", MB_RPS)
        try:
            # "release-groups" is not a valid include for a recording lookup
            response = musicbrainzngs.get_recording_by_id(
                recording_mbid,
                includes=["artists", "releases"],
            )
        except musicbrainzngs.MusicBrainzError as e:
            raise MBLookupError(f"MusicBrainz API error: {e}") from e
        return response["recording"]


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

    cache = SourceCacheRepo(conn)
    cache_key = f"{normalize(artist)}|{normalize(title)}"
    cached = cache.get("mb-search", cache_key)
    if cached is None:
        try:
            found = client.search_recording(artist, title)
        except MBLookupError:
            return None
        cached = [asdict(m) for m in found]
        cache.put("mb-search", cache_key, cached)
    assert isinstance(cached, list)  # the cache is shared with dict-shaped sources

    duration_s = read_duration(Path(track_row["path"]))
    for match in _parse_matches(cached):
        ok = text_match_ok(
            match.score,
            normalize(match.artist or "") == normalize(artist),
            normalize(match.title or "") == normalize(title),
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
    (already reviewed, file gone, no usable release, no changes proposed).

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
    release_mbid: str | None = None
    if identity is not None:
        cache = SourceCacheRepo(conn)
        payload = cache.get("mb-recording", identity.recording_mbid)
        if payload is None:
            try:
                payload = client.get_recording(identity.recording_mbid)
            except MBLookupError:
                return None
            cache.put("mb-recording", identity.recording_mbid, payload)
        assert isinstance(payload, dict)  # the cache is shared with list-shaped sources

        earliest = earliest_release(payload)
        earliest_mbid = earliest.get("id") if earliest else None
        release = pick_release(
            payload,
            prefer=lookup_cfg.prefer,
            acquired_mbid=track_row["release_mbid"],
        )
        if release is None:
            return None
        release_mbid = release.get("id")
        mb_tags = payload_to_tags(payload, release, earliest_mbid=earliest_mbid)

    before = _current_str_dict(path)
    ctx = TrackContext(
        path=path,
        before=before,
        duration_s=read_duration(path),
        recording_mbid=identity.recording_mbid if identity is not None else None,
        release_mbid=release_mbid,
    )
    if identity is None:
        ctx.notes.append("identity: none")
    elif identity.via != "stored":
        ctx.notes.append(f"identity: {identity.via} {identity.score:.2f}")
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

    # Art alone is a diff worth reviewing, even when every text field agrees.
    if before == merged.after and art is None:
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
