"""AcoustID + MusicBrainz lookup orchestration.

Flow per track:
  1. If `tags_reviewed_at` is set on the DB row → skip entirely.
  2. Else if the file already has a `recording_mbid` and the stored
     `mb_confidence` meets the skip threshold → go straight to MB (cache-first).
  3. Else fingerprint with fpcalc, call AcoustID, pick best match, persist
     the score, then go to MB.
  4. Pull the recording from MB (cache-first), pick a release per `prefer`,
     run `mb_filter.payload_to_tags`, build a TrackDiff.
  5. Decision/confidence are derived from the AcoustID score bands.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from crater.db.repo import MbCacheRepo, TrackRepo
from crater.tags.errors import MBLookupError
from crater.tags.io import read_tags
from crater.tags.mb_filter import earliest_release, payload_to_tags, pick_release
from crater.tags.models import TAGGABLE_FIELDS, Confidence, Decision, TrackDiff
from crater.tags.ratelimit import RateLimiter
from crater.tags.values import current_str

ACOUSTID_RPS = 3.0
MB_RPS = 1.0


@dataclass
class AcoustIdMatch:
    score: float
    recording_id: str | None


class MbClient(Protocol):
    """Narrow interface for the external APIs. Stubbed in tests."""

    def fingerprint_and_lookup(self, path: Path) -> list[AcoustIdMatch]: ...
    def get_recording(self, recording_mbid: str) -> dict[str, Any]: ...


@dataclass
class LookupConfig:
    prefer: Literal["earliest", "acquired"] = "earliest"
    preserve_if_set: tuple[str, ...] = ("bpm",)
    acoustid_skip: float = 0.90
    acoustid_high: float = 0.90
    acoustid_medium: float = 0.75


# ---------------------------------------------------------------------------
# Real client (import-guarded so tests can run without the dependencies)
# ---------------------------------------------------------------------------


@dataclass
class RealMbClient:
    acoustid_key: str
    rate_limiter: RateLimiter = field(default_factory=RateLimiter)

    def fingerprint_and_lookup(self, path: Path) -> list[AcoustIdMatch]:
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

        self.rate_limiter.acquire("musicbrainz", MB_RPS)
        try:
            response = musicbrainzngs.get_recording_by_id(
                recording_mbid,
                includes=["artists", "releases", "release-groups"],
            )
        except musicbrainzngs.WebServiceError as e:
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


def _build_after(
    before: dict[str, str | None],
    mb_tags: dict[str, str | None],
    preserve_if_set: tuple[str, ...],
) -> dict[str, str | None]:
    after = dict(before)
    for field_name, value in mb_tags.items():
        if value is None:
            continue
        if field_name in preserve_if_set and before.get(field_name):
            continue
        after[field_name] = value
    return after


def lookup_track(
    conn: sqlite3.Connection,
    client: MbClient,
    track_row: sqlite3.Row,
    *,
    lookup_cfg: LookupConfig,
) -> TrackDiff | None:
    """Look up MB tags for one track. Returns a TrackDiff or None when skipped
    (already reviewed, fingerprint failed, no MB match, no changes proposed).
    """
    if track_row["tags_reviewed_at"] is not None:
        return None

    path = Path(track_row["path"])
    track_id = track_row["id"]
    if not path.exists():
        return None

    existing_mbid = track_row["recording_mbid"]
    existing_score = track_row["mb_confidence"]

    recording_mbid: str | None
    score: float

    if (
        existing_mbid
        and existing_score is not None
        and existing_score >= lookup_cfg.acoustid_skip
    ):
        recording_mbid = existing_mbid
        score = float(existing_score)
    else:
        try:
            matches = client.fingerprint_and_lookup(path)
        except MBLookupError:
            return None
        matches = [m for m in matches if m.recording_id]
        if not matches:
            return None
        best = max(matches, key=lambda m: m.score)
        score = best.score
        recording_mbid = best.recording_id
        TrackRepo(conn).set_mb_confidence(track_id, score)

    assert recording_mbid is not None

    cache = MbCacheRepo(conn)
    payload = cache.get(recording_mbid)
    if payload is None:
        try:
            payload = client.get_recording(recording_mbid)
        except MBLookupError:
            return None
        cache.put(recording_mbid, payload)

    earliest = earliest_release(payload)
    earliest_mbid = earliest.get("id") if earliest else None
    release = pick_release(
        payload,
        prefer=lookup_cfg.prefer,
        acquired_mbid=track_row["release_mbid"],
    )
    if release is None:
        return None

    mb_tags = payload_to_tags(payload, release, earliest_mbid=earliest_mbid)
    before = _current_str_dict(path)
    after = _build_after(before, mb_tags, tuple(lookup_cfg.preserve_if_set))

    if before == after:
        return None

    decision, confidence = _score_to_decision(score, lookup_cfg)
    return TrackDiff(
        track_id=track_id,
        path=path,
        decision=decision,
        confidence=confidence,
        before=before,
        after=after,
    )
