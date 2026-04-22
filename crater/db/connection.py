import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = _open(db_path)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        ) STRICT
    """)
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}

    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.stem.split("_")[0])
        if version not in applied:
            conn.executescript(path.read_text())
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.commit()
