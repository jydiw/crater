import json
import sqlite3
from pathlib import Path


class TrackRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_by_path(self, path: Path) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM track WHERE path = ?", (str(path),)
        ).fetchone()

    def get_by_id(self, track_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM track WHERE id = ?", (track_id,)
        ).fetchone()

    def get_by_ref(self, ref: str) -> sqlite3.Row | None:
        """Resolve a user-facing track reference: all-digits = id, else a path."""
        if ref.isdigit():
            return self.get_by_id(int(ref))
        return self.get_by_path(Path(ref).resolve())

    def insert(
        self,
        path: Path,
        size_bytes: int,
        mtime_ns: int,
        acquired_at: int,
        fmt: str,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO track (path, size_bytes, mtime_ns, acquired_at, format) VALUES (?,?,?,?,?)",
            (str(path), size_bytes, mtime_ns, acquired_at, fmt),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_tags(self, track_id: int, fields: dict[str, object]) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [track_id]
        self._conn.execute(
            f"UPDATE track SET {cols}, updated_at = strftime('%s','now') WHERE id = ?",  # noqa: S608
            vals,
        )
        self._conn.commit()

    def list_all(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM track ORDER BY id").fetchall()

    def delete_by_id(self, track_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM track WHERE id = ?", (track_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def get_orphaned(self) -> list[sqlite3.Row]:
        return [r for r in self.list_all() if not Path(r["path"]).exists()]

    def set_mb_confidence(self, track_id: int, score: float | None) -> None:
        self._conn.execute(
            "UPDATE track SET mb_confidence = ? WHERE id = ?",
            (score, track_id),
        )
        self._conn.commit()

    def mark_reviewed(self, track_id: int, when: int) -> None:
        self._conn.execute(
            "UPDATE track SET tags_reviewed_at = ? WHERE id = ?",
            (when, track_id),
        )
        self._conn.commit()


class SourceCacheRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, source: str, cache_key: str) -> dict | list | None:
        row = self._conn.execute(
            "SELECT payload FROM source_cache WHERE source = ? AND cache_key = ?",
            (source, cache_key),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def put(self, source: str, cache_key: str, payload: dict | list) -> None:
        self._conn.execute(
            """
            INSERT INTO source_cache (source, cache_key, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(source, cache_key) DO UPDATE SET
                payload = excluded.payload,
                fetched_at = strftime('%s','now')
            """,
            (source, cache_key, json.dumps(payload)),
        )
        self._conn.commit()

    def delete(self, source: str, cache_key: str) -> None:
        self._conn.execute(
            "DELETE FROM source_cache WHERE source = ? AND cache_key = ?",
            (source, cache_key),
        )
        self._conn.commit()


class BatchRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert(
        self,
        source: str,
        csv_path: str | None = None,
        reverts: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO batch (source, csv_path, reverts) VALUES (?,?,?)",
            (source, csv_path, reverts),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_by_id(self, batch_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM batch WHERE id = ?", (batch_id,)
        ).fetchone()


class ChangeRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_row(
        self,
        track_id: int,
        field: str,
        old_value: str | None,
        new_value: str | None,
        source: str,
        batch_id: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO change
                (track_id, field, old_value, new_value, source, batch_id)
            VALUES (?,?,?,?,?,?)
            """,
            (track_id, field, old_value, new_value, source, batch_id),
        )

    def get_by_track(
        self,
        track_id: int,
        field: str | None = None,
        batch_id: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM change WHERE track_id = ?"
        params: list[object] = [track_id]
        if field:
            sql += " AND field = ?"
            params.append(field)
        if batch_id is not None:
            sql += " AND batch_id = ?"
            params.append(batch_id)
        # id breaks ties: changed_at is second-granularity, a batch lands within one
        return self._conn.execute(f"{sql} ORDER BY changed_at, id", params).fetchall()  # noqa: S608

    def get_by_batch(self, batch_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM change WHERE batch_id = ? ORDER BY track_id, field",
            (batch_id,),
        ).fetchall()
