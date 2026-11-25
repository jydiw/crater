"""Apply an approved diff CSV to the DB (and file tags when the file needs updating)."""

import sqlite3
import time
from pathlib import Path

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.art import _EXT, ArtBlob, embed_art, read_art, sniff_mime
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


def _plan_art(d: TrackDiff, *, force: bool, result: ApplyResult) -> ArtBlob | None:
    """Check the staged image against the file as it stands now. Returns the
    blob to embed, or None with a skip recorded."""
    assert d.art is not None
    if not d.art.staged.exists():
        result.skipped_drift.append((d.track_id, "album_art (staged file missing)"))
        return None

    if (read_art(d.path) is not None) != d.art.had_art and not force:
        result.skipped_drift.append((d.track_id, "album_art"))
        return None

    data = d.art.staged.read_bytes()
    mime = sniff_mime(data)
    if mime is None:
        result.skipped_drift.append((d.track_id, "album_art (unknown image format)"))
        return None
    return ArtBlob(data, mime)


def _embed_art_change(
    d: TrackDiff, blob: ArtBlob, batch_id: int
) -> tuple[str, str | None, str | None]:
    """Stash whatever picture the file carries now so revert can put it back,
    embed the staged one, and return the change row."""
    assert d.art is not None
    current = read_art(d.path)
    stash: Path | None = None
    if current is not None:
        ext = _EXT.get(current.mime, "")
        stash = d.art.staged.parent / "stash" / f"{d.track_id}-{batch_id}{ext}"
        stash.parent.mkdir(parents=True, exist_ok=True)
        stash.write_bytes(current.data)
    embed_art(d.path, blob)
    return ("album_art", str(stash) if stash is not None else None, str(d.art.staged))


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

    updates: dict[str, str | None] = {}
    history: list[tuple[str, str | None, str | None]] = []
    needs_file_write = False

    for field_name, before_val, after_val in d.changed_fields():
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

    art_blob = None if d.art is None else _plan_art(d, force=force, result=result)

    if not updates and art_blob is None:
        return

    if dry_run:
        result.applied += len(updates) + (1 if art_blob is not None else 0)
        return

    if needs_file_write:
        updated_tags = apply_changes_to_tags(current_tags, updates)
        try:
            write_tags(d.path, updated_tags)
        except TagIOError as exc:
            result.errors.append(f"{d.path}: file write failed: {exc}")
            return

    if art_blob is not None:
        history.append(_embed_art_change(d, art_blob, batch_id))

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
