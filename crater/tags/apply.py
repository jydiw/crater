"""Apply an approved diff CSV to the DB (and file tags when the file needs updating)."""

import sqlite3
from pathlib import Path

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.diff import read_diff_csv
from crater.tags.errors import TagIOError
from crater.tags.io import read_tags, write_tags
from crater.tags.models import ApplyResult, TrackDiff, TrackTags

_INT_FIELDS = {"release_year", "release_month", "release_day", "track_no"}
_REAL_FIELDS = {"bpm"}


def _to_db_value(field: str, str_val: str | None) -> object:
    if str_val is None:
        return None
    if field in _INT_FIELDS:
        try:
            return int(str_val)
        except ValueError:
            return None
    if field in _REAL_FIELDS:
        try:
            return float(str_val)
        except ValueError:
            return None
    return str_val


def _current_str(tags: TrackTags, field: str) -> str | None:
    val = getattr(tags, field)
    return str(val) if val is not None else None


def _apply_changes_to_tags(
    current: TrackTags, updates: dict[str, str | None]
) -> TrackTags:
    kwargs = {f: getattr(current, f) for f in current.__dataclass_fields__}
    for f, new_val in updates.items():
        kwargs[f] = _to_db_value(f, new_val)
    return TrackTags(**kwargs)


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
        current = _current_str(current_tags, field_name)

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
        updated_tags = _apply_changes_to_tags(current_tags, updates)
        try:
            write_tags(d.path, updated_tags)
        except TagIOError as exc:
            result.errors.append(f"{d.path}: file write failed: {exc}")
            return

    db_updates: dict[str, object] = {f: _to_db_value(f, v) for f, v in updates.items()}
    TrackRepo(conn).update_tags(d.track_id, db_updates)

    change_repo = ChangeRepo(conn)
    for field_name, before_val, after_val in history:
        change_repo.insert_row(
            d.track_id, field_name, before_val, after_val, "csv-apply", batch_id
        )

    conn.commit()
    result.applied += len(history)
