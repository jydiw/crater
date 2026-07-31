# crater

DJ music library management CLI. Cleans ID3/FLAC tags, detects musical keys,
and organizes files into a canonical store with symlink-based views.

## Status

| Module | Status |
|--------|--------|
| `tags diff` | Working (registration + AcoustID/MusicBrainz lookup) |
| `tags apply` | Working |
| `tags revert` | Working |
| `tags history` | Working |
| `tags forget / prune` | Working |
| `store` / `ingest` | Planned |
| `keys` | Planned |
| `views` | Planned |

---

## Installation

Requires Python 3.13 and [`fpcalc`](https://acoustid.org/chromaprint) (for future
AcoustID fingerprinting).

```bash
# macOS
brew install chromaprint

pip install -e .
```

---

## Configuration

Create `~/.config/crater/config.toml`:

```toml
library_root = "/path/to/your/dj/library"

[tags]
prefer = "earliest"                # or "acquired" — which MB release drives album/year/label
preserve_if_set = ["bpm"]          # never overwrite these fields when the file already has a value
acoustid_key = "YOUR_KEY"          # https://acoustid.org/login
acoustid_skip_threshold = 0.90     # skip fingerprinting when stored confidence ≥ this
acoustid_high_threshold = 0.90     # score ≥ this → decision=accept (auto)
acoustid_medium_threshold = 0.75   # score ≥ this → decision=review (medium confidence)
```

Or use environment variables:

```bash
export CRATER_LIBRARY_ROOT="/path/to/your/dj/library"
export ACOUSTID_KEY="YOUR_KEY"
```

The library root must exist and will hold:

```
<library_root>/
  crater.db       # SQLite database (auto-created on first run)
  store/          # canonical files — SHA256-named (future)
  staging/        # drop new downloads here (future)
  views/          # symlink trees (future)
```

---

## Usage

### `crater tags diff`

Registers any new files in the database (file tags → DB) and runs an AcoustID
fingerprint + MusicBrainz lookup against each one. Tracks where MB has
something to propose are written to a review CSV.

```bash
# Register + look up one or more files
crater tags diff track.flac

# Multiple files
crater tags diff ~/Downloads/*.mp3

# Save to a specific file (default: {library_root}/review/YYYY-MM-DD-HHMMSS.csv)
crater tags diff track.flac --out tmp/review.csv
```

Progress prints one line per track:

```
[1/3] Aphex Twin — Xtal … 4 change(s)
[2/3] ? — unknown.flac … skipped
[3/3] Boards of Canada — Roygbiv … 2 change(s)
```

The review CSV has **two rows per track** (`row_kind = before | after`) with
one column per taggable field. Editing an `after` cell is an implicit override.
The per-track `decision` column on the `after` row is one of:

| `decision` value | Effect |
|---|---|
| `accept` | Write the `after` row's values |
| `reject` | Skip this track |
| `review` | Skip and leave for later (default for medium-/low-confidence matches) |

Confidence bands (driven by AcoustID score):

| Score | `confidence` | Default `decision` |
|---|---|---|
| ≥ 0.90 | `high` | `accept` |
| 0.75–0.90 | `medium` | `review` |
| < 0.75 | `low` | `review` |

**Lookup short-circuits:**
- Tracks where you've already accepted a diff (`tags_reviewed_at` is set) are
  skipped entirely.
- Tracks with a stored confidence ≥ `acoustid_skip_threshold` skip the
  fingerprint step and go straight to MusicBrainz (cache-first).

MusicBrainz responses are cached in the `mb_cache` table, so re-running `diff`
on the same files doesn't hit the network again.

### `crater tags apply`

Applies an approved diff CSV to the database. Records every change in a single
batch for future revert.

```bash
# Preview without writing anything
crater tags apply review.csv --dry-run

# Apply (prompts for confirmation)
crater tags apply review.csv

# Apply without prompting
crater tags apply review.csv --yes

# Apply even if the file changed on disk since the diff was generated
crater tags apply review.csv --force
```

**Exit codes:** `0` success, `2` partial (drift/missing files detected — see stderr).

**Drift detection:** if a file's tag value changed on disk between `diff` and
`apply`, that field is skipped and reported. Re-run `diff` to get fresh values,
or use `--force` to write anyway.

### `crater tags revert`

Undoes one `apply` batch as a unit — every field it wrote goes back to its
earlier value, in the file tags and the DB. The revert is recorded as a batch
of its own, pointing at the batch it undid.

```bash
# Preview (including drift warnings)
crater tags revert 3 --dry-run

# Revert
crater tags revert 3

# Revert fields whose file value moved on since the apply
crater tags revert 3 --force
```

**Exit codes:** `0` success, `1` unknown batch, `2` partial (drift/missing files).

**Drift detection:** a field is only reverted if the file still holds the value
that batch wrote. Anything else is skipped and reported — use `--force` to
restore it anyway. Tracks whose file is gone are skipped, never an error.

### `crater tags history`

Shows the change log for one track, given its id or its path.

```bash
crater tags history 12
crater tags history ~/Music/DJ/store/track.flac

# Filter
crater tags history 12 --field title
crater tags history 12 --batch 3

# Machine-readable
crater tags history 12 --format json
```

```
when                 field   change                   source     batch
2026-07-30 14:02:11  title   Xtal → Xtal (Remaster)   csv-apply  3
2026-07-30 14:09:48  title   Xtal (Remaster) → Xtal   revert     4
```

### `crater tags forget` / `crater tags prune`

Deletion is first-class: `forget` drops one track, `prune` sweeps every track
whose file is gone. Both delete the DB row and its change history; batches and
the files on disk are left alone.

```bash
# One track (id or path), prompts for confirmation
crater tags forget 12
crater tags forget 12 --yes

# Sweep tracks whose file no longer exists
crater tags prune --dry-run
crater tags prune
crater tags prune --yes
```

---

## Development

```bash
# Install with dev dependencies
pip install -e . pytest soundfile numpy

# Run tests
pytest

# Type check
mypy crater/
```

Tests use self-authored silent audio fixtures generated at test time via
`soundfile` + `numpy`. No copyrighted audio is committed to the repo.
