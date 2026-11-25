"""Tests for crater tags revert.

Each test applies a real batch first (see tests/tags/conftest.py), then
reverts it. "drift" means the file tag moved on since the apply.
"""

import sqlite3
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

import mutagen.flac
import mutagen.mp3
import pytest

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.api import apply_diff, generate_diff, revert_batch, write_diff_csv
from crater.tags.art import ArtBlob, embed_art, read_art
from crater.tags.errors import CraterError
from crater.tags.io import read_tags
from crater.tags.models import TAGGABLE_FIELDS, ApplyResult, ArtChange, TrackDiff

EXISTING_JPEG = b"\xff\xd8\xff" + b"the file's own cover" * 2
FOREIGN_PNG = b"\x89PNG\r\n\x1a\n" + b"someone else's cover" * 2


def _file_title(path: Path) -> str:
    audio = mutagen.flac.FLAC(path)
    assert audio.tags is not None
    return audio.tags["title"][0]  # type: ignore[index,no-any-return]


def _set_file_title(path: Path, title: str) -> None:
    audio = mutagen.flac.FLAC(path)
    audio.tags["title"] = [title]  # type: ignore[index]
    audio.save()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

class TestRevertRoundTrip:
    def test_restores_file_tag(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        assert _file_title(flac_with_tags) == "MB Title"

        result = revert_batch(db, applied.batch_id)

        assert result.reverted == 1
        assert _file_title(flac_with_tags) == "Test Track"

    def test_restores_db_value(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(album="MB Album")
        revert_batch(db, applied.batch_id)

        row = TrackRepo(db).get_by_id(track_id)
        assert row is not None
        assert row["album"] == "Test Album"

    def test_reverts_every_field_in_the_batch(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="T", artist="A", album="B")
        result = revert_batch(db, applied.batch_id)

        assert result.reverted == 3
        row = TrackRepo(db).get_by_id(track_id)
        assert row is not None
        assert (row["title"], row["artist"], row["album"]) == (
            "Test Track",
            "Test Artist",
            "Test Album",
        )

    def test_records_a_new_batch_pointing_at_the_original(
        self, db: sqlite3.Connection, apply_change: Callable[..., ApplyResult]
    ) -> None:
        applied = apply_change(title="MB Title")
        result = revert_batch(db, applied.batch_id)

        batch = BatchRepo(db).get_by_id(result.batch_id)
        assert batch is not None
        assert batch["source"] == "revert"
        assert batch["reverts"] == applied.batch_id

    def test_history_shows_both_batches(
        self,
        db: sqlite3.Connection,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        result = revert_batch(db, applied.batch_id)

        changes = ChangeRepo(db).get_by_track(track_id)
        assert [c["batch_id"] for c in changes] == [applied.batch_id, result.batch_id]
        assert [c["source"] for c in changes] == ["csv-apply", "revert"]
        undo = changes[1]
        assert (undo["old_value"], undo["new_value"]) == ("MB Title", "Test Track")

    def test_unknown_batch_raises(self, db: sqlite3.Connection) -> None:
        with pytest.raises(CraterError):
            revert_batch(db, 999)

    def test_integer_field_bpm_reverts_on_flac(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(bpm="130")

        result = revert_batch(db, applied.batch_id)

        assert result.skipped_drift == []
        assert result.reverted == 1
        audio = mutagen.flac.FLAC(flac_with_tags)
        assert audio.tags is not None
        assert audio.tags["bpm"] == ["120"]
        row = TrackRepo(db).get_by_id(track_id)
        assert row is not None
        assert row["bpm"] == 120

    def test_mp3_title_and_track_no_round_trip(
        self,
        db: sqlite3.Connection,
        mp3_with_tags: Path,
        tmp_path: Path,
    ) -> None:
        generate_diff(db, [mp3_with_tags])
        row = TrackRepo(db).get_by_path(mp3_with_tags.resolve())
        assert row is not None
        track_id = int(row["id"])

        tags = read_tags(mp3_with_tags)
        before = {
            f: (str(getattr(tags, f)) if getattr(tags, f) is not None else None)
            for f in TAGGABLE_FIELDS
        }
        after = dict(before)
        after["title"] = "Changed Title"
        after["track_no"] = "7"

        diff = TrackDiff(
            track_id=track_id,
            path=mp3_with_tags.resolve(),
            decision="accept",
            confidence="high",
            before=before,
            after=after,
        )
        csv_path = tmp_path / "mp3_diff.csv"
        write_diff_csv([diff], csv_path)
        applied = apply_diff(db, csv_path)

        result = revert_batch(db, applied.batch_id)

        assert result.reverted == 2
        assert result.skipped_drift == []
        audio = mutagen.mp3.MP3(mp3_with_tags)
        assert audio.tags is not None
        assert str(audio.tags["TIT2"]) == "MP3 Track"
        assert str(audio.tags["TRCK"]) == "5"
        final_row = TrackRepo(db).get_by_id(track_id)
        assert final_row is not None
        assert final_row["title"] == "MP3 Track"
        assert final_row["track_no"] == 5


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------

class TestRevertDrift:
    def test_drift_skips_the_change(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        _set_file_title(flac_with_tags, "Changed Externally")

        result = revert_batch(db, applied.batch_id)

        assert result.skipped_drift == [(track_id, "title")]
        assert result.reverted == 0
        assert _file_title(flac_with_tags) == "Changed Externally"

    def test_drift_leaves_no_revert_history(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        _set_file_title(flac_with_tags, "Changed Externally")
        revert_batch(db, applied.batch_id)

        changes = ChangeRepo(db).get_by_track(track_id)
        assert [c["source"] for c in changes] == ["csv-apply"]

    def test_force_reverts_drifted_field(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        _set_file_title(flac_with_tags, "Changed Externally")

        result = revert_batch(db, applied.batch_id, force=True)

        assert result.skipped_drift == []
        assert result.reverted == 1
        assert _file_title(flac_with_tags) == "Test Track"

    def test_only_drifted_field_is_skipped(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title", artist="MB Artist")
        _set_file_title(flac_with_tags, "Changed Externally")

        result = revert_batch(db, applied.batch_id)

        assert result.skipped_drift == [(track_id, "title")]
        assert result.reverted == 1
        row = TrackRepo(db).get_by_id(track_id)
        assert row is not None
        assert row["artist"] == "Test Artist"


# ---------------------------------------------------------------------------
# Album art
# ---------------------------------------------------------------------------

class TestRevertArt:
    def test_strips_art_the_apply_added(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(art=ArtChange(staged_art, had_art=False))
        assert read_art(flac_with_tags) is not None

        result = revert_batch(db, applied.batch_id)

        assert result.reverted == 1
        assert read_art(flac_with_tags) is None

    def test_restores_the_stashed_image_byte_for_byte(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))
        applied = apply_change(art=ArtChange(staged_art, had_art=True))

        result = revert_batch(db, applied.batch_id)

        assert result.reverted == 1
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert sha256(blob.data).digest() == sha256(EXISTING_JPEG).digest()

    def test_art_changed_since_the_apply_is_drift(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(art=ArtChange(staged_art, had_art=False))
        embed_art(flac_with_tags, ArtBlob(FOREIGN_PNG, "image/png"))

        result = revert_batch(db, applied.batch_id)

        assert result.skipped_drift == [(track_id, "album_art")]
        assert result.reverted == 0
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert blob.data == FOREIGN_PNG

    def test_force_reverts_drifted_art(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(art=ArtChange(staged_art, had_art=False))
        embed_art(flac_with_tags, ArtBlob(FOREIGN_PNG, "image/png"))

        result = revert_batch(db, applied.batch_id, force=True)

        assert result.skipped_drift == []
        assert read_art(flac_with_tags) is None

    def test_reverting_the_revert_re_embeds(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(art=ArtChange(staged_art, had_art=False))
        undone = revert_batch(db, applied.batch_id)

        redone = revert_batch(db, undone.batch_id)

        assert redone.reverted == 1
        assert redone.skipped_drift == []
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert blob.data == staged_art.read_bytes()

    def test_missing_stash_is_skipped_with_a_reason(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        staged_art: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        embed_art(flac_with_tags, ArtBlob(EXISTING_JPEG, "image/jpeg"))
        applied = apply_change(art=ArtChange(staged_art, had_art=True))
        stash = Path(ChangeRepo(db).get_by_track(track_id)[0]["old_value"])
        stash.unlink()

        result = revert_batch(db, applied.batch_id)

        assert result.skipped_drift == [(track_id, "album_art (stash file missing)")]
        assert result.reverted == 0
        blob = read_art(flac_with_tags)
        assert blob is not None
        assert blob.data == staged_art.read_bytes()


# ---------------------------------------------------------------------------
# Missing file
# ---------------------------------------------------------------------------

class TestRevertMissingFile:
    def test_missing_file_skipped_not_an_error(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        flac_with_tags.unlink()

        result = revert_batch(db, applied.batch_id)

        assert result.skipped_missing == [track_id]
        assert result.reverted == 0

    def test_missing_file_keeps_db_row_and_history(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        flac_with_tags.unlink()
        revert_batch(db, applied.batch_id)

        assert TrackRepo(db).get_by_id(track_id) is not None
        assert len(ChangeRepo(db).get_by_track(track_id)) == 1


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestRevertDryRun:
    def test_dry_run_writes_nothing(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        result = revert_batch(db, applied.batch_id, dry_run=True)

        assert result.reverted == 1
        assert result.batch_id == 0
        assert _file_title(flac_with_tags) == "MB Title"
        row = TrackRepo(db).get_by_id(track_id)
        assert row is not None
        assert row["title"] == "MB Title"
        assert len(ChangeRepo(db).get_by_track(track_id)) == 1

    def test_dry_run_reports_drift(
        self,
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        _set_file_title(flac_with_tags, "Changed Externally")

        result = revert_batch(db, applied.batch_id, dry_run=True)

        assert result.skipped_drift == [(track_id, "title")]
