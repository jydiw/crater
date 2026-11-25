"""Tests for crater tags forget / prune."""

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.api import (
    forget_track,
    generate_diff,
    list_missing,
    prune_missing,
)
from crater.tags.models import ApplyResult


def _second_track(
    db: sqlite3.Connection, source: Path, tmp_path: Path
) -> tuple[int, Path]:
    """Register a second track, returning (track_id, path)."""
    path = tmp_path / "second.flac"
    shutil.copy(source, path)
    generate_diff(db, [path])
    row = TrackRepo(db).get_by_path(path.resolve())
    assert row is not None
    return int(row["id"]), path


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------

class TestForget:
    def test_deletes_the_track_row(
        self, db: sqlite3.Connection, track_id: int
    ) -> None:
        assert forget_track(db, track_id) is True
        assert TrackRepo(db).get_by_id(track_id) is None

    def test_cascades_history(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title")
        assert ChangeRepo(db).get_by_track(track_id) != []

        forget_track(db, track_id)

        assert ChangeRepo(db).get_by_track(track_id) == []

    def test_batches_survive(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        forget_track(db, track_id)

        assert BatchRepo(db).get_by_id(applied.batch_id) is not None

    def test_leaves_the_file_on_disk(
        self, db: sqlite3.Connection, flac_with_tags: Path, track_id: int
    ) -> None:
        forget_track(db, track_id)

        assert flac_with_tags.exists()

    def test_unknown_id_returns_false(self, db: sqlite3.Connection) -> None:
        assert forget_track(db, 999) is False


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

class TestPrune:
    def test_lists_only_tracks_whose_file_is_gone(
        self, db: sqlite3.Connection, flac_with_tags: Path, track_id: int
    ) -> None:
        assert list_missing(db) == []

        flac_with_tags.unlink()

        assert [row["id"] for row in list_missing(db)] == [track_id]

    def test_dry_run_deletes_nothing(
        self, db: sqlite3.Connection, flac_with_tags: Path, track_id: int
    ) -> None:
        flac_with_tags.unlink()

        pruned = prune_missing(db, dry_run=True)

        assert [row["id"] for row in pruned] == [track_id]
        assert TrackRepo(db).get_by_id(track_id) is not None

    def test_deletes_only_missing_file_tracks(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        tmp_path: Path,
    ) -> None:
        kept_id, kept_path = _second_track(db, flac_with_tags, tmp_path)
        flac_with_tags.unlink()

        pruned = prune_missing(db)

        assert [row["id"] for row in pruned] == [track_id]
        assert TrackRepo(db).get_by_id(track_id) is None
        assert TrackRepo(db).get_by_id(kept_id) is not None
        assert kept_path.exists()

    def test_cascades_history(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title")
        flac_with_tags.unlink()

        prune_missing(db)

        assert ChangeRepo(db).get_by_track(track_id) == []
