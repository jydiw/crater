"""Tests for generate_diff (registration) and CSV read/write round-trip."""

import csv
import sqlite3
from pathlib import Path

import pytest

from crater.db.repo import TrackRepo
from crater.tags.api import generate_diff, read_diff_csv, write_diff_csv
from crater.tags.models import TAGGABLE_FIELDS, ArtChange, TrackDiff


# ---------------------------------------------------------------------------
# Registration (the side-effect of generate_diff)
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_new_track_inserted_into_db(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        generate_diff(db, [flac_with_tags])
        assert TrackRepo(db).get_by_path(flac_with_tags) is not None

    def test_file_tags_populated_on_first_insert(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        generate_diff(db, [flac_with_tags])
        row = TrackRepo(db).get_by_path(flac_with_tags)
        assert row is not None
        assert row["title"] == "Test Track"
        assert row["artist"] == "Test Artist"
        assert row["release_year"] == 1996
        assert row["release_month"] == 9
        assert row["release_day"] == 23
        assert row["track_no"] == 3
        assert row["bpm"] == pytest.approx(120.0)

    def test_existing_track_not_overwritten(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        generate_diff(db, [flac_with_tags])
        track = TrackRepo(db).get_by_path(flac_with_tags)
        assert track is not None
        # Manually clobber a field in the DB
        TrackRepo(db).update_tags(track["id"], {"title": "Manually Set"})

        # Second diff should leave the existing row intact
        generate_diff(db, [flac_with_tags])
        row = TrackRepo(db).get_by_path(flac_with_tags)
        assert row is not None
        assert row["title"] == "Manually Set"

    def test_missing_file_skipped(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        ghost = tmp_path / "ghost.flac"
        generate_diff(db, [ghost])
        assert TrackRepo(db).list_all() == []

    def test_no_diff_rows_returned(
        self, db: sqlite3.Connection, flac_with_tags: Path
    ) -> None:
        # MB lookup not yet wired — always empty
        diffs = generate_diff(db, [flac_with_tags])
        assert diffs == []


# ---------------------------------------------------------------------------
# CSV write/read round-trip (independent of generate_diff)
# ---------------------------------------------------------------------------

def _make_diff(track_id: int, path: Path, **after_overrides: str | None) -> TrackDiff:
    """Build a TrackDiff simulating an MB proposal."""
    before: dict[str, str | None] = {f: None for f in TAGGABLE_FIELDS}
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


class TestCsvRoundtrip:
    def test_write_and_read_preserves_fields(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="MB Title", artist="MB Artist")
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)
        loaded = read_diff_csv(out)

        assert len(loaded) == 1
        assert loaded[0].track_id == 1
        assert loaded[0].after["title"] == "MB Title"
        assert loaded[0].after["artist"] == "MB Artist"
        assert loaded[0].decision == "accept"
        assert loaded[0].confidence == "high"

    def test_csv_has_two_rows_per_track(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="X")
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)

        with out.open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert rows[0]["row_kind"] == "before"
        assert rows[1]["row_kind"] == "after"

    def test_decision_only_on_after_row(
        self, db: sqlite3.Connection, flac_with_tags: Path, tmp_path: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="X")
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)

        with out.open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["decision"] == ""
        assert rows[1]["decision"] == "accept"

    def test_csv_headers_include_all_field_columns(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags)
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)

        with out.open(newline="") as f:
            headers = csv.DictReader(f).fieldnames
        assert headers is not None
        for col in ("track_id", "path", "row_kind", "decision", "confidence",
                    "notes", "title", "artist", "bpm", "recording_mbid"):
            assert col in headers

    def test_before_values_preserved_on_read(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        before = {f: None for f in TAGGABLE_FIELDS}
        before["title"] = "Old Title"
        after = dict(before)
        after["title"] = "New Title"
        diff = TrackDiff(
            track_id=1, path=flac_with_tags,
            decision="accept", confidence="high",
            before=before, after=after,
        )
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)
        loaded = read_diff_csv(out)

        assert loaded[0].before["title"] == "Old Title"
        assert loaded[0].after["title"] == "New Title"

    def test_reject_decision_round_trips(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="X")
        diff.decision = "reject"
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)
        loaded = read_diff_csv(out)
        assert loaded[0].decision == "reject"

    def test_no_source_row_when_provenance_is_empty(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="X")
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)

        with out.open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert [r["row_kind"] for r in rows] == ["before", "after"]

    def test_source_row_names_the_winner_per_field(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="X", genre="House")
        diff.sources = {"title": "musicbrainz", "genre": "deezer"}
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)

        with out.open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert [r["row_kind"] for r in rows] == ["before", "after", "source"]

        source_row = rows[2]
        assert source_row["title"] == "musicbrainz"
        assert source_row["genre"] == "deezer"
        assert source_row["decision"] == ""
        assert source_row["confidence"] == ""
        assert source_row["notes"] == ""
        assert all(
            source_row[f] == "" for f in TAGGABLE_FIELDS if f not in diff.sources
        )

    def test_source_row_is_invisible_to_read(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        plain = _make_diff(1, flac_with_tags, title="X")
        with_sources = _make_diff(1, flac_with_tags, title="X")
        with_sources.sources = {"title": "musicbrainz"}

        plain_out = tmp_path / "plain.csv"
        sourced_out = tmp_path / "sourced.csv"
        write_diff_csv([plain], plain_out)
        write_diff_csv([with_sources], sourced_out)

        assert read_diff_csv(sourced_out) == read_diff_csv(plain_out)

    def test_edited_after_cell_read_as_new_value(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        diff = _make_diff(1, flac_with_tags, title="MB Title")
        out = tmp_path / "diff.csv"
        write_diff_csv([diff], out)

        # Simulate user hand-editing the after row's title cell
        text = out.read_text()
        out.write_text(text.replace("MB Title", "User Override"))

        loaded = read_diff_csv(out)
        assert loaded[0].after["title"] == "User Override"


# ---------------------------------------------------------------------------
# Album art column
# ---------------------------------------------------------------------------

class TestCsvArt:
    def _art_only(self, path: Path, staged: Path, *, had_art: bool) -> TrackDiff:
        diff = _make_diff(1, path)
        diff.art = ArtChange(staged, had_art=had_art)
        return diff

    def test_art_only_diff_is_written_with_a_source_row(
        self, tmp_path: Path, flac_with_tags: Path, staged_art: Path
    ) -> None:
        out = tmp_path / "diff.csv"
        write_diff_csv([self._art_only(flac_with_tags, staged_art, had_art=True)], out)

        with out.open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert [r["row_kind"] for r in rows] == ["before", "after", "source"]
        assert rows[0]["album_art"] == "present"
        assert rows[1]["album_art"] == str(staged_art)
        assert rows[2]["album_art"] == "coverartarchive"

    def test_absent_art_leaves_the_before_cell_empty(
        self, tmp_path: Path, flac_with_tags: Path, staged_art: Path
    ) -> None:
        out = tmp_path / "diff.csv"
        write_diff_csv([self._art_only(flac_with_tags, staged_art, had_art=False)], out)

        with out.open(newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["album_art"] == ""

    @pytest.mark.parametrize("had_art", [True, False])
    def test_art_survives_the_round_trip(
        self, tmp_path: Path, flac_with_tags: Path, staged_art: Path, had_art: bool
    ) -> None:
        out = tmp_path / "diff.csv"
        write_diff_csv(
            [self._art_only(flac_with_tags, staged_art, had_art=had_art)], out
        )

        loaded = read_diff_csv(out)
        assert len(loaded) == 1
        assert loaded[0].art == ArtChange(staged_art, had_art=had_art)

    def test_no_art_reads_back_as_none(
        self, tmp_path: Path, flac_with_tags: Path
    ) -> None:
        out = tmp_path / "diff.csv"
        write_diff_csv([_make_diff(1, flac_with_tags, title="X")], out)

        assert read_diff_csv(out)[0].art is None

    def test_legacy_csv_without_the_column_reads_as_none(
        self, tmp_path: Path, flac_with_tags: Path, staged_art: Path
    ) -> None:
        out = tmp_path / "diff.csv"
        write_diff_csv([self._art_only(flac_with_tags, staged_art, had_art=True)], out)

        # album_art is the last column: drop it to get a pre-art CSV back
        trimmed = [ln.rsplit(",", 1)[0] for ln in out.read_text().splitlines()]
        out.write_text("\n".join(trimmed) + "\n")

        loaded = read_diff_csv(out)
        assert loaded[0].art is None
