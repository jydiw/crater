"""Tests for crater.tags.enrich — Deezer, iTunes and MB-genre adapters.

No network: `FakeFetch` replays canned payloads keyed by URL and records the
(url, bucket, rps) of every request, so laziness and rate-limit buckets are
assertions rather than hopes.
"""

from __future__ import annotations

import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from crater.db.repo import SourceCacheRepo
from crater.tags.enrich import (
    USER_AGENT,
    ArtCandidate,
    DeezerArtSource,
    DeezerSource,
    GuardMode,
    ITunesArtSource,
    ITunesSource,
    MbGenreFetcher,
    MbSource,
    _guard_ok,
    get_json,
)
from crater.tags.merge import StaticSource, TrackContext
from crater.tags.models import TAGGABLE_FIELDS
from crater.tags.ratelimit import RateLimiter

DEEZER_SEARCH = (
    "https://api.deezer.com/search"
    "?q=artist%3A%22Test+Artist%22+track%3A%22Test+Track%22"
)
DEEZER_TRACK = "https://api.deezer.com/track/77"
DEEZER_ALBUM = "https://api.deezer.com/album/99"
ITUNES_SEARCH = (
    "https://itunes.apple.com/search?term=Test+Artist+Test+Track&entity=song&limit=10"
)
MB_GENRES = "https://musicbrainz.org/ws/2/recording/rec-abc?inc=genres&fmt=json"


class FakeFetch:
    """Canned payloads by URL + a call log. Unknown URL = a failed request."""

    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str, float]] = []

    def __call__(
        self, url: str, *, limiter: RateLimiter, bucket: str, rps: float
    ) -> object | None:
        self.calls.append((url, bucket, rps))
        return self.payloads.get(url)

    @property
    def urls(self) -> list[str]:
        return [url for url, _, _ in self.calls]


def _ctx(duration_s: float | None = 200.0, **before: str | None) -> TrackContext:
    values: dict[str, str | None] = {
        "artist": "Test Artist",
        "title": "Test Track",
    } | before
    return TrackContext(
        path=Path("/tmp/track.flac"),
        before={f: values.get(f) for f in TAGGABLE_FIELDS},
        duration_s=duration_s,
        recording_mbid="rec-abc",
        release_mbid=None,
    )


def _deezer_hit(duration: int = 202, **over: Any) -> dict[str, Any]:
    return {
        "id": 77,
        "title": "Test Track",
        "artist": {"name": "Test Artist"},
        "album": {"id": 99},
        "duration": duration,
    } | over


def _deezer(
    db: sqlite3.Connection, payloads: dict[str, object]
) -> tuple[DeezerSource, FakeFetch]:
    fetch = FakeFetch(payloads)
    return DeezerSource(SourceCacheRepo(db), RateLimiter(), fetch), fetch


def _itunes(
    db: sqlite3.Connection, payloads: dict[str, object]
) -> tuple[ITunesSource, FakeFetch]:
    fetch = FakeFetch(payloads)
    return ITunesSource(SourceCacheRepo(db), RateLimiter(), fetch), fetch


def _mb(
    db: sqlite3.Connection,
    payloads: dict[str, object],
    static: dict[str, str | None] | None = None,
) -> tuple[MbSource, FakeFetch]:
    fetch = FakeFetch(payloads)
    fetcher = MbGenreFetcher(SourceCacheRepo(db), RateLimiter(), fetch)
    return MbSource(StaticSource("musicbrainz", static or {}), fetcher), fetch


# ---------------------------------------------------------------------------
# get_json
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class TestGetJson:
    def test_rate_limits_before_requesting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        seen: list[urllib.request.Request] = []

        class Limiter:
            def acquire(self, name: str, rps: float) -> None:
                events.append(f"acquire {name} {rps}")

        def fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
            events.append("request")
            seen.append(request)
            return _FakeResponse(b'{"ok": true}')

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = get_json(
            "https://example.test/x",
            limiter=Limiter(),  # type: ignore[arg-type]
            bucket="deezer",
            rps=10.0,
        )

        assert result == {"ok": True}
        assert events == ["acquire deezer 10.0", "request"]
        assert seen[0].get_header("User-agent") == USER_AGENT

    def test_any_failure_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(request: Any, timeout: float) -> _FakeResponse:
            raise urllib.error.URLError("no route")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert (
            get_json(
                "https://example.test/x",
                limiter=RateLimiter(),
                bucket="deezer",
                rps=10.0,
            )
            is None
        )


# ---------------------------------------------------------------------------
# The shared candidate guard
# ---------------------------------------------------------------------------


def _guard(ctx: TrackContext, hit: dict[str, Any], mode: GuardMode = "strict") -> bool:
    return _guard_ok(
        ctx, hit["artist"]["name"], hit["title"], hit["duration"], mode=mode
    )


class TestGuardModes:
    def test_art_mode_forgives_a_duration_gap_strict_does_not(self) -> None:
        ctx, hit = _ctx(), _deezer_hit(duration=408)
        assert _guard(ctx, hit) is False
        assert _guard(ctx, hit, mode="art") is True

    @pytest.mark.parametrize("mode", ["strict", "art"])
    def test_a_partial_artist_credit_passes(self, mode: GuardMode) -> None:
        ctx = _ctx(artist="Jerome Isma-Ae, JES")
        hit = _deezer_hit(artist={"name": "Jerome Isma-Ae"})
        assert _guard(ctx, hit, mode=mode) is True

    @pytest.mark.parametrize("mode", ["strict", "art"])
    def test_a_remix_candidate_never_passes(self, mode: GuardMode) -> None:
        hit = _deezer_hit(title="Test Track (Argy Remix)")
        assert _guard(_ctx(), hit, mode=mode) is False


# ---------------------------------------------------------------------------
# Deezer
# ---------------------------------------------------------------------------


class TestDeezerGuard:
    def test_close_duration_passes(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 128.0},
            },
        )
        assert source.propose(_ctx(), frozenset({"bpm"})) == {"bpm": "128"}

    def test_far_duration_stops_before_the_track_call(
        self, db: sqlite3.Connection
    ) -> None:
        source, fetch = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit(duration=210)]},
                DEEZER_TRACK: {"bpm": 128.0},
            },
        )
        assert source.propose(_ctx(), frozenset({"bpm"})) == {}
        assert fetch.urls == [DEEZER_SEARCH]

    def test_network_failure_proposes_nothing(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer(db, {})
        assert source.propose(_ctx(), frozenset({"bpm", "genre"})) == {}


class TestDeezerBpm:
    def test_zero_bpm_is_not_proposed(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 0.0},
            },
        )
        assert source.propose(_ctx(), frozenset({"bpm"})) == {}

    def test_fractional_bpm_rounds_and_notes(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 126.02},
            },
        )
        ctx = _ctx()
        assert source.propose(ctx, frozenset({"bpm"})) == {"bpm": "126"}
        assert ctx.notes == ["deezer bpm 126.02"]

    def test_integral_bpm_has_no_note(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 128.0},
            },
        )
        ctx = _ctx()
        source.propose(ctx, frozenset({"bpm"}))
        assert ctx.notes == []


class TestDeezerLaziness:
    def test_bpm_not_requested_skips_the_track_call(
        self, db: sqlite3.Connection
    ) -> None:
        source, fetch = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_ALBUM: {"genres": {"data": [{"name": "House"}]}},
            },
        )
        assert source.propose(_ctx(), frozenset({"genre"})) == {"genre": "House"}
        assert DEEZER_TRACK not in fetch.urls

    def test_genre_not_requested_skips_the_album_call(
        self, db: sqlite3.Connection
    ) -> None:
        source, fetch = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 128.0},
            },
        )
        source.propose(_ctx(), frozenset({"bpm"}))
        assert DEEZER_ALBUM not in fetch.urls

    def test_no_requested_field_makes_no_request(self, db: sqlite3.Connection) -> None:
        source, fetch = _deezer(db, {DEEZER_SEARCH: {"data": [_deezer_hit()]}})
        assert source.propose(_ctx(), frozenset({"title"})) == {}
        assert fetch.calls == []


class TestDeezerCache:
    def test_second_propose_makes_no_new_requests(self, db: sqlite3.Connection) -> None:
        source, fetch = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 128.0},
            },
        )
        source.propose(_ctx(), frozenset({"bpm"}))
        before = len(fetch.calls)

        assert source.propose(_ctx(), frozenset({"bpm"})) == {"bpm": "128"}
        assert len(fetch.calls) == before

    def test_requests_use_the_deezer_bucket(self, db: sqlite3.Connection) -> None:
        source, fetch = _deezer(
            db,
            {
                DEEZER_SEARCH: {"data": [_deezer_hit()]},
                DEEZER_TRACK: {"bpm": 128.0},
            },
        )
        source.propose(_ctx(), frozenset({"bpm"}))
        assert [(b, r) for _, b, r in fetch.calls] == [("deezer", 10.0)] * 2


# ---------------------------------------------------------------------------
# iTunes
# ---------------------------------------------------------------------------


def _itunes_result(**over: Any) -> dict[str, Any]:
    return {
        "artistName": "Test Artist",
        "trackName": "Test Track",
        "trackTimeMillis": 201000,
        "primaryGenreName": "Dance",
    } | over


class TestItunes:
    def test_genre_from_the_first_guard_passing_result(
        self, db: sqlite3.Connection
    ) -> None:
        source, fetch = _itunes(db, {ITUNES_SEARCH: {"results": [_itunes_result()]}})
        assert source.propose(_ctx(), frozenset({"genre"})) == {"genre": "Dance"}
        assert [(b, r) for _, b, r in fetch.calls] == [("itunes", 0.3)]

    def test_duration_mismatch_proposes_nothing(self, db: sqlite3.Connection) -> None:
        source, fetch = _itunes(
            db,
            {ITUNES_SEARCH: {"results": [_itunes_result(trackTimeMillis=210000)]}},
        )
        assert source.propose(_ctx(), frozenset({"genre"})) == {}
        assert len(fetch.calls) == 1

    def test_genre_not_requested_makes_no_request(self, db: sqlite3.Connection) -> None:
        source, fetch = _itunes(db, {ITUNES_SEARCH: {"results": [_itunes_result()]}})
        assert source.propose(_ctx(), frozenset({"bpm"})) == {}
        assert fetch.calls == []


# ---------------------------------------------------------------------------
# Album art sources
# ---------------------------------------------------------------------------

DEEZER_XL = "https://cdn.deezer.test/99/1000x1000.jpg"
DEEZER_BIG = "https://cdn.deezer.test/99/500x500.jpg"
ITUNES_THUMB = "https://is1.itunes.test/55/100x100bb.jpg"


def _deezer_art(**album: Any) -> dict[str, Any]:
    return _deezer_hit(album={"id": 99, "cover_xl": DEEZER_XL} | album)


def _deezer_art_source(
    db: sqlite3.Connection, payloads: dict[str, object]
) -> tuple[DeezerArtSource, FakeFetch]:
    fetch = FakeFetch(payloads)
    return DeezerArtSource(SourceCacheRepo(db), RateLimiter(), fetch), fetch


def _itunes_art_source(
    db: sqlite3.Connection, payloads: dict[str, object]
) -> tuple[ITunesArtSource, FakeFetch]:
    fetch = FakeFetch(payloads)
    return ITunesArtSource(SourceCacheRepo(db), RateLimiter(), fetch), fetch


class TestDeezerArt:
    def test_cover_xl_becomes_the_candidate(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer_art_source(db, {DEEZER_SEARCH: {"data": [_deezer_art()]}})
        assert source.find(_ctx()) == ArtCandidate("deezer", "deezer-99", DEEZER_XL)

    def test_cover_big_is_used_when_xl_is_absent(self, db: sqlite3.Connection) -> None:
        hit = _deezer_art()
        del hit["album"]["cover_xl"]
        hit["album"]["cover_big"] = DEEZER_BIG
        source, _ = _deezer_art_source(db, {DEEZER_SEARCH: {"data": [hit]}})

        assert source.find(_ctx()) == ArtCandidate("deezer", "deezer-99", DEEZER_BIG)

    def test_a_duration_gap_still_yields_a_candidate(
        self, db: sqlite3.Connection
    ) -> None:
        """An extended mix ships with the radio edit's artwork."""
        source, _ = _deezer_art_source(
            db, {DEEZER_SEARCH: {"data": [_deezer_art(duration=408)]}}
        )
        assert source.find(_ctx()) == ArtCandidate("deezer", "deezer-99", DEEZER_XL)

    def test_no_cover_url_means_no_candidate(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer_art_source(
            db, {DEEZER_SEARCH: {"data": [_deezer_hit(album={"id": 99})]}}
        )
        assert source.find(_ctx()) is None

    def test_no_album_id_means_no_candidate(self, db: sqlite3.Connection) -> None:
        source, _ = _deezer_art_source(
            db, {DEEZER_SEARCH: {"data": [_deezer_hit(album={"cover_xl": DEEZER_XL})]}}
        )
        assert source.find(_ctx()) is None

    def test_the_text_search_cache_is_reused(self, db: sqlite3.Connection) -> None:
        cache = SourceCacheRepo(db)
        fetch = FakeFetch(
            {DEEZER_SEARCH: {"data": [_deezer_art()]}, DEEZER_TRACK: {"bpm": 128.0}}
        )
        DeezerSource(cache, RateLimiter(), fetch).propose(_ctx(), frozenset({"bpm"}))
        before = len(fetch.calls)

        art = DeezerArtSource(cache, RateLimiter(), fetch).find(_ctx())

        assert art == ArtCandidate("deezer", "deezer-99", DEEZER_XL)
        assert len(fetch.calls) == before


class TestItunesArt:
    def test_the_thumbnail_url_is_upscaled(self, db: sqlite3.Connection) -> None:
        source, _ = _itunes_art_source(
            db,
            {
                ITUNES_SEARCH: {
                    "results": [
                        _itunes_result(collectionId=55, artworkUrl100=ITUNES_THUMB)
                    ]
                }
            },
        )
        assert source.find(_ctx()) == ArtCandidate(
            "itunes", "itunes-55", "https://is1.itunes.test/55/1200x1200bb.jpg"
        )

    def test_a_duration_gap_still_yields_a_candidate(
        self, db: sqlite3.Connection
    ) -> None:
        source, _ = _itunes_art_source(
            db,
            {
                ITUNES_SEARCH: {
                    "results": [
                        _itunes_result(
                            collectionId=55,
                            artworkUrl100=ITUNES_THUMB,
                            trackTimeMillis=409000,
                        )
                    ]
                }
            },
        )
        assert source.find(_ctx()) is not None

    @pytest.mark.parametrize(
        "over",
        [
            {"artworkUrl100": ITUNES_THUMB},
            {"collectionId": 55},
        ],
    )
    def test_a_missing_half_means_no_candidate(
        self, db: sqlite3.Connection, over: dict[str, Any]
    ) -> None:
        source, _ = _itunes_art_source(
            db, {ITUNES_SEARCH: {"results": [_itunes_result(**over)]}}
        )
        assert source.find(_ctx()) is None

    def test_the_text_search_cache_is_reused(self, db: sqlite3.Connection) -> None:
        cache = SourceCacheRepo(db)
        result = _itunes_result(collectionId=55, artworkUrl100=ITUNES_THUMB)
        fetch = FakeFetch({ITUNES_SEARCH: {"results": [result]}})
        ITunesSource(cache, RateLimiter(), fetch).propose(_ctx(), frozenset({"genre"}))
        before = len(fetch.calls)

        assert ITunesArtSource(cache, RateLimiter(), fetch).find(_ctx()) is not None
        assert len(fetch.calls) == before


# ---------------------------------------------------------------------------
# MusicBrainz genres
# ---------------------------------------------------------------------------


class TestMbGenres:
    def test_highest_count_wins(self, db: sqlite3.Connection) -> None:
        source, fetch = _mb(
            db,
            {
                MB_GENRES: {
                    "genres": [
                        {"name": "house", "count": 2},
                        {"name": "techno", "count": 7},
                    ]
                }
            },
        )
        assert source.propose(_ctx(), frozenset({"genre"})) == {"genre": "techno"}
        assert [(b, r) for _, b, r in fetch.calls] == [("musicbrainz", 1.0)]

    def test_tie_keeps_the_first_listed(self, db: sqlite3.Connection) -> None:
        source, _ = _mb(
            db,
            {
                MB_GENRES: {
                    "genres": [
                        {"name": "house", "count": 3},
                        {"name": "techno", "count": 3},
                    ]
                }
            },
        )
        assert source.propose(_ctx(), frozenset({"genre"})) == {"genre": "house"}

    def test_empty_genres_propose_nothing(self, db: sqlite3.Connection) -> None:
        source, _ = _mb(db, {MB_GENRES: {"genres": []}})
        assert source.propose(_ctx(), frozenset({"genre"})) == {}

    def test_static_genre_is_not_looked_up(self, db: sqlite3.Connection) -> None:
        source, fetch = _mb(
            db,
            {MB_GENRES: {"genres": [{"name": "techno", "count": 7}]}},
            static={"genre": "House"},
        )
        assert source.propose(_ctx(), frozenset({"genre"})) == {"genre": "House"}
        assert fetch.calls == []

    def test_genre_not_requested_makes_no_request(self, db: sqlite3.Connection) -> None:
        source, fetch = _mb(
            db,
            {MB_GENRES: {"genres": [{"name": "techno", "count": 7}]}},
            static={"title": "MB Title"},
        )
        assert source.propose(_ctx(), frozenset({"title"})) == {"title": "MB Title"}
        assert fetch.calls == []

    def test_second_propose_makes_no_new_requests(self, db: sqlite3.Connection) -> None:
        source, fetch = _mb(
            db, {MB_GENRES: {"genres": [{"name": "techno", "count": 7}]}}
        )
        source.propose(_ctx(), frozenset({"genre"}))
        assert source.propose(_ctx(), frozenset({"genre"})) == {"genre": "techno"}
        assert len(fetch.calls) == 1
