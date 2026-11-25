"""CLI-level tests for revert / history / forget / prune.

The runner points crater at the tmp library and an isolated XDG_CONFIG_HOME,
so no real config file is read or scaffolded.
"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import mutagen.flac
import pytest
from click.testing import CliRunner, Result

from crater.cli import main
from crater.db.repo import TrackRepo
from crater.tags.models import ApplyResult


@pytest.fixture()
def cli(tmp_library: Path, tmp_path: Path) -> Callable[..., Result]:
    runner = CliRunner()
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
        "CRATER_LIBRARY_ROOT": str(tmp_library),
    }

    def _invoke(*args: str, stdin: str | None = None) -> Result:
        return runner.invoke(main, list(args), env=env, input=stdin)

    return _invoke


def _file_title(path: Path) -> str:
    audio = mutagen.flac.FLAC(path)
    assert audio.tags is not None
    return audio.tags["title"][0]  # type: ignore[index,no-any-return]


# ---------------------------------------------------------------------------
# revert
# ---------------------------------------------------------------------------


class TestCliRevert:
    def test_reverts_a_batch(
        self,
        cli: Callable[..., Result],
        flac_with_tags: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")

        result = cli("tags", "revert", str(applied.batch_id))

        assert result.exit_code == 0
        assert "reverted 1 field change(s)" in result.output
        assert _file_title(flac_with_tags) == "Test Track"

    def test_drift_exits_2(
        self,
        cli: Callable[..., Result],
        flac_with_tags: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        applied = apply_change(title="MB Title")
        audio = mutagen.flac.FLAC(flac_with_tags)
        audio.tags["title"] = ["Changed Externally"]  # type: ignore[index]
        audio.save()

        result = cli("tags", "revert", str(applied.batch_id))

        assert result.exit_code == 2
        assert "drift" in result.output
        assert _file_title(flac_with_tags) == "Changed Externally"

    def test_unknown_batch_exits_1(self, cli: Callable[..., Result]) -> None:
        result = cli("tags", "revert", "999")

        assert result.exit_code == 1
        assert "unknown batch: 999" in result.output


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


class TestCliHistory:
    def test_json_format(
        self,
        cli: Callable[..., Result],
        track_id: int,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title", artist="MB Artist")

        result = cli("tags", "history", str(track_id), "--format", "json")

        assert result.exit_code == 0
        payload = json.loads(result.stdout)  # stdout only: stderr carries notices
        assert {e["field"] for e in payload} == {"title", "artist"}

    def test_field_filter_by_path(
        self,
        cli: Callable[..., Result],
        flac_with_tags: Path,
        apply_change: Callable[..., ApplyResult],
    ) -> None:
        apply_change(title="MB Title", artist="MB Artist")

        result = cli("tags", "history", str(flac_with_tags), "--field", "title")

        assert result.exit_code == 0
        assert "Test Track → MB Title" in result.output
        assert "MB Artist" not in result.output


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


class TestCliForget:
    def test_yes_deletes_without_prompting(
        self, cli: Callable[..., Result], db: sqlite3.Connection, track_id: int
    ) -> None:
        result = cli("tags", "forget", str(track_id), "--yes")

        assert result.exit_code == 0
        assert f"forgot track {track_id}." in result.output
        assert TrackRepo(db).get_by_id(track_id) is None

    def test_declining_the_prompt_keeps_the_track(
        self, cli: Callable[..., Result], db: sqlite3.Connection, track_id: int
    ) -> None:
        result = cli("tags", "forget", str(track_id), stdin="n\n")

        assert result.exit_code == 1
        assert TrackRepo(db).get_by_id(track_id) is not None

    def test_unknown_track_exits_1(self, cli: Callable[..., Result]) -> None:
        result = cli("tags", "forget", "999", "--yes")

        assert result.exit_code == 1
        assert "unknown track: 999" in result.output


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------


class TestCliPrune:
    def test_dry_run_deletes_nothing(
        self,
        cli: Callable[..., Result],
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
    ) -> None:
        flac_with_tags.unlink()

        result = cli("tags", "prune", "--dry-run")

        assert result.exit_code == 0
        assert "1 track(s) would be forgotten" in result.output
        assert TrackRepo(db).get_by_id(track_id) is not None

    def test_yes_deletes_missing_tracks(
        self,
        cli: Callable[..., Result],
        db: sqlite3.Connection,
        flac_with_tags: Path,
        track_id: int,
    ) -> None:
        flac_with_tags.unlink()

        result = cli("tags", "prune", "--yes")

        assert result.exit_code == 0
        assert "forgot 1 track(s)." in result.output
        assert TrackRepo(db).get_by_id(track_id) is None

    def test_nothing_missing(self, cli: Callable[..., Result], track_id: int) -> None:
        result = cli("tags", "prune", "--yes")

        assert result.exit_code == 0
        assert "no tracks with missing files." in result.output


# ---------------------------------------------------------------------------
# diff — config warnings
# ---------------------------------------------------------------------------


class TestCliDiffSources:
    def test_unknown_source_and_field_are_warned_about(
        self, cli: Callable[..., Result], tmp_path: Path, track_id: int
    ) -> None:
        config = tmp_path / "xdg" / "crater" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[tags.sources]\ngenre = ["beatport"]\nmood = ["deezer"]\n')

        # --track with no PATH registers nothing, so no lookup is attempted.
        result = cli("tags", "diff", "--track", str(track_id))

        assert result.exit_code == 0
        assert "warning: unknown tag source 'beatport' (skipped)" in result.output
        assert "warning: unknown tag field 'mood' (skipped)" in result.output

    def test_the_source_option_no_longer_exists(
        self, cli: Callable[..., Result], track_id: int
    ) -> None:
        result = cli("tags", "diff", "--source", "acquired", "--track", str(track_id))

        assert result.exit_code == 2
        assert "No such option: --source" in result.output
