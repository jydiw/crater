"""Tests for crater tags apply.

apply_diff consumes a review CSV where:
  before = what the file had when diff ran
  after  = what to write (MB proposal or user edit)

These tests construct TrackDiff objects directly (simulating MB proposals)
rather than calling generate_diff, since generate_diff returns no rows until
MB lookup is wired.
"""

import sqlite3
from hashlib import sha256
from pathlib import Path

import mutagen.flac
import pytest

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.api import apply_diff, generate_diff, write_diff_csv
from crater.tags.art import ArtBlob, embed_art, read_art
from crater.tags.io import read_tags
from crater.tags.models import TAGGABLE_FIELDS, ApplyResult, ArtChange, TrackDiff

EXISTING_JPEG = b"\xff\xd8\xff" + b"the file's own cover" * 2


def _register(db: sqlite3.Connection, path: Path) -> int:
    """Register a track and return its track_id."""
    generate_diff(db, [path])
    row = TrackRepo(db).get_by_path(path)
    assert row is not None
    return row["id"]


def _current_str_dict(path: Path) -> dict[str, str | None]:
    tags = read_tags(path)
    return {f: (str(getattr(tags, f)) if getattr(tags, f) is not None else None)
            for f in TAGGABLE_FIELDS}


def _mb_diff(
    track_id: int,
    path: Path,
    before: dict[str, str | None],
    **after_overrides: str | None,
) -> TrackDiff:
    """Build a TrackDiff simulating an MB proposal over the current file state."""
    after = dict(before)
    after.update(after_overrides)
    return TrackDiff(
        track_id=track_id,
        path=path,
        decision="accept",
        confidence="high",
        before=before,
        after=after,
    )


# ---------------------------------------------------------------------------
# Basic apply
# ---------------------------------------------------------------------------

class TestApplyMbProposal:
    def test_writes_mb_value_to_file(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv)

        assert result.applied == 1
        audio = mutagen.flac.FLAC(flac_with_tags)
        assert audio.tags is not None
        assert audio.tags["title"] == ["MB Title"]  # type: ignore[index]

    def test_updates_db_with_mb_value(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, album="MB Album")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        apply_diff(db, csv)

        row = TrackRepo(db).get_by_path(flac_with_tags)
        assert row is not None
        assert row["album"] == "MB Album"

    def test_inserts_change_history(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before,
                        title="MB Title", artist="MB Artist")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv)

        changes = ChangeRepo(db).get_by_track(track_id)
        assert len(changes) == result.applied
        assert {c["field"] for c in changes} == {"title", "artist"}

    def test_all_changes_share_batch_id(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before,
                        title="T", artist="A", album="B")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv)

        changes = ChangeRepo(db).get_by_track(track_id)
        batch_ids = {c["batch_id"] for c in changes}
        assert len(batch_ids) == 1
        assert result.batch_id in batch_ids

    def test_change_source_is_csv_apply(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        apply_diff(db, csv)

        changes = ChangeRepo(db).get_by_track(track_id)
        assert all(c["source"] == "csv-apply" for c in changes)

    def test_marks_track_reviewed(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        apply_diff(db, csv)

        row = TrackRepo(db).get_by_path(flac_with_tags)
        assert row is not None
        assert row["tags_reviewed_at"] is not None


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestApplyDryRun:
    def test_dry_run_does_not_write_file(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        apply_diff(db, csv, dry_run=True)

        audio = mutagen.flac.FLAC(flac_with_tags)
        assert audio.tags is not None
        assert audio.tags["title"] == ["Test Track"]  # type: ignore[index]

    def test_dry_run_does_not_update_db(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        apply_diff(db, csv, dry_run=True)

        row = TrackRepo(db).get_by_path(flac_with_tags)
        assert row is not None
        assert row["title"] == "Test Track"

    def test_dry_run_reports_count(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before,
                        title="MB Title", artist="MB Artist")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv, dry_run=True)

        assert result.applied == 2


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------

class TestApplyDrift:
    def test_drift_detected_when_file_changes_after_diff(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)

        # Externally modify the file between diff and apply
        audio = mutagen.flac.FLAC(flac_with_tags)
        audio.tags["title"] = ["Changed Externally"]  # type: ignore[index]
        audio.save()

        result = apply_diff(db, csv)
        drifted = {f for _, f in result.skipped_drift}
        assert "title" in drifted

    def test_force_overrides_drift(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)

        audio = mutagen.flac.FLAC(flac_with_tags)
        audio.tags["title"] = ["Changed Externally"]  # type: ignore[index]
        audio.save()

        result = apply_diff(db, csv, force=True)
        drifted = {f for _, f in result.skipped_drift}
        assert "title" not in drifted

    def test_idempotent_if_file_already_has_after_value(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        """If the file already equals 'after', apply syncs DB without writing the file."""
        track_id = _register(db, flac_with_tags)
        # before = "Test Track" (what file had at diff time)
        # after  = "MB Title"   (MB proposal)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        # Simulate: file was already updated to MB Title externally
        audio = mutagen.flac.FLAC(flac_with_tags)
        audio.tags["title"] = ["MB Title"]  # type: ignore[index]
        audio.save()

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv)

        # No drift (current == after), applied as DB-only sync
        assert result.applied == 1
        assert result.skipped_drift == []
        row = TrackRepo(db).get_by_path(flac_with_tags)
        assert row is not None
        assert row["title"] == "MB Title"


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

class TestApplyMissingFile:
    def test_missing_file_skipped(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)

        flac_with_tags.unlink()
        result = apply_diff(db, csv)

        assert track_id in result.skipped_missing
        # DB row still present
        assert TrackRepo(db).get_by_path(flac_with_tags) is not None


# ---------------------------------------------------------------------------
# Album art
# ---------------------------------------------------------------------------

class TestApplyArt:
    def _art_diff(
        self, track_id: int, path: Path, staged: Path, *, had_art: bool
    ) -> TrackDiff:
        """An art-only proposal: no text field moves."""
        diff = _mb_diff(track_id, path, _current_str_dict(path))
        diff.art = ArtChange(staged, had_art=had_art)
        return diff

    def _apply(
        self, db: sqlite3.Connection, diff: TrackDiff, tmp_path: Path, **kwargs: bool
    ) -> ApplyResult:
        csv = tmp_path / "art.csv"
        write_diff_csv([diff], csv)
        return apply_diff(db, csv, **kwargs)

    def test_embeds_the_staged_image(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=False)

        result = self._apply(db, diff, tmp_path)

        assert result.applied == 1
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert blob.data == staged_art.read_bytes()

    def test_records_a_change_row_with_no_old_value(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=False)

        self._apply(db, diff, tmp_path)

        changes = ChangeRepo(db).get_by_track(track_id)
        assert [c["field"] for c in changes] == ["album_art"]
        assert changes[0]["old_value"] is None
        assert changes[0]["new_value"] == str(staged_art)

    def test_replacement_stashes_the_old_image(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=True)

        result = self._apply(db, diff, tmp_path)

        change = ChangeRepo(db).get_by_track(track_id)[0]
        stash_dir = staged_art.parent / "stash"
        assert change["old_value"] == str(
            stash_dir / f"{track_id}-{result.batch_id}.jpg"
        )
        assert change["new_value"] == str(staged_art)
        stashed = Path(change["old_value"]).read_bytes()
        assert sha256(stashed).digest() == sha256(EXISTING_JPEG).digest()

    def test_presence_drift_is_skipped(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=False)
        # The file gained a cover between diff and apply.
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))

        result = self._apply(db, diff, tmp_path)

        assert result.skipped_drift == [(track_id, "album_art")]
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert blob.data == EXISTING_JPEG

    def test_force_overrides_presence_drift(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=False)
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))

        result = self._apply(db, diff, tmp_path, force=True)

        assert result.skipped_drift == []
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert blob.data == staged_art.read_bytes()

    def test_missing_staged_file_is_skipped_with_a_reason(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=False)
        staged_art.unlink()

        result = self._apply(db, diff, tmp_path)

        assert result.skipped_drift == [(track_id, "album_art (staged file missing)")]
        assert read_art(flac_with_tags) is None
        assert ChangeRepo(db).get_by_track(track_id) == []

    def test_dry_run_writes_nothing(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        tmp_path: Path,
    ) -> None:
        track_id = _register(db, flac_with_tags)
        diff = self._art_diff(track_id, flac_with_tags, staged_art, had_art=False)

        result = self._apply(db, diff, tmp_path, dry_run=True)

        assert result.applied == 1
        assert result.batch_id == 0
        assert read_art(flac_with_tags) is None
        assert BatchRepo(db).get_by_id(1) is None
        assert ChangeRepo(db).get_by_track(track_id) == []


# ---------------------------------------------------------------------------
# Decision handling
# ---------------------------------------------------------------------------

class TestApplyDecision:
    def test_rejected_track_not_applied(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")
        diff.decision = "reject"

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv)

        assert result.applied == 0
        assert result.skipped_review == 1

    def test_review_track_not_applied(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        track_id = _register(db, flac_with_tags)
        before = _current_str_dict(flac_with_tags)
        diff = _mb_diff(track_id, flac_with_tags, before, title="MB Title")
        diff.decision = "review"

        csv = tmp_path / "diff.csv"
        write_diff_csv([diff], csv)
        result = apply_diff(db, csv)

        assert result.applied == 0
