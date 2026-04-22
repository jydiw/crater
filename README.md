# crater

DJ music library management CLI. Cleans ID3/FLAC tags, detects musical keys,
and organizes files into a canonical store with symlink-based views.

## Status

| Module | Status |
|--------|--------|
| `tags diff` | Working (file→DB, no MB lookup) |
| `tags apply` | Working |
| `tags revert` | Planned |
| `tags history` | Planned |
| `tags forget / prune` | Planned |
| MusicBrainz lookup | Planned |
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
prefer = "earliest"          # or "acquired" — which MB release drives album/year/label
acoustid_key = "YOUR_KEY"    # https://acoustid.org/login
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

Compares each file's on-disk tags to the database and writes a CSV for review.
On first run, auto-registers the file in the database.

```bash
# Compare one or more files to DB (no MusicBrainz lookup)
crater tags diff track.flac --no-lookup

# Multiple files
crater tags diff ~/Downloads/*.mp3 --no-lookup

# Save to a specific file (default: ./crater-diff-<timestamp>.csv)
crater tags diff track.flac --no-lookup --out /tmp/review.csv
```

The output CSV has one row per changed field:

```
batch_ref,track_id,path,field,old_value,new_value,source,confidence,decision,override_value,notes
```

Edit the CSV in a spreadsheet or text editor, then set the `decision` column:

| `decision` value | Effect |
|---|---|
| `accept` | Write `new_value` to DB |
| `reject` | Skip this field |
| `override` | Write `override_value` instead |
| `review` | Skip and report (hasn't been decided yet) |

Default for `--no-lookup` diffs: all rows start as `accept`.

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
