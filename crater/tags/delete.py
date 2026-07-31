"""Delete track rows. History cascades with the row; batches and files survive."""

import sqlite3

from crater.db.repo import TrackRepo


def forget_track(conn: sqlite3.Connection, track_id: int) -> bool:
    return TrackRepo(conn).delete_by_id(track_id)


def list_missing(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return TrackRepo(conn).get_orphaned()


def prune_missing(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> list[sqlite3.Row]:
    """Delete every track whose file is gone. Returns the rows deleted."""
    rows = list_missing(conn)
    if dry_run:
        return rows
    repo = TrackRepo(conn)
    for row in rows:
        repo.delete_by_id(row["id"])
    return rows
