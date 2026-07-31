"""Public API for the tags module."""

import sqlite3
from collections.abc import Callable, Iterable
from pathlib import Path

from crater.tags.apply import apply_diff as _apply_diff
from crater.tags.delete import forget_track as _forget_track
from crater.tags.delete import list_missing as _list_missing
from crater.tags.delete import prune_missing as _prune_missing
from crater.tags.diff import generate_diff as _generate_diff
from crater.tags.diff import read_diff_csv as _read_diff_csv
from crater.tags.diff import write_diff_csv as _write_diff_csv
from crater.tags.history import format_history as _format_history
from crater.tags.history import track_history as _track_history
from crater.tags.lookup import LookupConfig, MbClient
from crater.tags.models import ApplyResult, HistoryEntry, RevertResult, TrackDiff
from crater.tags.revert import revert_batch as _revert_batch
from crater.tags.tracks import find_track as _find_track


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


def revert_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> RevertResult:
    return _revert_batch(conn, batch_id, dry_run=dry_run, force=force)


def find_track(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    return _find_track(conn, ref)


def track_history(
    conn: sqlite3.Connection,
    track_id: int,
    *,
    field: str | None = None,
    batch_id: int | None = None,
) -> list[HistoryEntry]:
    return _track_history(conn, track_id, field=field, batch_id=batch_id)


def format_history(entries: list[HistoryEntry], fmt: str = "table") -> str:
    return _format_history(entries, fmt)


def forget_track(conn: sqlite3.Connection, track_id: int) -> bool:
    return _forget_track(conn, track_id)


def list_missing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return _list_missing(conn)


def prune_missing(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> list[sqlite3.Row]:
    return _prune_missing(conn, dry_run=dry_run)
