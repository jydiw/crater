"""Tests for crater.tags.merge — per-field source priority and call batching."""

from __future__ import annotations

from pathlib import Path

from crater.tags.merge import (
    DEFAULT_FIELD_SOURCES,
    MergeResult,
    MetadataSource,
    TrackContext,
    merge_proposals,
)
from crater.tags.models import TAGGABLE_FIELDS


class StubSource:
    """Canned values + call counters. Only answers the fields it's asked for."""

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


def _ctx(**before: str | None) -> TrackContext:
    return TrackContext(
        path=Path("/tmp/track.flac"),
        before={f: before.get(f) for f in TAGGABLE_FIELDS},
        duration_s=None,
        recording_mbid=None,
        release_mbid=None,
    )


def _merge(
    ctx: TrackContext,
    field_order: dict[str, tuple[str, ...]],
    *sources: StubSource,
    preserve_if_set: tuple[str, ...] = (),
) -> MergeResult:
    registry: dict[str, MetadataSource] = {s.name: s for s in sources}
    return merge_proposals(ctx, field_order, registry, preserve_if_set=preserve_if_set)


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


class TestPriority:
    def test_first_source_in_order_wins(self) -> None:
        first = StubSource("first", {"genre": "House"})
        second = StubSource("second", {"genre": "Techno"})
        result = _merge(_ctx(), {"genre": ("first", "second")}, first, second)
        assert result.after["genre"] == "House"
        assert result.winners["genre"] == "first"

    def test_none_falls_through_to_next_source(self) -> None:
        first = StubSource("first", {"genre": None})
        second = StubSource("second", {"genre": "Techno"})
        result = _merge(_ctx(), {"genre": ("first", "second")}, first, second)
        assert result.after["genre"] == "Techno"
        assert result.winners["genre"] == "second"

    def test_all_none_keeps_before(self) -> None:
        first = StubSource("first", {"genre": None})
        second = StubSource("second", {})
        result = _merge(
            _ctx(genre="Electronic"), {"genre": ("first", "second")}, first, second
        )
        assert result.after["genre"] == "Electronic"
        assert "genre" not in result.winners

    def test_unregistered_source_name_is_skipped(self) -> None:
        mb = StubSource("musicbrainz", {"genre": "House"})
        result = _merge(_ctx(), {"genre": ("deezer", "musicbrainz")}, mb)
        assert result.after["genre"] == "House"

    def test_empty_order_never_proposes(self) -> None:
        src = StubSource("first", {"key": "Am - 8A"})
        result = _merge(_ctx(key="Fm - 4A"), {"key": ()}, src)
        assert result.after["key"] == "Fm - 4A"
        assert src.calls == 0


# ---------------------------------------------------------------------------
# preserve_if_set
# ---------------------------------------------------------------------------


class TestPreserve:
    def test_preserved_field_consults_no_source(self) -> None:
        src = StubSource("first", {"bpm": "128"})
        result = _merge(
            _ctx(bpm="120"),
            {"bpm": ("first",)},
            src,
            preserve_if_set=("bpm",),
        )
        assert result.after["bpm"] == "120"
        assert src.calls == 0

    def test_preserved_field_still_filled_when_before_is_empty(self) -> None:
        src = StubSource("first", {"bpm": "128"})
        result = _merge(
            _ctx(), {"bpm": ("first",)}, src, preserve_if_set=("bpm",)
        )
        assert result.after["bpm"] == "128"

    def test_preserved_field_excluded_from_the_source_request(self) -> None:
        src = StubSource("first", {"bpm": "128", "genre": "House"})
        _merge(
            _ctx(bpm="120"),
            {"bpm": ("first",), "genre": ("first",)},
            src,
            preserve_if_set=("bpm",),
        )
        assert src.asked == [frozenset({"genre"})]


# ---------------------------------------------------------------------------
# Call batching
# ---------------------------------------------------------------------------


class TestCallBatching:
    def test_source_is_called_once_for_all_its_fields(self) -> None:
        src = StubSource(
            "first", {"title": "T", "artist": "A", "album": "AL"}
        )
        order = {f: ("first",) for f in ("title", "artist", "album")}
        result = _merge(_ctx(), order, src)
        assert src.calls == 1
        assert src.asked == [frozenset({"title", "artist", "album"})]
        assert result.after["album"] == "AL"

    def test_fields_already_won_are_not_requested_from_later_sources(self) -> None:
        first = StubSource("first", {"genre": "House"})
        second = StubSource("second", {"genre": "Techno", "label": "L"})
        _merge(
            _ctx(),
            {"genre": ("first", "second"), "label": ("second",)},
            first,
            second,
        )
        assert second.asked == [frozenset({"label"})]


# ---------------------------------------------------------------------------
# winners
# ---------------------------------------------------------------------------


class TestWinners:
    def test_unchanged_field_is_not_a_winner(self) -> None:
        src = StubSource("first", {"title": "Same", "artist": "New"})
        order = {"title": ("first",), "artist": ("first",)}
        result = _merge(_ctx(title="Same", artist="Old"), order, src)
        assert result.winners == {"artist": "first"}

    def test_untouched_fields_keep_their_before_values(self) -> None:
        src = StubSource("first", {"title": "New"})
        result = _merge(_ctx(title="Old", label="Mine"), {"title": ("first",)}, src)
        assert result.after["label"] == "Mine"
        assert result.after["title"] == "New"


# ---------------------------------------------------------------------------
# Default table
# ---------------------------------------------------------------------------


class TestDefaultFieldSources:
    def test_covers_every_taggable_field(self) -> None:
        assert set(DEFAULT_FIELD_SOURCES) == set(TAGGABLE_FIELDS)

    def test_track_no_and_key_are_never_proposed(self) -> None:
        assert DEFAULT_FIELD_SOURCES["track_no"] == ()
        assert DEFAULT_FIELD_SOURCES["key"] == ()

    def test_musicbrainz_alone_proposes_nothing_for_bpm(self) -> None:
        assert "musicbrainz" not in DEFAULT_FIELD_SOURCES["bpm"]
