"""Undo an apply batch: write each recorded change's old_value back.

Drift check: a change is only reverted when the file still holds the value
that batch wrote (new_value). Anything else means the file moved on since —
skipped unless --force. Missing files are skipped, never an error; the DB
row (and its history) survives.

The revert is itself a batch, with `reverts` pointing at the batch undone.
"""

import sqlite3
from hashlib import sha256
from pathlib import Path

from crater.db.repo import BatchRepo, ChangeRepo, TrackRepo
from crater.tags.art import ArtBlob, embed_art, read_art, sniff_mime, strip_art
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


def _art_intact(path: Path, embedded: str | None) -> bool:
    """True when the file still carries what the batch left there: the exact
    bytes of the image at `embedded`, or no picture at all when it stripped one."""
    current = read_art(path)
    if not embedded:
        return current is None
    image = Path(embedded)
    if current is None or not image.is_file():
        return False
    return sha256(current.data).digest() == sha256(image.read_bytes()).digest()


def _art_skip_reason(path: Path, change: sqlite3.Row, *, force: bool) -> str | None:
    """Why this art change can't be reverted, or None when it can be."""
    if not force and not _art_intact(path, change["new_value"]):
        return "album_art"
    stash = change["old_value"]
    if not stash:
        return None  # the file had no art before the apply: strip it back off
    if not Path(stash).is_file():
        return "album_art (stash file missing)"
    if sniff_mime(Path(stash).read_bytes()) is None:
        return "album_art (unknown image format)"
    return None


def _restore_art(path: Path, change: sqlite3.Row) -> None:
    stash = change["old_value"]
    if not stash:
        strip_art(path)
        return
    data = Path(stash).read_bytes()
    mime = sniff_mime(data)
    assert mime is not None  # checked by _art_skip_reason
    embed_art(path, ArtBlob(data, mime))


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
    art_changes: list[sqlite3.Row] = []
    history: list[tuple[str, str | None, str | None]] = []

    for change in changes:
        field_name = change["field"]
        if field_name == "album_art":
            reason = _art_skip_reason(path, change, force=force)
            if reason is not None:
                result.skipped_drift.append((track_id, reason))
                continue
            art_changes.append(change)
        elif current_str(current_tags, field_name) != change["new_value"] and not force:
            result.skipped_drift.append((track_id, field_name))
            continue
        else:
            updates[field_name] = change["old_value"]
        history.append((field_name, change["new_value"], change["old_value"]))

    if not updates and not art_changes:
        return

    if dry_run:
        result.reverted += len(history)
        return

    if updates:
        try:
            write_tags(path, apply_changes_to_tags(current_tags, updates))
        except TagIOError as exc:
            result.errors.append(f"{path}: file write failed: {exc}")
            return

    for change in art_changes:
        _restore_art(path, change)

    db_updates: dict[str, object] = {f: to_db_value(f, v) for f, v in updates.items()}
    TrackRepo(conn).update_tags(track_id, db_updates)

    change_repo = ChangeRepo(conn)
    for field_name, before_val, after_val in history:
        change_repo.insert_row(
            track_id, field_name, before_val, after_val, "revert", batch_id
        )

    conn.commit()
    result.reverted += len(history)
