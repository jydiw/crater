"""Apply an approved diff CSV to the DB (and file tags when the file needs updating)."""

import sqlite3
import time
from pathlib import Path

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.diff import read_diff_csv
from crater.tags.errors import TagIOError
from crater.tags.io import read_tags, write_tags
from crater.tags.models import ApplyResult, TrackDiff
from crater.tags.values import apply_changes_to_tags, current_str, to_db_value


def apply_diff(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> ApplyResult:
    diffs = read_diff_csv(csv_path)

    actionable = [d for d in diffs if d.decision == "accept"]
    skipped_review = sum(1 for d in diffs if d.decision in ("review", "reject"))

    if not actionable:
        return ApplyResult(batch_id=0, skipped_review=skipped_review)

    batch_id = 0
    if not dry_run:
        batch_id = BatchRepo(conn).insert("csv-apply", csv_path=str(csv_path))

    result = ApplyResult(batch_id=batch_id, skipped_review=skipped_review)

    for d in actionable:
        _apply_track(conn, d, batch_id, dry_run=dry_run, force=force, result=result)

    return result


def _apply_track(
    conn: sqlite3.Connection,
    d: TrackDiff,
    batch_id: int,
    *,
    dry_run: bool,
    force: bool,
    result: ApplyResult,
) -> None:
    if not d.path.exists():
        result.skipped_missing.append(d.track_id)
        return

    try:
        current_tags = read_tags(d.path)
    except TagIOError as exc:
        result.errors.append(f"{d.path}: {exc}")
        return

    changes = d.changed_fields()
    if not changes:
        return

    updates: dict[str, str | None] = {}
    history: list[tuple[str, str | None, str | None]] = []
    needs_file_write = False

    for field_name, before_val, after_val in changes:
        current = current_str(current_tags, field_name)

        if current == after_val:
            # File already has the target value; DB-only sync.
            updates[field_name] = after_val
            history.append((field_name, before_val, after_val))
            continue

        if current != before_val and not force:
            result.skipped_drift.append((d.track_id, field_name))
            continue

        # File needs updating to reach after_val.
        needs_file_write = True
        updates[field_name] = after_val
        history.append((field_name, before_val, after_val))

    if not updates:
        return

    if dry_run:
        result.applied += len(updates)
        return

    if needs_file_write:
        updated_tags = apply_changes_to_tags(current_tags, updates)
        try:
            write_tags(d.path, updated_tags)
        except TagIOError as exc:
            result.errors.append(f"{d.path}: file write failed: {exc}")
            return

    db_updates: dict[str, object] = {f: to_db_value(f, v) for f, v in updates.items()}
    repo = TrackRepo(conn)
    repo.update_tags(d.track_id, db_updates)
    repo.mark_reviewed(d.track_id, int(time.time()))

    change_repo = ChangeRepo(conn)
    for field_name, before_val, after_val in history:
        change_repo.insert_row(
            d.track_id, field_name, before_val, after_val, "csv-apply", batch_id
        )

    conn.commit()
    result.applied += len(history)
