import sys
from datetime import datetime
from pathlib import Path

import click

from crater import config as cfg_mod
from crater.db.connection import open_db
from crater.db.repo import SourceCacheRepo
from crater.tags.api import (
    apply_diff,
    find_track,
    forget_track,
    format_history,
    generate_diff,
    list_missing,
    prune_missing,
    read_diff_csv,
    revert_batch,
    track_history,
    write_diff_csv,
)
from crater.tags.art import ArtStager, CaaArtSource
from crater.tags.enrich import (
    KNOWN_SOURCES,
    ArtSource,
    DeezerArtSource,
    DeezerSource,
    ITunesArtSource,
    ITunesSource,
    MbGenreFetcher,
    unknown_field_names,
    unknown_source_names,
)
from crater.tags.errors import CraterError
from crater.tags.lookup import LookupConfig, RealMbClient
from crater.tags.merge import DEFAULT_FIELD_SOURCES
from crater.tags.ratelimit import RateLimiter


def _default_review_path(library_root: Path) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return library_root / "review" / f"{ts}.csv"


def _art_sources(
    names: tuple[str, ...], cache: SourceCacheRepo, limiter: RateLimiter
) -> list[ArtSource]:
    """The art lookup chain, in configured order. Names crater has no source for
    were already warned about."""
    sources: list[ArtSource] = []
    for name in names:
        if name == "coverartarchive":
            sources.append(CaaArtSource())
        elif name == "deezer":
            sources.append(DeezerArtSource(cache, limiter))
        elif name == "itunes":
            sources.append(ITunesArtSource(cache, limiter))
    return sources


def _resolve_out_path(out: Path | None, library_root: Path) -> Path:
    """Absolute → untouched; relative → rooted at library_root; None → auto-timestamped."""
    if out is None:
        return _default_review_path(library_root)
    if out.is_absolute():
        return out
    return library_root / out


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "--library",
    envvar="CRATER_LIBRARY_ROOT",
    type=click.Path(),
    default=None,
    help="Path to library root (contains crater.db). Overrides config.",
)
@click.pass_context
def main(ctx: click.Context, library: str | None) -> None:
    ctx.ensure_object(dict)
    try:
        crater_cfg = cfg_mod.load(Path(library) if library else None)
        library_root = cfg_mod.require_library_root(crater_cfg)
    except CraterError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    ctx.obj["cfg"] = crater_cfg
    ctx.obj["library_root"] = library_root
    ctx.obj["db_path"] = library_root / "crater.db"


# ---------------------------------------------------------------------------
# `crater tags` subgroup
# ---------------------------------------------------------------------------


@main.group()
def tags() -> None:
    """Tag management commands."""


@tags.command("diff")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Output CSV path. Relative paths resolve under the library root. "
    "Default: {library_root}/review/YYYY-MM-DD-HHMMSS.csv",
)
@click.option(
    "--track",
    "track_ids",
    multiple=True,
    type=int,
    help="Restrict to track IDs (repeatable).",
)
@click.option(
    "--source",
    type=click.Choice(["earliest", "acquired"]),
    default=None,
    help="Which release to prefer for MB tags. Overrides config.",
)
@click.option(
    "--replace-art",
    is_flag=True,
    default=False,
    help="Propose cover art even for files that already have some.",
)
@click.pass_context
def tags_diff(
    ctx: click.Context,
    paths: tuple[Path, ...],
    out: Path | None,
    track_ids: tuple[int, ...],
    source: str | None,
    replace_art: bool,
) -> None:
    """Register tracks and generate a MB tag diff CSV for review.

    New files are registered (existing file tags written to DB) automatically.
    The review CSV only contains MB-proposed changes.
    """
    if not paths and not track_ids:
        click.echo("error: provide at least one PATH or --track ID", err=True)
        sys.exit(1)

    db_path: Path = ctx.obj["db_path"]
    library_root: Path = ctx.obj["library_root"]
    crater_cfg: dict = ctx.obj["cfg"]
    prefer = source or cfg_mod.tags_prefer(crater_cfg)
    preserve_if_set = cfg_mod.tags_preserve_if_set(crater_cfg)

    acoustid_key = cfg_mod.tags_acoustid_key(crater_cfg)

    field_sources = {**DEFAULT_FIELD_SOURCES, **cfg_mod.tags_sources(crater_cfg)}
    for name in unknown_field_names(field_sources):
        click.echo(f"warning: unknown tag field '{name}' (skipped)", err=True)
        del field_sources[name]
    for name in unknown_source_names(field_sources, KNOWN_SOURCES):
        click.echo(f"warning: unknown tag source '{name}' (skipped)", err=True)

    lookup_cfg = LookupConfig(
        prefer=prefer,  # type: ignore[arg-type]
        preserve_if_set=tuple(preserve_if_set),
        acoustid_skip=cfg_mod.tags_acoustid_skip(crater_cfg),
        acoustid_high=cfg_mod.tags_acoustid_high(crater_cfg),
        acoustid_medium=cfg_mod.tags_acoustid_medium(crater_cfg),
        fingerprint_enabled=acoustid_key is not None,
        replace_art=replace_art,
        field_sources=field_sources,
    )
    limiter = RateLimiter()
    client = RealMbClient(acoustid_key=acoustid_key, rate_limiter=limiter)

    conn = open_db(db_path)
    cache = SourceCacheRepo(conn)
    art_sources = _art_sources(cfg_mod.tags_art_sources(crater_cfg), cache, limiter)
    diffs = generate_diff(
        conn,
        list(paths),
        client=client,
        lookup_cfg=lookup_cfg,
        enrichers={
            "deezer": DeezerSource(cache, limiter),
            "itunes": ITunesSource(cache, limiter),
        },
        mb_genre_fetcher=MbGenreFetcher(cache, limiter),
        art_stager=ArtStager(library_root / "review" / "art", limiter, art_sources),
        progress=lambda line: click.echo(line),
    )
    conn.close()

    if not diffs:
        click.echo("tracks registered. no MB differences found.")
        return

    out_path = _resolve_out_path(out, library_root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_diff_csv(diffs, out_path)
    click.echo(f"wrote {len(diffs)} track diff(s) to {out_path}")


@tags.command("apply")
@click.argument("csv_file", type=click.Path(path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be written without touching files or DB.",
)
@click.option(
    "--force", is_flag=True, default=False, help="Write even if drift is detected."
)
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
def tags_apply(
    ctx: click.Context,
    csv_file: Path,
    dry_run: bool,
    force: bool,
    yes: bool,
) -> None:
    """Apply an approved diff CSV.

    Tracks with decision=accept are written to the DB (and to file tags
    when the file differs from the "after" row). Every change is recorded
    in the change history under a single batch ID.

    Relative CSV paths resolve under the library root.
    """
    db_path: Path = ctx.obj["db_path"]
    library_root: Path = ctx.obj["library_root"]

    csv_file = _resolve_out_path(csv_file, library_root)
    if not csv_file.exists():
        click.echo(f"error: CSV not found: {csv_file}", err=True)
        sys.exit(1)

    diffs = read_diff_csv(csv_file)
    actionable = [d for d in diffs if d.decision == "accept"]

    if not actionable:
        click.echo("nothing to apply (no tracks with decision=accept).")
        return

    if not dry_run and not yes:
        click.echo(f"{len(actionable)} track(s) will be written.")
        click.confirm("proceed?", abort=True)

    conn = open_db(db_path)
    result = apply_diff(conn, csv_file, dry_run=dry_run, force=force)
    conn.close()

    if dry_run:
        click.echo(f"dry run: {result.applied} field change(s) would be applied.")
        return

    click.echo(f"applied {result.applied} field change(s) in batch {result.batch_id}.")

    if result.skipped_review:
        click.echo(
            f"  skipped {result.skipped_review} row(s) with decision=review/reject."
        )
    if result.skipped_drift:
        click.echo(
            f"  skipped {len(result.skipped_drift)} field(s) due to drift "
            f"(re-run diff or use --force):",
            err=True,
        )
        for track_id, field in result.skipped_drift:
            click.echo(f"    track {track_id}: {field}", err=True)
    if result.skipped_missing:
        click.echo(
            f"  skipped {len(result.skipped_missing)} track(s) — file not found:",
            err=True,
        )
        for track_id in result.skipped_missing:
            click.echo(f"    track {track_id}", err=True)
    if result.errors:
        for err in result.errors:
            click.echo(f"  error: {err}", err=True)

    if result.skipped_drift or result.skipped_missing or result.errors:
        sys.exit(2)


@tags.command("revert")
@click.argument("batch_id", type=int)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be reverted without touching files or DB.",
)
@click.option(
    "--force", is_flag=True, default=False, help="Revert even if drift is detected."
)
@click.pass_context
def tags_revert(ctx: click.Context, batch_id: int, dry_run: bool, force: bool) -> None:
    """Undo a previous apply batch.

    Every field the batch wrote is restored to its earlier value, in both the
    file tags and the DB. Fields whose file value has moved on since the apply
    are skipped unless --force. The revert is recorded as its own batch.
    """
    db_path: Path = ctx.obj["db_path"]

    conn = open_db(db_path)
    try:
        result = revert_batch(conn, batch_id, dry_run=dry_run, force=force)
    except CraterError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    finally:
        conn.close()

    if dry_run:
        click.echo(f"dry run: {result.reverted} field change(s) would be reverted.")
    else:
        click.echo(
            f"reverted {result.reverted} field change(s) in batch "
            f"{result.batch_id} (undoes batch {batch_id})."
        )

    if result.skipped_drift:
        click.echo(
            f"  skipped {len(result.skipped_drift)} field(s) due to drift "
            f"(file changed since the apply — use --force):",
            err=True,
        )
        for track_id, field_name in result.skipped_drift:
            click.echo(f"    track {track_id}: {field_name}", err=True)
    if result.skipped_missing:
        click.echo(
            f"  skipped {len(result.skipped_missing)} track(s) — file not found:",
            err=True,
        )
        for track_id in result.skipped_missing:
            click.echo(f"    track {track_id}", err=True)
    if result.errors:
        for err in result.errors:
            click.echo(f"  error: {err}", err=True)

    if result.skipped_drift or result.skipped_missing or result.errors:
        sys.exit(2)


@tags.command("history")
@click.argument("track")
@click.option("--field", "field_name", default=None, help="Only this field.")
@click.option("--batch", "batch_id", type=int, default=None, help="Only this batch.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format.",
)
@click.pass_context
def tags_history(
    ctx: click.Context,
    track: str,
    field_name: str | None,
    batch_id: int | None,
    fmt: str,
) -> None:
    """Show the change log for one track (id or path)."""
    db_path: Path = ctx.obj["db_path"]

    conn = open_db(db_path)
    try:
        row = find_track(conn, track)
        entries = track_history(conn, row["id"], field=field_name, batch_id=batch_id)
    except CraterError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    finally:
        conn.close()

    if not entries and fmt == "table":
        click.echo(f"no changes recorded for track {row['id']}.")
        return
    click.echo(format_history(entries, fmt))


@tags.command("forget")
@click.argument("track")
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
def tags_forget(ctx: click.Context, track: str, yes: bool) -> None:
    """Delete one track (id or path) from the DB.

    Its change history goes with it; batches and the file on disk survive.
    """
    db_path: Path = ctx.obj["db_path"]

    conn = open_db(db_path)
    try:
        row = find_track(conn, track)
        if not yes:
            click.echo(f"  [{row['id']}] {row['path']}")
            click.confirm("forget this track and its history?", abort=True)
        forget_track(conn, row["id"])
    except CraterError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    finally:
        conn.close()

    click.echo(f"forgot track {row['id']}.")


@tags.command("prune")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="List tracks with missing files without deleting them.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.pass_context
def tags_prune(ctx: click.Context, dry_run: bool, yes: bool) -> None:
    """Delete tracks whose file no longer exists."""
    db_path: Path = ctx.obj["db_path"]

    conn = open_db(db_path)
    try:
        missing = list_missing(conn)
        if not missing:
            click.echo("no tracks with missing files.")
            return

        for row in missing:
            click.echo(f"  [{row['id']}] {row['path']}")

        if dry_run:
            click.echo(f"dry run: {len(missing)} track(s) would be forgotten.")
            return

        if not yes:
            click.confirm(
                f"forget {len(missing)} track(s) and their history?", abort=True
            )
        pruned = prune_missing(conn)
        click.echo(f"forgot {len(pruned)} track(s).")
    finally:
        conn.close()
