"""Tests for crater.tags.lookup — MB search + AcoustID orchestration."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from crater.db.repo import SourceCacheRepo, TrackRepo
from crater.tags.art import ArtBlob, StagedArt, embed_art
from crater.tags.diff import _register_track
from crater.tags.lookup import (
    AcoustIdMatch,
    LookupConfig,
    RecordingMatch,
    lookup_track,
)
from crater.tags.merge import TrackContext
from crater.tags.models import TAGGABLE_FIELDS, ArtChange

STAGED_PNG = b"\x89PNG\r\n\x1a\n" + b"staged" * 4
EXISTING_JPEG = b"\xff\xd8\xff" + b"existing" * 4


class StubClient:
    """Canned responses + call counters for lookup_track tests."""

    def __init__(
        self,
        matches: list[AcoustIdMatch] | None = None,
        recording: dict | None = None,
        fingerprint_error: Exception | None = None,
        mb_error: Exception | None = None,
        search_matches: list[RecordingMatch] | None = None,
        search_error: Exception | None = None,
    ) -> None:
        self._matches = matches or []
        self._recording = recording or _default_recording()
        self._search_matches = search_matches or []
        self.fingerprint_error = fingerprint_error
        self.mb_error = mb_error
        self.search_error = search_error
        self.fingerprint_calls = 0
        self.mb_calls = 0
        self.search_calls = 0

    def search_recording(self, artist: str, title: str) -> list[RecordingMatch]:
        self.search_calls += 1
        if self.search_error:
            raise self.search_error
        return list(self._search_matches)

    def fingerprint_and_lookup(self, path: Path) -> list[AcoustIdMatch]:
        self.fingerprint_calls += 1
        if self.fingerprint_error:
            raise self.fingerprint_error
        return list(self._matches)

    def get_recording(self, recording_mbid: str) -> dict[str, Any]:
        self.mb_calls += 1
        if self.mb_error:
            raise self.mb_error
        return dict(self._recording)


def _search_match(
    score: float = 1.0,
    duration_s: float | None = 1.0,  # the fixture FLAC is exactly 1 second
    artist: str = "Test Artist",
    title: str = "Test Track",
) -> RecordingMatch:
    return RecordingMatch(
        score=score,
        recording_id="rec-abc",
        artist=artist,
        title=title,
        duration_s=duration_s,
    )


def _default_recording() -> dict:
    return {
        "id": "rec-abc",
        "title": "MB Title",
        "artist-credit": [{"name": "MB Artist"}],
        "release-list": [
            {
                "id": "rel-early",
                "title": "MB Album",
                "date": "1990-01-01",
                "label-info-list": [{"label": {"name": "MB Label"}}],
            },
            {
                "id": "rel-late",
                "title": "Reissue",
                "date": "2010-06-15",
            },
        ],
    }


def _cfg(**kwargs: Any) -> LookupConfig:
    return LookupConfig(**kwargs)


def _register(db: sqlite3.Connection, path: Path) -> sqlite3.Row:
    tid = _register_track(db, path.resolve())
    row = TrackRepo(db).get_by_id(tid)
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Short-circuit paths
# ---------------------------------------------------------------------------


class TestReviewedSkip:
    def test_reviewed_tracks_are_skipped(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).mark_reviewed(row["id"], int(time.time()))
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient()
        assert lookup_track(db, client, row, lookup_cfg=_cfg()) is None
        assert client.fingerprint_calls == 0
        assert client.mb_calls == 0


class TestSkipFingerprint:
    def test_high_stored_confidence_skips_fingerprint(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).update_tags(row["id"], {"recording_mbid": "rec-abc"})
        TrackRepo(db).set_mb_confidence(row["id"], 0.95)
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient()
        diff = lookup_track(db, client, row, lookup_cfg=_cfg())
        assert diff is not None
        assert client.fingerprint_calls == 0
        assert client.mb_calls == 1

    def test_low_stored_confidence_still_fingerprints(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).update_tags(row["id"], {"recording_mbid": "rec-abc"})
        TrackRepo(db).set_mb_confidence(row["id"], 0.5)
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        lookup_track(db, client, row, lookup_cfg=_cfg())
        assert client.fingerprint_calls == 1


# ---------------------------------------------------------------------------
# Text-search identity
# ---------------------------------------------------------------------------


class TestTextSearch:
    def test_text_hit_skips_the_fingerprint(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(search_matches=[_search_match()])

        diff = lookup_track(db, client, row, lookup_cfg=_cfg())

        assert diff is not None
        assert client.fingerprint_calls == 0
        assert "identity: mb-search" in diff.notes

    def test_weak_score_falls_through_to_the_fingerprint(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            search_matches=[_search_match(score=0.50)],
        )

        diff = lookup_track(db, client, row, lookup_cfg=_cfg())

        assert diff is not None
        assert client.search_calls == 1
        assert client.fingerprint_calls == 1
        assert "identity: acoustid" in diff.notes

    def test_duration_mismatch_falls_through_to_the_fingerprint(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            search_matches=[_search_match(score=0.92, duration_s=11.0)],
        )

        lookup_track(db, client, row, lookup_cfg=_cfg())

        assert client.fingerprint_calls == 1

    def test_no_match_and_no_key_gives_up(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])

        diff = lookup_track(db, client, row, lookup_cfg=_cfg(fingerprint_enabled=False))

        assert diff is None
        assert client.fingerprint_calls == 0

    @pytest.mark.parametrize("blank", ["artist", "title"])
    def test_missing_artist_or_title_skips_the_search(
        self, db: sqlite3.Connection, flac_with_tags: Path, blank: str
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).update_tags(row["id"], {blank: None})
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            search_matches=[_search_match()],
        )
        lookup_track(db, client, row, lookup_cfg=_cfg())

        assert client.search_calls == 0
        assert client.fingerprint_calls == 1

    def test_search_is_cached_under_the_normalized_query(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(search_matches=[_search_match()])

        lookup_track(db, client, row, lookup_cfg=_cfg())
        second = lookup_track(db, client, row, lookup_cfg=_cfg())

        assert client.search_calls == 1
        assert second is not None
        assert "identity: mb-search" in second.notes  # cached matches still resolve
        assert SourceCacheRepo(db).get("mb-search", "test artist|test track") is not None

    def test_search_error_skips_the_track(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        from crater.tags.errors import MBLookupError

        row = _register(db, flac_with_tags)
        client = StubClient(search_error=MBLookupError("mb down"))

        diff = lookup_track(db, client, row, lookup_cfg=_cfg(fingerprint_enabled=False))

        assert diff is None
        assert client.fingerprint_calls == 0


# ---------------------------------------------------------------------------
# MB cache
# ---------------------------------------------------------------------------


class TestMbCache:
    def test_cache_hit_skips_mb_call(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).update_tags(row["id"], {"recording_mbid": "rec-abc"})
        TrackRepo(db).set_mb_confidence(row["id"], 0.95)
        SourceCacheRepo(db).put("mb-recording", "rec-abc", _default_recording())
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient()
        lookup_track(db, client, row, lookup_cfg=_cfg())
        assert client.mb_calls == 0

    def test_cache_miss_stores_response(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        lookup_track(db, client, row, lookup_cfg=_cfg())
        assert SourceCacheRepo(db).get("mb-recording", "rec-abc") is not None


# ---------------------------------------------------------------------------
# Decision bands
# ---------------------------------------------------------------------------


class TestDecisionBands:
    @pytest.mark.parametrize(
        "score,expected_decision,expected_confidence",
        [
            (0.95, "accept", "high"),
            (0.90, "accept", "high"),
            (0.80, "review", "medium"),
            (0.60, "review", "low"),
        ],
    )
    def test_score_to_decision(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        score: float,
        expected_decision: str,
        expected_confidence: str,
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(score, "rec-abc")])
        diff = lookup_track(db, client, row, lookup_cfg=_cfg())
        assert diff is not None
        assert diff.decision == expected_decision
        assert diff.confidence == expected_confidence


# ---------------------------------------------------------------------------
# preserve_if_set
# ---------------------------------------------------------------------------


class TestPreserveIfSet:
    def test_bpm_preserved_when_file_has_value(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        # MB never proposes bpm anyway, but the file has "120" tagged.
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        diff = lookup_track(db, client, row, lookup_cfg=_cfg())
        assert diff is not None
        assert diff.after["bpm"] == "120"

    def test_artist_is_overwritten_even_if_file_had_one(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        diff = lookup_track(db, client, row, lookup_cfg=_cfg())
        assert diff is not None
        assert diff.after["artist"] == "MB Artist"


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


class TestFailureModes:
    def test_no_acoustid_match_returns_none(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[])
        assert lookup_track(db, client, row, lookup_cfg=_cfg()) is None

    def test_fingerprint_error_returns_none(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        from crater.tags.errors import MBLookupError

        row = _register(db, flac_with_tags)
        client = StubClient(fingerprint_error=MBLookupError("boom"))
        assert lookup_track(db, client, row, lookup_cfg=_cfg()) is None

    def test_mb_error_returns_none(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        from crater.tags.errors import MBLookupError

        row = _register(db, flac_with_tags)
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            mb_error=MBLookupError("mb down"),
        )
        assert lookup_track(db, client, row, lookup_cfg=_cfg()) is None


# ---------------------------------------------------------------------------
# prefer="acquired"
# ---------------------------------------------------------------------------


class TestPreferAcquired:
    def test_uses_acquired_release_when_mbid_set(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).update_tags(row["id"], {"release_mbid": "rel-late"})
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        diff = lookup_track(db, client, row, lookup_cfg=_cfg(prefer="acquired"))
        assert diff is not None
        assert diff.after["release_mbid"] == "rel-late"
        assert diff.after["earliest_mbid"] == "rel-early"

    def test_default_prefers_earliest(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        TrackRepo(db).update_tags(row["id"], {"release_mbid": "rel-late"})
        row = TrackRepo(db).get_by_id(row["id"])

        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        diff = lookup_track(db, client, row, lookup_cfg=_cfg())
        assert diff is not None
        assert diff.after["release_mbid"] == "rel-early"


# ---------------------------------------------------------------------------
# Enrichers
# ---------------------------------------------------------------------------


class StubSource:
    """Canned values + call counters, standing in for a network adapter."""

    def __init__(self, name: str, values: dict[str, str | None]) -> None:
        self.name = name
        self.values = values
        self.calls = 0
        self.asked: list[frozenset[str]] = []

    def propose(
        self, ctx: TrackContext, fields: frozenset[str]
    ) -> dict[str, str | None]:
        self.calls += 1
        self.asked.append(fields)
        return {f: self.values[f] for f in fields if f in self.values}


class StubGenreFetcher:
    def __init__(self, genre: str | None) -> None:
        self.genre_value = genre
        self.calls = 0

    def genre(self, recording_mbid: str) -> str | None:
        self.calls += 1
        return self.genre_value


class StubStager:
    """Hands back one canned staged image and counts what it was asked for."""

    def __init__(self, staged: Path | None) -> None:
        self.staged = staged
        self.asked: list[TrackContext] = []

    def stage(self, ctx: TrackContext) -> StagedArt | None:
        self.asked.append(ctx)
        if self.staged is None:
            return None
        return StagedArt(self.staged, "coverartarchive")


class TestArt:
    def _stager(self, tmp_path: Path) -> StubStager:
        staged = tmp_path / "rel-early.png"
        staged.write_bytes(STAGED_PNG)
        return StubStager(staged)

    def test_art_is_proposed_when_the_file_has_none(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        stager = self._stager(tmp_path)

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(),
            art_stager=stager,  # type: ignore[arg-type]
        )

        assert diff is not None
        assert diff.art == ArtChange(stager.staged, had_art=False)
        assert [c.release_mbid for c in stager.asked] == ["rel-early"]

    def test_existing_art_is_left_alone(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        stager = self._stager(tmp_path)

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(),
            art_stager=stager,  # type: ignore[arg-type]
        )

        assert diff is not None
        assert diff.art is None
        assert stager.asked == []

    def test_replace_art_proposes_over_an_existing_picture(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        stager = self._stager(tmp_path)

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(replace_art=True),
            art_stager=stager,  # type: ignore[arg-type]
        )

        assert diff is not None
        assert diff.art == ArtChange(stager.staged, had_art=True)

    def test_art_alone_still_yields_a_diff(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        stager = self._stager(tmp_path)

        # No field takes proposals, so every text value stays as the file has it.
        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(field_sources={f: () for f in TAGGABLE_FIELDS}),
            art_stager=stager,  # type: ignore[arg-type]
        )

        assert diff is not None
        assert diff.changed_fields() == []
        assert diff.art == ArtChange(stager.staged, had_art=False)

    def test_no_stager_means_no_art(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])

        diff = lookup_track(db, client, row, lookup_cfg=_cfg())

        assert diff is not None
        assert diff.art is None


class TestEnrichers:
    def test_deezer_fills_an_empty_bpm(
        self, db: sqlite3.Connection, flac_no_tags: Path
    ) -> None:
        row = _register(db, flac_no_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        deezer = StubSource("deezer", {"bpm": "128"})

        diff = lookup_track(
            db, client, row, lookup_cfg=_cfg(), enrichers={"deezer": deezer}
        )

        assert diff is not None
        assert diff.after["bpm"] == "128"
        assert diff.sources["bpm"] == "deezer"

    def test_tagged_bpm_is_never_asked_about(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        deezer = StubSource("deezer", {"bpm": "128"})

        diff = lookup_track(
            db, client, row, lookup_cfg=_cfg(), enrichers={"deezer": deezer}
        )

        assert diff is not None
        assert diff.after["bpm"] == "120"
        assert all("bpm" not in asked for asked in deezer.asked)

    def test_itunes_is_not_consulted_once_deezer_has_the_genre(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        deezer = StubSource("deezer", {"genre": "House"})
        itunes = StubSource("itunes", {"genre": "Dance"})

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(),
            enrichers={"deezer": deezer, "itunes": itunes},
        )

        assert diff is not None
        assert diff.sources["genre"] == "deezer"
        assert itunes.calls == 0

    def test_itunes_answers_when_deezer_has_no_genre(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        deezer = StubSource("deezer", {})
        itunes = StubSource("itunes", {"genre": "Dance"})

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(),
            enrichers={"deezer": deezer, "itunes": itunes},
        )

        assert diff is not None
        assert diff.after["genre"] == "Dance"
        assert diff.sources["genre"] == "itunes"

    def test_mb_genre_is_the_last_resort(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.95, "rec-abc")])
        deezer = StubSource("deezer", {})
        itunes = StubSource("itunes", {})
        fetcher = StubGenreFetcher("Techno")

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(),
            enrichers={"deezer": deezer, "itunes": itunes},
            mb_genre_fetcher=fetcher,  # type: ignore[arg-type]
        )

        assert diff is not None
        assert diff.after["genre"] == "Techno"
        assert diff.sources["genre"] == "musicbrainz"
        assert fetcher.calls == 1


# ---------------------------------------------------------------------------
# Unidentified tracks
# ---------------------------------------------------------------------------


class TestUnidentified:
    """MB can't name it — an extended mix, usually — but the enrichers and art
    sources search on the file's own tags, so there is still a diff to review."""

    def test_enricher_genre_survives_a_failed_identity(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient()
        deezer = StubSource("deezer", {"genre": "House"})

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(fingerprint_enabled=False),
            enrichers={"deezer": deezer},
        )

        assert diff is not None
        assert diff.decision == "review"
        assert diff.confidence == "review"
        assert "identity: none" in diff.notes
        assert diff.after["genre"] == "House"
        assert diff.sources["genre"] == "deezer"

    def test_art_alone_yields_a_review_diff(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient()
        staged = tmp_path / "deezer-42.png"
        staged.write_bytes(STAGED_PNG)

        diff = lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(fingerprint_enabled=False),
            art_stager=StubStager(staged),  # type: ignore[arg-type]
        )

        assert diff is not None
        assert diff.before == diff.after
        assert diff.art == ArtChange(staged, had_art=False)

    def test_nothing_proposed_is_still_a_skip(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient()

        diff = lookup_track(db, client, row, lookup_cfg=_cfg(fingerprint_enabled=False))

        assert diff is None

    def test_no_identity_skips_the_mb_fetch(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient()
        deezer = StubSource("deezer", {"genre": "House"})

        lookup_track(
            db,
            client,
            row,
            lookup_cfg=_cfg(fingerprint_enabled=False),
            enrichers={"deezer": deezer},
        )

        assert client.mb_calls == 0

    def test_identified_tracks_keep_their_score_band(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(matches=[AcoustIdMatch(0.80, "rec-abc")])

        diff = lookup_track(db, client, row, lookup_cfg=_cfg())

        assert diff is not None
        assert (diff.decision, diff.confidence) == ("review", "medium")
        assert "identity: none" not in diff.notes

    def test_identity_without_a_usable_release_is_a_skip(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording={"id": "rec-abc", "title": "MB Title", "release-list": []},
        )

        assert lookup_track(db, client, row, lookup_cfg=_cfg()) is None
        assert client.mb_calls == 1
