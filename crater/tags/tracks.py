"""Resolve user-facing track references (id or path) to DB rows."""

import sqlite3

from crater.db.repo import TrackRepo
from crater.tags.errors import CraterError


def find_track(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = TrackRepo(conn).get_by_ref(ref)
    if row is None:
        raise CraterError(f"unknown track: {ref}")
    return row
