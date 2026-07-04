# crater roadmap — macOS app direction

Status: adopted 2026-07-03. Supersedes the "Build order" section of CLAUDE.md.
Context: personal tool for one DJ, not distributed. Licensing notes below only
matter if that changes.

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

## Source strategy (keyless-first)

| source | auth | gives crater | phase |
|---|---|---|---|
| MusicBrainz | none (User-Agent + 1 rps) | identity, album, year, label, MBIDs — **built** | done |
| MusicBrainz genres (`inc=genres`) | none | genre (generic but free) | P1 |
| Deezer API | none | **per-track BPM**, release date, album genre | P1 |
| iTunes Search API | none | genre (Apple taxonomy), release date | P1 |
| Cover Art Archive | none | album art (deferred milestone from tags slice) | P1+ |
| AcoustID | free key, one signup | fingerprint → recording MBID — **built**, becomes optional | done |
| Beatport | none (scraping) | DJ-accurate genre, key, BPM, catalog # | P6 |
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

### P0 — finish the tags CLI slice  [Sonnet/Opus mix; already planned]
Deliver: `crater tags revert BATCH_ID` (drift detection), `crater tags
history`, `crater tags forget`, `crater tags prune`.
Verify: pytest; manual round-trip — apply a batch, revert it, history shows both.

### P1 — metadata source adapters + merge policy  [Opus: design-sensitive]
- Generalize the lookup layer: a `MetadataSource` protocol (the existing
  `MbClient` Protocol is the seed). Each adapter returns per-field proposals
  with provenance.
- New adapters: Deezer (search + track BPM), iTunes Search (genre), MB
  text-search (makes fingerprinting optional), MB genres on the existing client.
- Merge policy: `[tags.sources]` config, provenance recorded in the CSV
  `notes`/a new `source` column so review shows where each value came from.
- Respect per-source rate limits (extend `ratelimit.py`).
Verify: stub-based tests per adapter (mock network layer, per repo convention);
live smoke on 3 real files showing BPM from Deezer + genre merge order.

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

### P3 — store + ingest engine, then ingest screen  [Opus engine, Sonnet screen]
- Engine as designed in CLAUDE.md: content-hash store, staging sweep,
  `acquired_at` from `st_birthtime`, `content_hash` backfill via one UPDATE.
- Screen 2: drag files from Finder → staging → watch hash/ingest/lookup.
Verify: ingest fixtures, re-ingest same file dedupes, backfill UPDATE row
count; drag-drop smoke test in app.

### P4 — keys engine, then key review screen  [Opus engine, Sonnet screen]
**Flagged risk — the roadmap's lowest-confidence phase, and not because of the
code.** Textbook K-S detection lands ~60–75% on electronic music; fixture tests
will pass while real-world detections disappoint. Expectation: the baseline is
a starting point, and the review workflow exists *because* detection is
imperfect. Do not skip the benchmark step.
- Engine as designed: librosa Krumhansl-Schmuckler baseline, canonical 24-key
  table, `key_source` lifecycle, `crater keys detect/confirm/review` CLI.
  Pin librosa/numba versions known to support Python 3.13 at execution time.
- Benchmark before trusting: ground truth is (a) keys the user has manually
  confirmed — `key_source = 'manual'` accumulates this for free during review —
  and (b) the GiantSteps Key / GiantSteps-MTG datasets (Beatport EDM excerpts
  with expert-corrected annotations; load via `mirdata`). Beatport's own keys
  are a *noisy reference only* — the user finds them unreliable, which is also
  why GiantSteps exists. Score with `mir_eval.key` (MIREX weighting) plus plain
  accuracy. Cheap known wins if the number is poor: HPSS preprocessing, segment
  voting, Essentia's EDM-tuned `edma`/`edmm` profiles, madmom's key CNN as an
  alternative backend.
- The detector sits behind a small interface (audio path → pitch class, mode,
  confidence). The user plans a crater-agnostic key-research package as a side
  project (eval-harness-first; calibrated "unknown" abstention is the research
  angle that fits crater's review queue). crater is that package's labeling
  tool and first consumer; the librosa baseline here is just backend #1.
- Screen 3: key review — in-app audio playback (HTML5 `<audio>` served over
  the localhost bridge), waveform overview strip (peaks computed in Python,
  canvas render) so the user can seek to the drop, confirm/correct key, sets
  `key_source = manual`.
- Prior art for the screen's feel: OneTagger's QuickTag (hotkey-driven rapid
  review). If energy tagging (see Candidate features) is adopted, capture it
  on this screen with hotkeys — one listening pass yields key + energy — and
  add the column in this phase's migration.
Verify: detection tests on self-authored tonal fixtures (known-key sine/chord
renders); canonicalization table test covers all 24 spellings; benchmark
accuracy reported against the labeled set; playback + confirm smoke test.

### P5 — views  [Sonnet: well-specified]
Symlink trees as designed: default views, user-defined templates + filters,
`rebuild`/`relink`.
Verify: rebuild against a seeded DB, tree structure matches a SQL query's
expectation; relink after moving a temp library root.

### P6 — Beatport adapter  [Opus; optional, explicitly rot-owned]
- Scraping, contained in one adapter module behind the same `MetadataSource`
  protocol. Study OneTagger's beatport module for endpoint/field shape
  (learn facts, don't copy code).
- Must degrade gracefully: markup shift → adapter returns nothing, run
  continues (same contract as lookup errors today).
- Spike first: verify current Beatport page structure before committing.
Verify: live smoke on tracks known to be on Beatport; kill-switch config to
disable the adapter.

### P7 — library browser + playlist planning  [spec only — do not build yet]
The end-state ambition. Sketch to pressure-test in a dedicated planning
session before any build:
- Browser: search/filter/sort over `tracks` (the GUI analog of views).
- Playlists: `playlists(id, name, created_at)` +
  `playlist_tracks(playlist_id, track_id, position)` tables.
- Planning aids: Camelot-wheel adjacency suggestions (±1 number, A↔B swap)
  and a BPM window (±6%, configurable) — crater's canonical key format makes
  this nearly free.
- Export: `.m3u8` (paths into `store/`) and Rekordbox XML — the bridge that
  makes planned playlists usable in actual DJ software.
Dependencies: P2 app shell, P4 keys, ideally P3 store.

## Candidate features (discussed 2026-07-03 — unscheduled until user opts in)

- **Energy/situation tagging** (1–5 energy; opener/peak/closer) — the P7
  playlist-planning ambition has no energy data to plan with; energy arc
  matters more than key compatibility for set flow. Cheapest capture: hotkeys
  on the P4 review screen (the one OneTagger QuickTag idea worth
  reimplementing). Wants its schema column decided before P4's migration.
- **Duplicate/version detection** — same `recording_mbid`, different
  `content_hash` = same track owned multiple times (320/FLAC/old rip). Nearly
  free SQL on data lookup already produces; surface in `crater status` or the
  browser.
- **Watched staging folder** — watchdog on `staging/`, auto-ingest + lookup,
  notify when tracks await review. Turns crater into a pipeline rather than a
  chore.
- **Library integrity doctor** — verify store hashes match filenames, orphaned
  DB rows, broken symlinks. An fsck for a hash-addressed store; fits P3 or P5.
- **Local BPM detection fallback** — librosa beat-track for tracks Deezer
  doesn't know (edits, bootlegs); piggybacks on P4's analysis pass.
- **Serato crate export** alongside Rekordbox XML at P7 (platform-agnostic
  goal). Stretch: transition preview (key/BPM-matched A→B audition) — native
  AVAudioUnitTimePitch does this well, webview DSP poorly; a reason the Swift
  shell migration might eventually pay for itself.

## Parked / out of scope

- Distribution, notarization, App Store (personal tool; revisit licensing +
  packaging if this changes). Note: App Store specifically is effectively
  incompatible with a bundled-Python app; a native shell is a prerequisite.
- Spotify audio features (API deprecated for new apps)
- Multi-key schema (per CLAUDE.md: `key_notes` free text is the answer)
- Tauri migration (only if pywebview packaging/polish fails)
