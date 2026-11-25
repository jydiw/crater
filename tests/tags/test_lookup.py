"""Tests for crater.tags.lookup — MB search + AcoustID orchestration."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from crater.db.repo import SourceCacheRepo, TrackRepo
from crater.tags.api import generate_diff
from crater.tags.art import ArtBlob, StagedArt, embed_art
from crater.tags.diff import _register_track
from crater.tags.lookup import (
    AcoustIdMatch,
    LookupConfig,
    RealMbClient,
    RecordingMatch,
    lookup_track,
)
from crater.tags.merge import TrackContext
from crater.tags.models import TAGGABLE_FIELDS, ArtChange
from tests.conftest import _write_silent_flac
from tests.tags.test_enrich import FakeFetch

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


def _release(
    id_: str,
    title: str,
    date: str | None,
    *,
    status: str | None = "Official",
    primary: str = "Single",
    secondary: tuple[str, ...] = (),
    label: str | None = None,
    catno: str | None = None,
) -> dict:
    """One Release in the shape MusicBrainz's JSON release browse returns."""
    release: dict = {
        "id": id_,
        "title": title,
        "date": date,
        "country": "GB",
        "release-group": {
            "id": f"rg-{id_}",
            "primary-type": primary,
            "secondary-types": list(secondary),
        },
        "media": [{"format": "CD"}],
        "label-info": (
            [{"catalog-number": catno, "label": {"name": label}}] if label else []
        ),
    }
    if status is not None:
        release["status"] = status
    return release


def _default_recording() -> dict:
    return {
        "id": "rec-abc",
        "title": "MB Title",
        "artist-credit": [{"name": "MB Artist"}],
        "releases": [
            _release(
                "rel-early", "MB Album", "1990-01-01", label="MB Label", catno="MB-1"
            ),
            _release("rel-late", "Reissue", "2010-06-15", primary="Album"),
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
        assert (
            SourceCacheRepo(db).get("mb-search", "test artist|test track") is not None
        )

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
        SourceCacheRepo(db).put(
            "mb-recording-releases", "rec-abc", _default_recording()
        )
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
        cached = SourceCacheRepo(db).get("mb-recording-releases", "rec-abc")
        assert cached is not None


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
# The release ID already in the file
# ---------------------------------------------------------------------------


class TestAcquiredReleaseIgnored:
    def test_the_earliest_release_wins_over_the_file_release_id(
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
# Earliest release, through the tags API
# ---------------------------------------------------------------------------


def _sky_falls_down(*releases: dict) -> dict:
    return {
        "id": "rec-abc",
        "title": "Sky Falls Down (Armin van Buuren remix)",
        "artist-credit": [{"name": "OceanLab"}],
        "releases": list(releases),
    }


_SINGLE_2002 = _release(
    "rel-single",
    "Sky Falls Down",
    "2002-10-14",
    label="Anjunabeats",
    catno="ANJCDS002",
)
_PROMO_2003 = _release(
    "rel-promo",
    "Sky Falls Down (promo)",
    "2003",
    status="Promotion",
    label="Promo Pool",
    catno="PROMO-1",
)
_ALBUM_2008 = _release(
    "rel-album",
    "Sirens of the Sea",
    "2008-07-21",
    primary="Album",
    label="Anjunabeats",
    catno="ANJCD011",
)


class TestEarliestReleaseInTheDiff:
    def test_the_2002_single_supplies_album_date_and_label(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording=_sky_falls_down(_ALBUM_2008, _PROMO_2003, _SINGLE_2002),
        )

        [diff] = generate_diff(db, [flac_with_tags], client=client)

        assert diff.after["album"] == "Sky Falls Down"
        assert diff.after["release_year"] == "2002"
        assert diff.after["release_month"] == "10"
        assert diff.after["release_day"] == "14"
        assert diff.after["label"] == "Anjunabeats"
        assert diff.after["release_mbid"] == "rel-single"
        assert diff.after["earliest_mbid"] == "rel-single"

    def test_no_official_release_proposes_no_release_fields(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording=_sky_falls_down(_PROMO_2003),
        )

        [diff] = generate_diff(db, [flac_with_tags], client=client)

        assert diff.after["title"] == "Sky Falls Down (Armin van Buuren Remix)"
        assert diff.after["album"] == "Test Album"
        assert diff.after["release_year"] == "1996"
        assert diff.after["release_month"] == "9"
        assert diff.after["release_day"] == "23"
        assert diff.after["label"] == "Test Label"
        assert diff.after["release_mbid"] is None
        assert diff.after["earliest_mbid"] is None
        assert "release: no official release" in diff.notes

    def test_no_official_release_is_reported_even_with_nothing_to_change(
        self, tmp_path: Path, db: sqlite3.Connection
    ) -> None:
        path = _write_silent_flac(
            tmp_path / "already.flac",
            tags={
                "title": "Test Track",
                "artist": "Test Artist",
                "musicbrainz_trackid": "rec-abc",
            },
        )
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording=_already_tagged_recording(_PROMO_2003),
        )

        diffs = generate_diff(db, [path], client=client)

        assert len(diffs) == 1
        assert diffs[0].changed_fields() == []
        assert "release: no official release" in diffs[0].notes

    def test_an_identified_track_with_nothing_to_report_is_dropped(
        self, tmp_path: Path, db: sqlite3.Connection
    ) -> None:
        path = _write_silent_flac(
            tmp_path / "already.flac",
            tags={
                "title": "Test Track",
                "artist": "Test Artist",
                "album": "Test Album",
                "date": "1996-09-23",
                "organization": "Test Label",
                "musicbrainz_trackid": "rec-abc",
                "musicbrainz_albumid": "rel-test",
                "musicbrainz_earliestid": "rel-test",
            },
        )
        official = _release("rel-test", "Test Album", "1996-09-23", label="Test Label")
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording=_already_tagged_recording(official),
        )

        assert generate_diff(db, [path], client=client) == []


class TestHouseStyleInTheDiff:
    def test_the_mix_descriptor_is_capitalised(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording=_sky_falls_down(_SINGLE_2002),
        )

        [diff] = generate_diff(db, [flac_with_tags], client=client)

        assert diff.after["title"] == "Sky Falls Down (Armin van Buuren Remix)"

    def test_the_files_original_mix_is_kept(
        self, tmp_path: Path, db: sqlite3.Connection
    ) -> None:
        path = _write_silent_flac(
            tmp_path / "cola.flac",
            tags={"title": "cola (original mix)", "artist": "CamelPhat"},
        )
        recording = {
            "id": "rec-abc",
            "title": "Cola",
            "artist-credit": [{"name": "CamelPhat"}],
            "releases": [_SINGLE_2002],
        }
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")], recording=recording
        )

        [diff] = generate_diff(db, [path], client=client)

        assert diff.after["title"] == "Cola (Original Mix)"

    def test_album_and_artist_are_not_house_styled(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        recording = {
            "id": "rec-abc",
            "title": "Sky Falls Down",
            "artist-credit": [{"name": "OceanLab (dub)"}],
            "releases": [_release("rel-x", "Sky Falls Down (club mix)", "2002")],
        }
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")], recording=recording
        )

        [diff] = generate_diff(db, [flac_with_tags], client=client)

        assert diff.after["album"] == "Sky Falls Down (club mix)"
        assert diff.after["artist"] == "OceanLab (dub)"


def _already_tagged_recording(*releases: dict) -> dict:
    """The Recording whose title and artist the fixture FLAC already carries."""
    return {
        "id": "rec-abc",
        "title": "Test Track",
        "artist-credit": [{"name": "Test Artist"}],
        "releases": list(releases),
    }


_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/rec-abc?inc=artists&fmt=json"


def _releases_url(offset: int) -> str:
    return (
        "https://musicbrainz.org/ws/2/release?recording=rec-abc"
        f"&inc=labels+media+release-groups&limit=100&offset={offset}&fmt=json"
    )


def _two_page_payloads() -> dict[str, object]:
    """101 Releases: a page of later album issues, then the 2002 single alone."""
    albums = [
        _release(f"rel-{i:03d}", "Sirens of the Sea", "2008-07-21", primary="Album")
        for i in range(100)
    ]
    return {
        _RECORDING_URL: _sky_falls_down(),
        _releases_url(0): {
            "release-count": 101,
            "release-offset": 0,
            "releases": albums,
        },
        _releases_url(100): {
            "release-count": 101,
            "release-offset": 100,
            "releases": [_SINGLE_2002],
        },
    }


def _register_with_stored_identity(db: sqlite3.Connection, path: Path) -> None:
    """A confident recording ID already on the row, so no search request is made."""
    generate_diff(db, [path])
    row = TrackRepo(db).get_by_path(path.resolve())
    assert row is not None
    TrackRepo(db).update_tags(row["id"], {"recording_mbid": "rec-abc"})
    TrackRepo(db).set_mb_confidence(row["id"], 0.95)


class TestRealClientReleaseList:
    def test_a_release_list_longer_than_one_page_is_fetched_completely(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        client = RealMbClient(fetch=FakeFetch(_two_page_payloads()))

        [diff] = generate_diff(db, [flac_with_tags], client=client)

        assert diff.after["album"] == "Sky Falls Down"
        assert diff.after["release_year"] == "2002"
        assert diff.after["label"] == "Anjunabeats"

    def test_pages_without_a_release_count_are_fetched_until_one_comes_back_empty(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        payloads = _two_page_payloads()
        for offset in (0, 100):
            del payloads[_releases_url(offset)]["release-count"]  # type: ignore[attr-defined]
        payloads[_releases_url(101)] = {"release-offset": 101, "releases": []}

        [diff] = generate_diff(
            db, [flac_with_tags], client=RealMbClient(fetch=FakeFetch(payloads))
        )

        assert diff.after["album"] == "Sky Falls Down"
        assert diff.after["release_year"] == "2002"

    def test_a_second_diff_of_the_same_file_makes_no_request(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        fetch = FakeFetch(_two_page_payloads())
        client = RealMbClient(fetch=fetch)

        generate_diff(db, [flac_with_tags], client=client)
        requests_after_first = list(fetch.urls)
        [second] = generate_diff(db, [flac_with_tags], client=client)

        assert requests_after_first == [
            _RECORDING_URL,
            _releases_url(0),
            _releases_url(100),
        ]
        assert fetch.urls == requests_after_first
        assert second.after["album"] == "Sky Falls Down"

    def test_a_failed_page_is_not_cached_as_a_short_list(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        payloads = _two_page_payloads()
        last_page = payloads.pop(_releases_url(100))

        failed = generate_diff(
            db, [flac_with_tags], client=RealMbClient(fetch=FakeFetch(payloads))
        )
        payloads[_releases_url(100)] = last_page
        [retried] = generate_diff(
            db, [flac_with_tags], client=RealMbClient(fetch=FakeFetch(payloads))
        )

        assert failed == []
        assert retried.after["album"] == "Sky Falls Down"

    def test_a_recording_cached_before_release_data_is_fetched_again(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        # What P1 cached: musicbrainzngs's shape, no status, no label data.
        SourceCacheRepo(db).put(
            "mb-recording",
            "rec-abc",
            {"id": "rec-abc", "title": "Old", "release-list": [{"id": "rel-x"}]},
        )
        fetch = FakeFetch(_two_page_payloads())

        [diff] = generate_diff(db, [flac_with_tags], client=RealMbClient(fetch=fetch))

        assert diff.after["album"] == "Sky Falls Down"
        assert _RECORDING_URL in fetch.urls

    def test_a_recording_lookup_that_fails_once_is_retried(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        fetch = FakeFetch(_two_page_payloads(), failures={_RECORDING_URL: 1})

        [diff] = generate_diff(db, [flac_with_tags], client=RealMbClient(fetch=fetch))

        assert diff.after["album"] == "Sky Falls Down"

    def test_a_release_page_that_fails_once_is_retried(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        fetch = FakeFetch(_two_page_payloads(), failures={_releases_url(100): 1})

        [diff] = generate_diff(db, [flac_with_tags], client=RealMbClient(fetch=fetch))

        assert diff.after["album"] == "Sky Falls Down"
        assert diff.after["release_year"] == "2002"

    def test_a_request_that_never_answers_is_tried_four_times(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        _register_with_stored_identity(db, flac_with_tags)
        dead = FakeFetch(_two_page_payloads(), failures={_RECORDING_URL: 99})

        failed = generate_diff(db, [flac_with_tags], client=RealMbClient(fetch=dead))
        [retried] = generate_diff(
            db,
            [flac_with_tags],
            client=RealMbClient(fetch=FakeFetch(_two_page_payloads())),
        )

        assert dead.urls == [_RECORDING_URL] * 4
        assert failed == []
        assert retried.after["album"] == "Sky Falls Down"


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

    def test_identity_without_any_release_says_so_in_the_notes(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        row = _register(db, flac_with_tags)
        client = StubClient(
            matches=[AcoustIdMatch(0.95, "rec-abc")],
            recording={"id": "rec-abc", "title": "MB Title", "releases": []},
        )

        diff = lookup_track(db, client, row, lookup_cfg=_cfg())

        assert diff is not None
        assert diff.after["title"] == "MB Title"
        assert diff.after["album"] == "Test Album"
        assert diff.after["release_mbid"] is None
        assert "release: no official release" in diff.notes
