"""Tests for crater.tags.art — sniffing, embed/read/strip, multi-source staging.

The image payloads are not real images: nothing in crater decodes them, so a
valid magic prefix plus distinguishable bytes is enough to prove that the exact
bytes staged are the exact bytes embedded, stashed and restored.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from crater.tags.art import (
    CAA_RELEASE,
    ArtBlob,
    ArtStager,
    CaaArtSource,
    StagedArt,
    embed_art,
    fetch_bytes,
    read_art,
    sniff_mime,
    strip_art,
)
from crater.tags.enrich import USER_AGENT, ArtCandidate, ArtSource
from crater.tags.merge import TrackContext
from crater.tags.models import TAGGABLE_FIELDS
from crater.tags.ratelimit import RateLimiter

PNG_A = b"\x89PNG\r\n\x1a\n" + b"payload A" * 4
PNG_B = b"\x89PNG\r\n\x1a\n" + b"payload B" * 4
JPEG_A = b"\xff\xd8\xff" + b"payload J" * 4

FRONT_500 = f"{CAA_RELEASE}/rel-abc/front-500"
FRONT = f"{CAA_RELEASE}/rel-abc/front"
DEEZER_COVER = "https://cdn.deezer.test/album/123/cover_xl.jpg"
ITUNES_COVER = "https://is1.itunes.test/456/1200x1200bb.jpg"


class FakeFetch:
    """Canned image bytes by URL + a call log. Unknown URL = a failed request."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, str, float]] = []

    def __call__(
        self, url: str, *, limiter: RateLimiter, bucket: str, rps: float
    ) -> bytes | None:
        self.calls.append((url, bucket, rps))
        return self.payloads.get(url)

    @property
    def urls(self) -> list[str]:
        return [url for url, _, _ in self.calls]


class StubArtSource:
    """One canned candidate + a call counter, so "the later sources were never
    consulted" is an assertion rather than a hope."""

    def __init__(self, name: str, candidate: ArtCandidate | None) -> None:
        self.name = name
        self.candidate = candidate
        self.calls = 0

    def find(self, ctx: TrackContext) -> ArtCandidate | None:
        self.calls += 1
        return self.candidate


def _ctx(release_mbid: str | None = "rel-abc") -> TrackContext:
    return TrackContext(
        path=Path("/tmp/track.flac"),
        before=dict.fromkeys(TAGGABLE_FIELDS),
        duration_s=200.0,
        recording_mbid="rec-abc",
        release_mbid=release_mbid,
    )


def _stager(
    art_dir: Path, payloads: dict[str, bytes], sources: list[ArtSource] | None = None
) -> tuple[ArtStager, FakeFetch]:
    fetch = FakeFetch(payloads)
    stager = ArtStager(
        art_dir,
        RateLimiter(),
        [CaaArtSource()] if sources is None else sources,
        fetch,
    )
    return stager, fetch


# ---------------------------------------------------------------------------
# sniff_mime
# ---------------------------------------------------------------------------


class TestSniffMime:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (PNG_A, "image/png"),
            (JPEG_A, "image/jpeg"),
            (b"GIF89a not supported", None),
            (b"", None),
        ],
    )
    def test_magic_bytes_decide(self, data: bytes, expected: str | None) -> None:
        assert sniff_mime(data) == expected


# ---------------------------------------------------------------------------
# embed / read / strip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.fixture(params=["flac", "mp3"])
    def audio_file(
        self, request: pytest.FixtureRequest, flac_with_tags: Path, mp3_with_tags: Path
    ) -> Path:
        return flac_with_tags if request.param == "flac" else mp3_with_tags

    def test_no_art_reads_as_none(self, audio_file: Path) -> None:
        assert read_art(audio_file) is None

    def test_embed_then_read_returns_the_exact_bytes(self, audio_file: Path) -> None:
        embed_art(audio_file, ArtBlob(PNG_A, "image/png"))

        blob = read_art(audio_file)
        assert blob is not None
        assert blob.data == PNG_A
        assert blob.mime == "image/png"

    def test_embed_replaces_rather_than_appends(self, audio_file: Path) -> None:
        embed_art(audio_file, ArtBlob(PNG_A, "image/png"))
        embed_art(audio_file, ArtBlob(JPEG_A, "image/jpeg"))

        blob = read_art(audio_file)
        assert blob is not None
        assert blob.data == JPEG_A
        assert blob.mime == "image/jpeg"

    def test_strip_removes_the_picture(self, audio_file: Path) -> None:
        embed_art(audio_file, ArtBlob(PNG_A, "image/png"))
        strip_art(audio_file)

        assert read_art(audio_file) is None

    def test_declared_mime_does_not_override_the_bytes(self, audio_file: Path) -> None:
        embed_art(audio_file, ArtBlob(JPEG_A, "image/png"))

        blob = read_art(audio_file)
        assert blob is not None
        assert blob.mime == "image/jpeg"


# ---------------------------------------------------------------------------
# fetch_bytes
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


class TestFetchBytes:
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
            return _FakeResponse(PNG_A)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        data = fetch_bytes(
            FRONT_500,
            limiter=Limiter(),  # type: ignore[arg-type]
            bucket="caa",
            rps=1.0,
        )

        assert data == PNG_A
        assert events == ["acquire caa 1.0", "request"]
        assert seen[0].get_header("User-agent") == USER_AGENT

    def test_any_failure_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(request: Any, timeout: float) -> _FakeResponse:
            raise urllib.error.URLError("no route")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert (
            fetch_bytes(FRONT_500, limiter=RateLimiter(), bucket="caa", rps=1.0) is None
        )


# ---------------------------------------------------------------------------
# ArtStager
# ---------------------------------------------------------------------------


class TestCaaArtSource:
    def test_candidate_is_the_500px_front_under_the_bare_mbid(self) -> None:
        assert CaaArtSource().find(_ctx()) == ArtCandidate(
            "coverartarchive", "rel-abc", FRONT_500
        )

    def test_no_release_mbid_means_no_candidate(self, tmp_path: Path) -> None:
        stager, fetch = _stager(tmp_path / "art", {FRONT_500: PNG_A})

        assert stager.stage(_ctx(release_mbid=None)) is None
        assert fetch.calls == []


class TestArtStager:
    def test_front_500_is_written_under_its_mbid(self, tmp_path: Path) -> None:
        stager, fetch = _stager(tmp_path / "art", {FRONT_500: PNG_A})

        staged = stager.stage(_ctx())

        assert staged == StagedArt(tmp_path / "art" / "rel-abc.png", "coverartarchive")
        assert staged.path.read_bytes() == PNG_A
        assert [(b, r) for _, b, r in fetch.calls] == [("caa", 1.0)]

    def test_jpeg_gets_a_jpg_extension(self, tmp_path: Path) -> None:
        stager, _ = _stager(tmp_path / "art", {FRONT_500: JPEG_A})
        assert stager.stage(_ctx()) == StagedArt(
            tmp_path / "art" / "rel-abc.jpg", "coverartarchive"
        )

    def test_already_on_disk_is_never_fetched(self, tmp_path: Path) -> None:
        art_dir = tmp_path / "art"
        art_dir.mkdir()
        (art_dir / "rel-abc.png").write_bytes(PNG_A)
        stager, fetch = _stager(art_dir, {FRONT_500: PNG_B})

        assert stager.stage(_ctx()) == StagedArt(
            art_dir / "rel-abc.png", "coverartarchive"
        )
        assert fetch.calls == []
        assert (art_dir / "rel-abc.png").read_bytes() == PNG_A

    def test_falls_back_to_front_when_front_500_is_missing(
        self, tmp_path: Path
    ) -> None:
        stager, fetch = _stager(tmp_path / "art", {FRONT: PNG_B})

        staged = stager.stage(_ctx())

        assert staged is not None
        assert staged.path.read_bytes() == PNG_B
        assert fetch.urls == [FRONT_500, FRONT]

    def test_no_art_at_either_url(self, tmp_path: Path) -> None:
        stager, fetch = _stager(tmp_path / "art", {})

        assert stager.stage(_ctx()) is None
        assert fetch.urls == [FRONT_500, FRONT]
        assert not (tmp_path / "art").exists()

    def test_unrecognized_format_is_not_staged(self, tmp_path: Path) -> None:
        stager, _ = _stager(tmp_path / "art", {FRONT_500: b"GIF89a nope"})

        assert stager.stage(_ctx()) is None
        assert not (tmp_path / "art").exists()


# ---------------------------------------------------------------------------
# ArtStager — the source chain
# ---------------------------------------------------------------------------


CAA_CANDIDATE = ArtCandidate("coverartarchive", "rel-abc", FRONT_500)
DEEZER_CANDIDATE = ArtCandidate("deezer", "deezer-123", DEEZER_COVER)
ITUNES_CANDIDATE = ArtCandidate("itunes", "itunes-456", ITUNES_COVER)


def _chain(
    art_dir: Path,
    payloads: dict[str, bytes],
    *candidates: ArtCandidate | None,
) -> tuple[ArtStager, FakeFetch, list[StubArtSource]]:
    sources = [
        StubArtSource(name, candidate)
        for name, candidate in zip(
            ("coverartarchive", "deezer", "itunes"), candidates, strict=True
        )
    ]
    stager, fetch = _stager(art_dir, payloads, list(sources))
    return stager, fetch, sources


class TestSourceChain:
    def test_sources_with_no_candidate_are_skipped(self, tmp_path: Path) -> None:
        stager, fetch, sources = _chain(tmp_path / "art", {}, None, None, None)

        assert stager.stage(_ctx()) is None
        assert [s.calls for s in sources] == [1, 1, 1]
        assert fetch.calls == []

    def test_a_failed_fetch_falls_through_to_the_next_source(
        self, tmp_path: Path
    ) -> None:
        stager, fetch, sources = _chain(
            tmp_path / "art",
            {ITUNES_COVER: PNG_B},
            CAA_CANDIDATE,
            DEEZER_CANDIDATE,
            ITUNES_CANDIDATE,
        )

        staged = stager.stage(_ctx())

        assert staged == StagedArt(tmp_path / "art" / "itunes-456.png", "itunes")
        assert staged.path.read_bytes() == PNG_B
        assert [s.calls for s in sources] == [1, 1, 1]
        assert fetch.urls == [FRONT_500, FRONT, DEEZER_COVER, ITUNES_COVER]

    def test_the_first_source_with_bytes_wins(self, tmp_path: Path) -> None:
        stager, fetch, sources = _chain(
            tmp_path / "art",
            {DEEZER_COVER: PNG_A, ITUNES_COVER: PNG_B},
            None,
            DEEZER_CANDIDATE,
            ITUNES_CANDIDATE,
        )

        staged = stager.stage(_ctx())

        assert staged == StagedArt(tmp_path / "art" / "deezer-123.png", "deezer")
        assert staged.path.read_bytes() == PNG_A
        assert [s.calls for s in sources] == [1, 1, 0]
        assert fetch.urls == [DEEZER_COVER]

    def test_a_key_already_on_disk_short_circuits_its_source(
        self, tmp_path: Path
    ) -> None:
        art_dir = tmp_path / "art"
        art_dir.mkdir()
        (art_dir / "deezer-123.jpg").write_bytes(JPEG_A)
        stager, fetch, sources = _chain(
            art_dir,
            {DEEZER_COVER: PNG_A},
            None,
            DEEZER_CANDIDATE,
            ITUNES_CANDIDATE,
        )

        assert stager.stage(_ctx()) == StagedArt(art_dir / "deezer-123.jpg", "deezer")
        assert fetch.calls == []
        assert [s.calls for s in sources] == [1, 1, 0]

    def test_unsniffable_bytes_fall_through_and_write_nothing(
        self, tmp_path: Path
    ) -> None:
        art_dir = tmp_path / "art"
        stager, _, _ = _chain(
            art_dir,
            {DEEZER_COVER: b"<html>404</html>", ITUNES_COVER: PNG_B},
            None,
            DEEZER_CANDIDATE,
            ITUNES_CANDIDATE,
        )

        assert stager.stage(_ctx()) == StagedArt(art_dir / "itunes-456.png", "itunes")
        assert not (art_dir / "deezer-123.png").exists()
        assert not (art_dir / "deezer-123.jpg").exists()

    def test_each_source_gets_its_own_bucket_and_rate(self, tmp_path: Path) -> None:
        stager, fetch, _ = _chain(
            tmp_path / "art",
            {ITUNES_COVER: PNG_B},
            CAA_CANDIDATE,
            DEEZER_CANDIDATE,
            ITUNES_CANDIDATE,
        )

        stager.stage(_ctx())

        assert [(b, r) for _, b, r in fetch.calls] == [
            ("caa", 1.0),
            ("caa", 1.0),
            ("deezer", 10.0),
            ("itunes", 0.3),
        ]
