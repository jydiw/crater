import sys
from datetime import datetime
from pathlib import Path

import click

from crater import config as cfg_mod
from crater.db.connection import open_db
from crater.tags.api import apply_diff, generate_diff, read_diff_csv, write_diff_csv
from crater.tags.errors import CraterError


def _default_review_path(library_root: Path) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return library_root / "review" / f"{ts}.csv"


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
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Output CSV path. Relative paths resolve under the library root. "
                   "Default: {library_root}/review/YYYY-MM-DD-HHMMSS.csv")
@click.option("--track", "track_ids", multiple=True, type=int,
              help="Restrict to track IDs (repeatable).")
@click.option("--source", type=click.Choice(["earliest", "acquired"]), default=None,
              help="Which release to prefer for MB tags. Overrides config.")
@click.pass_context
def tags_diff(
    ctx: click.Context,
    paths: tuple[Path, ...],
    out: Path | None,
    track_ids: tuple[int, ...],
    source: str | None,
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

    conn = open_db(db_path)
    diffs = generate_diff(
        conn,
        list(paths),
        prefer=prefer,
        preserve_if_set=preserve_if_set,
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
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be written without touching files or DB.")
@click.option("--force", is_flag=True, default=False,
              help="Write even if drift is detected.")
@click.option("--yes", is_flag=True, default=False,
              help="Skip confirmation prompt.")
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
        click.echo(f"  skipped {result.skipped_review} row(s) with decision=review/reject.")
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
