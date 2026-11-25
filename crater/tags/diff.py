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
from collections.abc import Callable, Iterable
from pathlib import Path

from crater.db.repo import TrackRepo
from crater.tags.art import ArtStager
from crater.tags.enrich import MbGenreFetcher
from crater.tags.io import detect_format, read_tags
from crater.tags.lookup import LookupConfig, MbClient, lookup_track
from crater.tags.merge import MetadataSource
from crater.tags.models import TAGGABLE_FIELDS, ArtChange, TrackDiff, TrackTags
from crater.tags.values import current_str

_META_FIELDS: tuple[str, ...] = (
    "track_id",
    "path",
    "row_kind",
    "decision",
    "confidence",
    "notes",
)
# album_art stays last so older CSVs (no such column) still read cleanly
_CSV_FIELDS: tuple[str, ...] = _META_FIELDS + TAGGABLE_FIELDS + ("album_art",)


def _tags_to_dict(tags: TrackTags) -> dict[str, str | None]:
    return {f: current_str(tags, f) for f in TAGGABLE_FIELDS}


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
    client: MbClient | None = None,
    lookup_cfg: LookupConfig | None = None,
    enrichers: dict[str, MetadataSource] | None = None,
    mb_genre_fetcher: MbGenreFetcher | None = None,
    art_stager: ArtStager | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[TrackDiff]:
    """Register tracks and return MB-proposed diffs.

    before = current file state; after = MB proposals.
    If `client` is None, only registration happens and no diffs are returned.
    """
    registered: list[tuple[int, Path]] = []
    for path in paths:
        path = path.resolve()
        if not path.exists():
            continue
        tid = _register_track(conn, path)
        registered.append((tid, path))

    if client is None or not registered:
        return []

    cfg = lookup_cfg or LookupConfig()
    repo = TrackRepo(conn)
    diffs: list[TrackDiff] = []
    total = len(registered)
    for i, (tid, path) in enumerate(registered, 1):
        row = repo.get_by_id(tid)
        assert row is not None
        diff = lookup_track(
            conn,
            client,
            row,
            lookup_cfg=cfg,
            enrichers=enrichers,
            mb_genre_fetcher=mb_genre_fetcher,
            art_stager=art_stager,
        )
        if progress is not None:
            label = f"{row['artist'] or '?'} — {row['title'] or path.name}"
            if diff is None:
                progress(f"[{i}/{total}] {label} … skipped")
            else:
                n = len(diff.changed_fields())
                progress(f"[{i}/{total}] {label} … {n} change(s)")
        # lookup_track already drops a Track with nothing to report.
        if diff is not None:
            diffs.append(diff)
    return diffs


def write_diff_csv(diffs: list[TrackDiff], out: Path) -> None:
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(_CSV_FIELDS))
        writer.writeheader()
        for d in diffs:
            writer.writerow(_row_dict(d, "before"))
            writer.writerow(_row_dict(d, "after"))
            if d.sources or d.art:
                writer.writerow(_source_row_dict(d))


def _art_cell(d: TrackDiff, kind: str) -> str:
    """before = whether the file already had a picture, after = the staged image."""
    if d.art is None:
        return ""
    if kind == "before":
        return "present" if d.art.had_art else ""
    if kind == "after":
        return str(d.art.staged)
    return d.art.source


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
    row["album_art"] = _art_cell(d, kind)
    return row


def _source_row_dict(d: TrackDiff) -> dict[str, object]:
    """Provenance row: which source won each changed field. Read back as an
    unknown row_kind, so apply never sees it."""
    row = _row_dict(d, "source")
    row.update({f: d.sources.get(f, "") for f in TAGGABLE_FIELDS})
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

        staged = (after_rec.get("album_art") or "").strip()
        art = (
            ArtChange(Path(staged), had_art=before_rec.get("album_art") == "present")
            if staged
            else None
        )

        diffs.append(
            TrackDiff(
                track_id=track_id,
                path=Path(after_rec["path"]),
                decision=decision,  # type: ignore[arg-type]
                confidence=confidence,  # type: ignore[arg-type]
                before=before,
                after=after,
                notes=after_rec.get("notes", ""),
                art=art,
            )
        )

    return diffs
