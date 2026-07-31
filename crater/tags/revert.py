"""Undo an apply batch: write each recorded change's old_value back.

Drift check: a change is only reverted when the file still holds the value
that batch wrote (new_value). Anything else means the file moved on since —
skipped unless --force. Missing files are skipped, never an error; the DB
row (and its history) survives.

The revert is itself a batch, with `reverts` pointing at the batch undone.
"""

import sqlite3
from pathlib import Path

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.errors import CraterError, TagIOError
from crater.tags.io import read_tags, write_tags
from crater.tags.models import RevertResult
from crater.tags.values import apply_changes_to_tags, current_str, to_db_value


def revert_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> RevertResult:
    if BatchRepo(conn).get_by_id(batch_id) is None:
        raise CraterError(f"unknown batch: {batch_id}")

    by_track: dict[int, list[sqlite3.Row]] = {}
    for change in ChangeRepo(conn).get_by_batch(batch_id):
        by_track.setdefault(change["track_id"], []).append(change)

    new_batch_id = 0
    if by_track and not dry_run:
        new_batch_id = BatchRepo(conn).insert("revert", reverts=batch_id)

    result = RevertResult(batch_id=new_batch_id)
    for track_id, changes in by_track.items():
        _revert_track(
            conn,
            track_id,
            changes,
            new_batch_id,
            dry_run=dry_run,
            force=force,
            result=result,
        )
    return result


def _revert_track(
    conn: sqlite3.Connection,
    track_id: int,
    changes: list[sqlite3.Row],
    batch_id: int,
    *,
    dry_run: bool,
    force: bool,
    result: RevertResult,
) -> None:
    row = TrackRepo(conn).get_by_id(track_id)
    if row is None:
        # Track was forgotten; its history rows went with it, nothing to restore.
        return

    path = Path(row["path"])
    if not path.exists():
        result.skipped_missing.append(track_id)
        return

    try:
        current_tags = read_tags(path)
    except TagIOError as exc:
        result.errors.append(f"{path}: {exc}")
        return

    updates: dict[str, str | None] = {}
    history: list[tuple[str, str | None, str | None]] = []

    for change in changes:
        field_name = change["field"]
        if current_str(current_tags, field_name) != change["new_value"] and not force:
            result.skipped_drift.append((track_id, field_name))
            continue
        updates[field_name] = change["old_value"]
        history.append((field_name, change["new_value"], change["old_value"]))

    if not updates:
        return

    if dry_run:
        result.reverted += len(updates)
        return

    try:
        write_tags(path, apply_changes_to_tags(current_tags, updates))
    except TagIOError as exc:
        result.errors.append(f"{path}: file write failed: {exc}")
        return

    db_updates: dict[str, object] = {f: to_db_value(f, v) for f, v in updates.items()}
    TrackRepo(conn).update_tags(track_id, db_updates)

    change_repo = ChangeRepo(conn)
    for field_name, before_val, after_val in history:
        change_repo.insert_row(
            track_id, field_name, before_val, after_val, "revert", batch_id
        )

    conn.commit()
    result.reverted += len(history)
