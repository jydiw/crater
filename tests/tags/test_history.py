"""Tests for crater tags history (and the id-or-path track reference)."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from crater.tags.api import find_track, format_history, revert_batch, track_history
from crater.tags.errors import CraterError
from crater.tags.models import ApplyResult

# ---------------------------------------------------------------------------
# Track references
# ---------------------------------------------------------------------------

class TestFindTrack:
    def test_finds_by_id(
        self, db: sqlite3.Connection, track_id: int
    ) -> None:
        assert find_track(db, str(track_id))["id"] == track_id

    def test_finds_by_path(
        self, db: sqlite3.Connection, flac_with_tags: Path, track_id: int
    ) -> None:
        assert find_track(db, str(flac_with_tags))["id"] == track_id

    def test_finds_by_path_after_file_is_deleted(
        self, db: sqlite3.Connection, flac_with_tags: Path, track_id: int
    ) -> None:
        flac_with_tags.unlink()
        assert find_track(db, str(flac_with_tags))["id"] == track_id

    def test_unknown_reference_raises(self, db: sqlite3.Connection) -> None:
        with pytest.raises(CraterError):
            find_track(db, "999")


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestTrackHistory:
    def test_empty_for_untouched_track(
        self, db: sqlite3.Connection, track_id: int
    ) -> None:
        assert track_history(db, track_id) == []

    def test_lists_changes_chronologically(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title")
        apply_change(artist="MB Artist")

        entries = track_history(db, track_id)

        assert [e.field for e in entries] == ["title", "artist"]
        assert (entries[0].old_value, entries[0].new_value) == (
            "Test Track",
            "MB Title",
        )

    def test_includes_revert_entries(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        revert = revert_batch(db, applied.batch_id)

        entries = track_history(db, track_id)

        assert [e.source for e in entries] == ["csv-apply", "revert"]
        assert [e.batch_id for e in entries] == [applied.batch_id, revert.batch_id]

    def test_field_filter(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title", artist="MB Artist")

        entries = track_history(db, track_id, field="title")

        assert [e.field for e in entries] == ["title"]

    def test_batch_filter(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        first = apply_change(title="MB Title")
        apply_change(artist="MB Artist")

        entries = track_history(db, track_id, batch_id=first.batch_id)

        assert [e.field for e in entries] == ["title"]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class TestFormatHistory:
    def test_table_shows_the_change(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title")
        out = format_history(track_history(db, track_id))

        lines = out.splitlines()
        assert lines[0].startswith("when")
        assert "Test Track → MB Title" in lines[1]
        assert "csv-apply" in lines[1]

    def test_json_is_machine_readable(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        payload = json.loads(format_history(track_history(db, track_id), "json"))

        assert len(payload) == 1
        entry = payload[0]
        assert entry["field"] == "title"
        assert entry["old_value"] == "Test Track"
        assert entry["new_value"] == "MB Title"
        assert entry["source"] == "csv-apply"
        assert entry["batch_id"] == applied.batch_id
        assert isinstance(entry["changed_at"], int)

    def test_json_of_empty_history_is_an_empty_list(self) -> None:
        assert json.loads(format_history([], "json")) == []
