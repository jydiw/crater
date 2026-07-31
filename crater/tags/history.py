"""Read and render the change log for one track."""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from crater.db.repo import ChangeRepo
from crater.tags.models import HistoryEntry

_HEADER = ("when", "field", "change", "source", "batch")


def track_history(
    conn: sqlite3.Connection,
    track_id: int,
    *,
    field: str | None = None,
    batch_id: int | None = None,
) -> list[HistoryEntry]:
    rows = ChangeRepo(conn).get_by_track(track_id, field=field, batch_id=batch_id)
    return [HistoryEntry(**dict(row)) for row in rows]


def format_history(entries: list[HistoryEntry], fmt: str = "table") -> str:
    if fmt == "json":
        return json.dumps([asdict(e) for e in entries], indent=2)
    return _table(entries)


def _show(val: str | None) -> str:
    return "—" if val is None else val


def _table(entries: list[HistoryEntry]) -> str:
    rows = [
        (
            datetime.fromtimestamp(e.changed_at).strftime("%Y-%m-%d %H:%M:%S"),
            e.field,
            f"{_show(e.old_value)} → {_show(e.new_value)}",
            e.source,
            str(e.batch_id),
        )
        for e in entries
    ]
    widths = [max(len(r[i]) for r in [_HEADER, *rows]) for i in range(len(_HEADER))]
    return "\n".join(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in [_HEADER, *rows]
    )
