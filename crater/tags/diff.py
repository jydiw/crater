"""Generate tag diffs and read/write the review CSV.

CSV format: two rows per track (row_kind=before/after), one column per
taggable field. Per-track decision/confidence/notes on the "after" row.

Semantics:
  before = current file state (what's already in your file)
  after  = MB-proposed values (what crater wants to write)

New tracks are registered (file tags → DB) silently on first sight.
No diff rows are produced until MB lookup is implemented.
"""

import csv
import sqlite3
from pathlib import Path
from typing import Iterable

from crater.db.repo import TrackRepo
from crater.tags.io import detect_format, read_tags
from crater.tags.models import TAGGABLE_FIELDS, TrackDiff, TrackTags

_META_FIELDS: tuple[str, ...] = (
    "track_id",
    "path",
    "row_kind",
    "decision",
    "confidence",
    "notes",
)
_CSV_FIELDS: tuple[str, ...] = _META_FIELDS + TAGGABLE_FIELDS


def _stringify(val: object) -> str | None:
    if val is None:
        return None
    return str(val)


def _tags_to_dict(tags: TrackTags) -> dict[str, str | None]:
    return {f: _stringify(getattr(tags, f)) for f in TAGGABLE_FIELDS}


def _register_track(conn: sqlite3.Connection, path: Path) -> int:
    """Ensure track is in the DB; populate tag fields from file on first insert.

    Returns the track_id.
    """
    repo = TrackRepo(conn)
    existing = repo.get_by_path(path)
    if existing is not None:
        return existing["id"]

    stat = path.stat()
    acquired_at = int(getattr(stat, "st_birthtime", stat.st_mtime))
    track_id = repo.insert(
        path=path,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        acquired_at=acquired_at,
        fmt=detect_format(path),
    )
    file_tags = read_tags(path)
    repo.update_tags(track_id, {f: getattr(file_tags, f) for f in TAGGABLE_FIELDS})
    return track_id


def generate_diff(
    conn: sqlite3.Connection,
    paths: Iterable[Path],
    *,
    preserve_if_set: Iterable[str] = (),
) -> list[TrackDiff]:
    """Register tracks and return MB-proposed diffs.

    before = current file state; after = MB proposals.
    Returns [] until MB lookup is implemented; tracks are still registered.
    preserve_if_set: fields to skip if the file already has a value (e.g. bpm).
    """
    _ = set(preserve_if_set)  # reserved for the MB-lookup path

    for path in paths:
        path = path.resolve()
        if not path.exists():
            continue
        _register_track(conn, path)

    # MB lookup not yet implemented — no diff rows.
    return []


def write_diff_csv(diffs: list[TrackDiff], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(_CSV_FIELDS))
        writer.writeheader()
        for d in diffs:
            writer.writerow(_row_dict(d, "before"))
            writer.writerow(_row_dict(d, "after"))


def _row_dict(d: TrackDiff, kind: str) -> dict[str, object]:
    values = d.before if kind == "before" else d.after
    row: dict[str, object] = {
        "track_id": d.track_id,
        "path": str(d.path),
        "row_kind": kind,
        "decision": d.decision if kind == "after" else "",
        "confidence": d.confidence if kind == "after" else "",
        "notes": d.notes if kind == "after" else "",
    }
    for f in TAGGABLE_FIELDS:
        row[f] = values.get(f) or ""
    return row


def read_diff_csv(path: Path) -> list[TrackDiff]:
    pairs: dict[int, dict[str, dict[str, str]]] = {}

    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for rec in reader:
            try:
                track_id = int(rec["track_id"])
            except (ValueError, KeyError):
                continue
            kind = (rec.get("row_kind") or "").strip()
            if kind not in ("before", "after"):
                continue
            pairs.setdefault(track_id, {})[kind] = rec

    diffs: list[TrackDiff] = []
    for track_id, kinds in pairs.items():
        if "before" not in kinds or "after" not in kinds:
            continue
        before_rec = kinds["before"]
        after_rec = kinds["after"]

        decision = (after_rec.get("decision") or "review").strip()
        if decision not in ("accept", "reject", "review"):
            decision = "review"

        confidence = (after_rec.get("confidence") or "review").strip()
        if confidence not in ("high", "medium", "low", "review"):
            confidence = "review"

        before = {f: (before_rec.get(f) or None) for f in TAGGABLE_FIELDS}
        after = {f: (after_rec.get(f) or None) for f in TAGGABLE_FIELDS}

        diffs.append(
            TrackDiff(
                track_id=track_id,
                path=Path(after_rec["path"]),
                decision=decision,  # type: ignore[arg-type]
                confidence=confidence,  # type: ignore[arg-type]
                before=before,
                after=after,
                notes=after_rec.get("notes", ""),
            )
        )

    return diffs
