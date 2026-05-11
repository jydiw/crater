"""Public API for the tags module."""

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from crater.tags.apply import apply_diff as _apply_diff
from crater.tags.diff import generate_diff as _generate_diff
from crater.tags.diff import read_diff_csv as _read_diff_csv
from crater.tags.diff import write_diff_csv as _write_diff_csv
from crater.tags.lookup import LookupConfig, MbClient
from crater.tags.models import ApplyResult, TrackDiff


def generate_diff(
    conn: sqlite3.Connection,
    paths: Iterable[Path] | None = None,
    *,
    client: MbClient | None = None,
    lookup_cfg: LookupConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[TrackDiff]:
    return _generate_diff(
        conn,
        paths or [],
        client=client,
        lookup_cfg=lookup_cfg,
        progress=progress,
    )


def write_diff_csv(diffs: list[TrackDiff], out: Path) -> None:
    _write_diff_csv(diffs, out)


def read_diff_csv(path: Path) -> list[TrackDiff]:
    return _read_diff_csv(path)


def apply_diff(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ApplyResult:
    return _apply_diff(conn, csv_path, dry_run=dry_run, force=force)
