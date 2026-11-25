"""Migration 003: cached MB payloads survive the move to source_cache."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import crater.db
from crater.db.connection import open_db
from crater.db.repo import SourceCacheRepo

_MIGRATIONS = Path(crater.db.__file__).parent / "migrations"

_PAYLOAD = {"id": "rec-abc", "title": "Cached Title"}


def _legacy_db(db_path: Path) -> None:
    """Build a schema-version-2 database the way connection._migrate would have."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        ) STRICT
    """)
    for version in (1, 2):
        sql = next(_MIGRATIONS.glob(f"{version:03d}_*.sql")).read_text()
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    conn.execute(
        "INSERT INTO mb_cache (recording_mbid, payload, fetched_at) VALUES (?,?,?)",
        ("rec-abc", json.dumps(_PAYLOAD), 1700000000),
    )
    conn.commit()
    conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


class TestSourceCacheMigration:
    def test_mb_cache_rows_move_to_source_cache(self, tmp_path: Path) -> None:
        db_path = tmp_path / "crater.db"
        _legacy_db(db_path)

        conn = open_db(db_path)
        try:
            rows = conn.execute("SELECT version FROM schema_version").fetchall()
            assert 3 in {r[0] for r in rows}
            assert _tables(conn) >= {"source_cache"}
            assert "mb_cache" not in _tables(conn)
            assert SourceCacheRepo(conn).get("mb-recording", "rec-abc") == _PAYLOAD

            row = conn.execute(
                "SELECT fetched_at FROM source_cache WHERE cache_key = 'rec-abc'"
            ).fetchone()
            assert row[0] == 1700000000
        finally:
            conn.close()

    def test_fresh_database_has_source_cache_only(self, tmp_path: Path) -> None:
        conn = open_db(tmp_path / "crater.db")
        try:
            tables = _tables(conn)
            assert "source_cache" in tables
            assert "mb_cache" not in tables
        finally:
            conn.close()
