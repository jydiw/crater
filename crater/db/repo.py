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
        self, track_id: int, field: str | None = None
    ) -> list[sqlite3.Row]:
        if field:
            return self._conn.execute(
                "SELECT * FROM change WHERE track_id = ? AND field = ? ORDER BY changed_at",
                (track_id, field),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM change WHERE track_id = ? ORDER BY changed_at",
            (track_id,),
        ).fetchall()

    def get_by_batch(self, batch_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM change WHERE batch_id = ? ORDER BY track_id, field",
            (batch_id,),
        ).fetchall()
