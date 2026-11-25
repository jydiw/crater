# crater roadmap — macOS app direction

Status: adopted 2026-07-03, revised 2026-07-30. Supersedes the "Build order"
section of CLAUDE.md. Context: personal tool for one DJ, not distributed.
Licensing notes below only matter if that changes.

## What changed and why

crater began as a CLI with a plan to wrap beets and OneTagger for metadata.
Investigation (2026-07-03) dissolved that plan:

- **beets** (MIT, Python, active — v2.12.0 June 2026) duplicates what crater's
  tags slice already does: MB lookup, release matching, review workflow. Its
  import model (moves/renames files, own `library.db`) conflicts with crater's
  content-hash store and single-source-of-truth principles. **Role: reference
  implementation only** — study its matching heuristics, never depend on it.
- **OneTagger** (GPL-3.0, Rust, no release since Aug 2023) has the DJ-store
  data crater wants (Beatport/Traxsource genre, key, BPM) but is effectively
  unmaintained, writes tags in place with no review step, and forking its code
  would make crater GPL if ever distributed. **Role: reference implementation
  only** — its source shows which endpoints/fields each store exposes.

The new direction: **crater becomes a macOS app — a GUI shell over the
existing Python engine — with native multi-source metadata adapters.** The CSV
review workflow was always a stand-in for a review UI; the app is its
successor, not a replacement for the engine. The CLI survives throughout.

### Revision 2026-07-30

Priorities reordered around what the user actually needs now: standardized ID3
tags, and playlists that land in rekordbox. The user over-acquires music and
plays little of it, and finds rekordbox itself hard to work in — so culling and
slicing the library along acquired date, genre, and key has to happen inside
crater. That pulls the library browser + playlists + rekordbox export from last
place to directly after the app shell. Album art moves up with it, into the
adapters phase.

Two phases fall away. Key detection now lives in **rille**
(`~/Documents/GitHub/rille`), a separate crater-agnostic key-research tool the
user built: its own SQLite DB keyed by audio content hash, nine detectors plus
a majority-vote ensemble, and its own audition/review UI. crater consumes
rille's manually-confirmed keys over a `content_hash` join instead of growing a
detection engine, benchmark harness, and review screen of its own. Views /
symlink trees are shelved — in-app filtering slices the library the same way
without a filesystem tree to keep in sync. The store's physical migration is
deferred as well: crater computes `content_hash` in place, no file moves.

## Decision log

| decision | choice | rationale | revisit if |
|---|---|---|---|
| Engine | Keep the existing Python core | Working, tested (69 tests); rewrite buys nothing; librosa/mutagen have no maintained Swift equivalents (Swift path = bridge Essentia/TagLib C++, a project in itself) | never, realistically |
| UI shell (v1) | pywebview (WKWebView, pure Python) | Engine reuse dominates; v1 screens are CRUD + audio, where webviews are fine; user is new to macOS app dev — one frontier at a time | see Swift shell row |
| Swift shell (later) | Supported migration path, not a failure mode | User is actively building Swift skills on another project; the bridge API is the seam — swap webview for SwiftUI views calling the same engine over a local socket | when drag-out-to-Finder/Rekordbox, native polish, or packaging pain justifies it (webviews cannot drag files *out*; that gesture is native-only) |
| Frontend | No-build vanilla JS/ESM (or preact via ESM import) | No node toolchain in a Python repo until a screen demands it | UI complexity outgrows it |
| Engine↔UI bridge | JSON API callable two ways: pywebview `js_api` for daily use, thin localhost dev server for development | **Executing agents cannot see a pywebview window.** UI must run in a plain browser tab during dev so agents drive/verify it with browser preview tooling | — |
| Metadata sources | Native adapters, keyless-first (see table) | No signups; stable JSON APIs don't rot like scrapers | a gated source becomes indispensable |
| Fingerprinting | Optional — text-match MB first, AcoustID only for mystery files | Store-bought files arrive well-tagged; AcoustID needs a (free) signup | text matching proves unreliable |
| beets / OneTagger | Reference implementations, never dependencies | See above | — |
| GPL hygiene | No OneTagger code copied in-tree; endpoints/field names are facts, fine to learn | Keeps crater MIT-clean | — |
| Key detection | Owned by rille, a separate repo; crater imports confirmed keys over a `content_hash` join | rille is deliberately crater-agnostic (its own DB, detectors, review UI); folding it in would couple a research tool's churn to crater's schema, and crater only ever needs the final answer | rille stalls, or crater needs keys for files rille has never seen |
| Rekordbox integration | XML export | XML is rekordbox's supported import bridge — playlists, file paths, cues. pyrekordbox can write the encrypted DB directly, but that path is reverse-engineered and breaks on rekordbox updates | XML proves too lossy for a needed field; pyrekordbox is then the fallback, accepted as fragile |
| `content_hash` | Computed in place — no `store/` directory, no file moves, yet | Unblocks the rille join and duplicate detection immediately; moving files is orthogonal and can wait | see the store migration note under Parked — the rekordbox wrinkle sets the deadline |
| Views / symlink trees | Shelved indefinitely | In-app browser filtering does the same slicing, with no derived tree to rebuild, relink, or let drift | in-app browsing proves insufficient, or another tool needs filesystem-shaped access |

## Source strategy (keyless-first)

| source | auth | gives crater | phase |
|---|---|---|---|
| MusicBrainz | none (User-Agent + 1 rps) | identity, album, year, label, MBIDs — **built** | done |
| MusicBrainz genres (`inc=genres`) | none | genre (generic but free) — **built** | done |
| Deezer API | none | **per-track BPM**, release date, album genre — **built** | done |
| iTunes Search API | none | genre (Apple taxonomy), release date — **built** | done |
| Cover Art Archive | none | album art (embedded, not just fetched) — **built** | done |
| AcoustID | free key, one signup | fingerprint → recording MBID — **built**, becomes optional | done |
| rille | none — local SQLite DB | key + `key_source`, joined on `content_hash` | P4 |
| Beatport | none (scraping) | DJ-accurate genre, key, BPM, catalog # | parked |
| Discogs | free personal token | label/catalog depth | parked |
| Spotify | app registration; audio-features API deprecated | — | skip |

Field-priority merge is config, e.g.:

```toml
[tags.sources]
genre = ["beatport", "deezer", "musicbrainz"]   # first source with a value wins
bpm   = ["deezer"]                               # preserve_if_set still applies
album = ["musicbrainz"]
```

## Phases

Model routing (per user CLAUDE.md): Sonnet for well-specified single-file work,
Opus for multi-file or design-sensitive phases. Tags below are suggestions.
Every phase ends with: full pytest pass, README.md updated, project memory
updated.

### P0 — finish the tags engine slice  [done 2026-07-30]
Deliver: `crater tags revert BATCH_ID` (drift detection), `crater tags
history`, `crater tags forget`, `crater tags prune`, and the thin Click
wrappers over each.
Verify: pytest; manual round-trip — apply a batch, revert it, history shows both.

### P1 — metadata source adapters + merge policy + album art  [done 2026-08-02 — live smoke pending]
- Generalize the lookup layer: a `MetadataSource` protocol (the existing
  `MbClient` Protocol is the seed). Each adapter returns per-field proposals
  with provenance.
- New adapters: Deezer (search + track BPM), iTunes Search (genre), MB
  text-search (makes fingerprinting optional), MB genres on the existing client.
- Merge policy: `[tags.sources]` config, provenance recorded in the CSV
  `notes`/a new `source` column so review shows where each value came from.
- Album art: Cover Art Archive fetch keyed on `release_mbid`, embedded as ID3
  `APIC` / FLAC picture block. Skip if the file already carries art unless
  forced; art changes go through the same batch/history record as text tags.
- Respect per-source rate limits (extend `ratelimit.py`).
Verify: stub-based tests per adapter (mock network layer, per repo convention);
live smoke on 3 real files showing BPM from Deezer + genre merge order + art
embedded and readable back by mutagen.

### P2 — app skeleton + tag review screen  [Opus: new surface]
- `crater/app/` — UI as static web files + a JSON bridge, runnable two ways:
  `crater app` (pywebview window, `js_api`) and `crater app --dev` (localhost
  server, open in a browser). Same files, same bridge surface. The dev mode
  exists so executing agents can drive and verify the UI with browser preview
  tooling — a pywebview window is opaque to them. Build and test browser-first;
  wrap in pywebview last.
- Screen 1: tag review table — the CSV diff as an interactive before/after
  grid; accept/reject/edit per track; apply records a batch exactly as CSV
  apply does. CSV export stays (it already exists; power-user path).
Verify: in `--dev` mode, run a diff against fixtures, accept 2 / reject 1,
confirm `tag_batches` + `tag_history` rows match; same flow smoke-checked once
in the pywebview window; engine test suite untouched.

### P3 — library browser + playlists + rekordbox export  [Opus: multi-surface]
The phase the revision exists for. Depends on P2.
- `content_hash` computed **in place** — hash every tracked file, backfill the
  column, no file moves. It is rille's join key (P4) and it makes duplicate
  detection a plain SQL query: same `recording_mbid`, different `content_hash`
  = the same track owned twice (320 vs FLAC vs old rip). Surface duplicates in
  the browser as a filter.
- Screen 2: library browser — search/filter/sort over `tracks` along acquired
  date, genre, key, label, artist, BPM. This is the slicing mechanism that
  replaces views; multi-select feeds culling and playlist building.
- Playlists: `playlists(id, name, created_at)` +
  `playlist_tracks(playlist_id, track_id, position)`, drag-to-reorder.
- Export: `.m3u8` (absolute paths to files where they currently live) and
  rekordbox XML — `DJ_PLAYLISTS` with a `COLLECTION` of `TRACK` entries
  (`Location` as `file://localhost/...` percent-encoded) plus a `PLAYLISTS`
  node. XML is the supported import bridge; no writes to rekordbox's own DB.
- Planning aids: Camelot adjacency suggestions (±1 number, A↔B swap) and a BPM
  window (±6%, configurable), live once P4 lands keys.
Verify: against a seeded DB, each browser filter's result set matches the
equivalent SQL query row-for-row; exported XML validates against a known-good
rekordbox export's structure and imports into rekordbox with playlist order
intact; every path in an exported `.m3u8` resolves on disk; re-hashing an
unmodified library is a no-op (zero rows updated).

### P4 — rille key integration  [Sonnet: well-specified]
Detection, benchmarking, and key review all live in rille. crater's side is a
small read-only adapter, nothing more.
- Open rille's SQLite DB read-only, select tracks whose key is
  manually confirmed, join to `tracks` on `content_hash`, write `key` and
  `key_source = 'manual'` through the normal diff/apply path so the import is
  batched and revertible like any other tag change.
- Unmatched hashes are reported, never guessed at — no fuzzy title matching.
- crater keeps the canonical key format and its 24-spelling table (CLAUDE.md,
  "Key format"): crater still stores and writes `Fm - 4A` form, and normalizes
  whatever rille hands over into it.
- rille's DB path is config (`[keys] rille_db`); missing DB is a clean skip,
  not an error.
Verify: fixture rille DB → confirmed keys land on the matching tracks with
`key_source = 'manual'` and a `tag_history` batch; a row whose `content_hash`
matches nothing is skipped and reported, writing zero rows; non-manual rille
rows are never imported; all 24 canonical spellings round-trip.

## Candidate features (unscheduled until user opts in)

- **Energy/situation tagging** (1–5 energy; opener/peak/closer) — playlist
  planning has no energy data to plan with, and energy arc matters more than
  key compatibility for set flow. Capture surface is now either the P3 browser
  (inline edit while culling) or rille's review flow, where the user is already
  listening end-to-end. Wants its schema column decided before P3's migration.
- **Watched staging folder** — watchdog on a drop folder, auto-hash + lookup,
  notify when tracks await review. Turns crater into a pipeline rather than a
  chore.
- **Library integrity doctor** — verify stored hashes still match file
  contents, find orphaned DB rows and missing files. Fits alongside P3's
  hashing pass.
- **Local BPM detection fallback** — for tracks Deezer doesn't know (edits,
  bootlegs). rille already loads and analyzes the audio, so it is the cheaper
  home for this than a second analysis pass in crater.
- **Serato crate export** alongside rekordbox XML at P3 (platform-agnostic
  goal). Stretch: transition preview (key/BPM-matched A→B audition) — native
  AVAudioUnitTimePitch does this well, webview DSP poorly; a reason the Swift
  shell migration might eventually pay for itself.

## Parked / out of scope

- **Views / symlink trees** — shelved indefinitely as of 2026-07-30. The P3
  browser slices the library along the same axes without a derived tree to
  rebuild, relink, or let drift. Revisit only if in-app browsing proves
  insufficient, or if some other tool needs filesystem-shaped access.
- **Store physical migration** — moving files into a content-hash-named
  `store/` is deferred; P3 computes `content_hash` in place. **Wrinkle:** once
  rekordbox holds file paths, moving files breaks every reference in it. So the
  move has to happen either before rekordbox is deeply wired, or as a planned
  one-time re-import. Decide before that debt compounds, not after.
- **Beatport adapter** — optional and explicitly rot-owned. If ever built:
  scraping contained in one adapter module behind the `MetadataSource`
  protocol, degrading to "returns nothing" on markup shift (same contract as
  lookup errors today), with a kill-switch config and a structure spike before
  any commitment. Study OneTagger's beatport module for endpoint/field shape —
  learn facts, don't copy code. Note the user finds Beatport's own keys
  unreliable; rille's confirmed keys win regardless.
- Distribution, notarization, App Store (personal tool; revisit licensing +
  packaging if this changes). Note: App Store specifically is effectively
  incompatible with a bundled-Python app; a native shell is a prerequisite.
- Spotify audio features (API deprecated for new apps)
- Multi-key schema (per CLAUDE.md: `key_notes` free text is the answer)
- Tauri migration (only if pywebview packaging/polish fails)
