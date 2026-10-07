# BRD — P1: Manifest Skip Cache for Mining

**Status:** Draft v0.1 (for review — no implementation exists yet)
**Scope decision:** P1 only. P2 (pi-layout subagent/fork exclusion in `scan_convos`) was explicitly **dropped** by Reevon — subagent transcripts keep being mined verbatim.
**Repo state at writing:** `develop` @ `0727958` (hermetic test-env conftest fix; the only code change from the RCA session).

---

## 1. Problem Statement

`mempalace mine` is idempotent, but the *skip-decision itself* scales with palace size, not with what changed. On anchor repos with long session history (e.g. TML CVP: **1319 files / 271 MB** of session transcripts), every mine trigger spends the overwhelming majority of its wall time computing what to skip — before a single new file is processed. Mines fire per session end and serialize behind the palace flock, so slow mines also queue.

## 2. Measured Evidence (2026-08-31, live CVP palace)

| # | Claim | Measurement |
|---|-------|-------------|
| C1 | CVP convo collection holds **457,952 points** | qdrant REST `count` on `mempalace_33fd231b0fdf6acf_mempalace_drawers` @ localhost:6333, status green |
| C2 | Prefetch page cost degrades 0.06s → 9s → 20.5s per 1000 rows | `collection.get(limit=1000, offset∈{0, total/2, total−1000}, include=[metadatas])` — on-disk payload makes late pages disk-bound |
| C3 | Every convo mine runs **two full paginated scans** (`prefetch_mined_set` + `prefetch_content_hashes`) regardless of how little changed | code: `mempalace/palace.py:1532` and `:1618`, called unconditionally from `mempalace/convo_miner.py::_mine_convos_impl` (~L1064) |
| C4 | Directory scan is cheap: 0.44–0.71s for 1319 files | `scan_convos()` timing, warm FS cache |
| C5 | Projects mode has the same disease in a different shape: **per-file** `file_already_mined(collection, source_file, check_mtime=True)` — once per file, twice for changed files (pre-lock + post-lock) | code: `mempalace/miner.py::process_file` (~L1519, L1564); the convo miner's own docstring calls this pattern ">1h of pure skip-checking" on a 2000-file sweep |
| C6 | A third miner exists: `--mode extract` → `format_miner.mine_formats` (PDF/DOCX/PPTX/XLSX/RTF/EPUB) — skip semantics **not yet audited** | `mempalace/cli.py` mode dispatch (~L801) |
| C7 | The 17 test failures seen during RCA were **environment leakage** (shell-exported `MEMPALACE_BACKEND=qdrant` etc. routed tests off the chroma backend), not chromadb drift. Fixed hermetically in `0727958`; clean-env run passes | `env -u MEMPALACE_* ... pytest tests/test_convo_miner.py::test_convo_mining` → passed |
| C8 | CVP drawers live in a separate palace: `~/.config/mempalace/cvp` → qdrant remote prefix `mempalace_33fd231b0fdf6acf`; namespace env mismatch raises `BackendMismatchError` by design | `qdrant_backend.json` marker in palace dir |

> Reviewers: attack these claims first. C2 in particular was measured partly under self-induced contention (a second scan process was running); absolute numbers may soften, the O(palace) scaling conclusion should not.

## 3. Goals

- **G1** — Skip-check cost scales with **changed files**, never palace size.
- **G2** — Warm no-op mine (nothing changed) completes in **< 1s** without opening the storage backend.
- **G3** — One design serves all three miners: `projects` (miner.py), `convos` (convo_miner.py), `extract` (format_miner.py).
- **G4** — Cache is **disposable by construction**: deleting it is always safe; worst case after crash/corruption = one slow re-mine. Zero risk to verbatim data.
- **G5** — No behavior change to what gets filed (verbatim principle untouched; P2 deliberately dropped).

## 4. Non-Goals

- Not touching direct-write flows (`add_drawer`, `diary_write`, `event_append`, `checkpoint`, `artifact_put`, `file_conversation_exchange`) — explicit single writes with their own dedup; no tree scan, nothing to cache.
- Not changing storage backends, embedding, chunking, or NORMALIZE_VERSION semantics.
- Not solving multi-machine manifest coherence (mesh/shared-brain) — documented as v2.
- No search-time filtering of subagent content (P2 is dead; if subagent noise shows up in search, that's a future search-filter feature).

## 5. Architecture

### 5.1 Storage

One SQLite file **inside the palace dir**: `<palace>/manifest/convo_manifest.sqlite3`, WAL mode, busy_timeout. Precedent: `qdrant_backend.json` already lives in palace dirs. Identity is implicit (in-palace), travels with palace copies.

```sql
CREATE TABLE files (
  wing TEXT NOT NULL,
  extract_mode TEXT NOT NULL,        -- 'exchange' | 'general' | 'projects' | 'extract'
  source_file TEXT NOT NULL,
  mtime REAL,                        -- source_mtime exactly as stored on drawers today
  size INTEGER,                      -- extra staleness signal
  normalize_version INTEGER NOT NULL,-- per-row; version bumps re-mine incrementally
  content_hash TEXT,                 -- serves convo cross-path dedup (may be comma-joined multi-hash)
  chunk_total INTEGER,               -- completeness marker (#2183 rule)
  mined_at TEXT NOT NULL,
  PRIMARY KEY (wing, extract_mode, source_file)
);
CREATE TABLE watermarks (           -- dir-level fast path
  wing TEXT NOT NULL,
  extract_mode TEXT NOT NULL,
  file_count INTEGER, max_mtime REAL, total_size INTEGER, updated_at TEXT,
  PRIMARY KEY (wing, extract_mode)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- schema_version, etc.
```

**Core invariant — rows only written after full successful filing.** A row therefore *implies* completeness by construction. The subtle `chunk_total`/partial-drawer rule from `prefetch_mined_set` (#2183: a source with a mid-file partial must NOT be trusted as mined) then matters in exactly one place: the cold rebuild, which must reuse the same grouping logic (factor it so both paths agree).

### 5.2 Decision flow (convo path)

```
mine_convos(dir)
 ├─ scan_convos(dir)                                   # unchanged (C4: fast)
 ├─ manifest readable? (MEMPALACE_NO_MANIFEST=1 → legacy path)
 │   ├─ watermark == (count, max_mtime, total_size)?
 │   │    → banner + "nothing new" → EXIT              # G2: no backend open
 │   └─ warm: stale = files where row !matches
 │        (mtime, size, normalize_version == CURRENT)  # O(1) stat lookups
 ├─ stale empty → update watermark → done              # never opened collection
 ├─ cold/corrupt/schema-mismatch (one-time):
 │    → existing prefetch_mined_set + prefetch_content_hashes
 │    → rebuild rows (reuse #2183 grouping logic)
 └─ per stale file: normalize → dedup vs manifest hashes
      → file drawers → upsert row (same transaction boundary as filing)
```

- **Cold fallback = today's behavior**, paid once, then warm forever.
- NORMALIZE_VERSION bump: rows stop matching individually and re-mine; **do not** discard the manifest wholesale.

### 5.3 Projects & extract paths

- `miner.py::process_file`: pre-lock skip check reads the manifest; **post-lock re-check still queries the collection** (concurrency correctness — only runs for files that passed as new/changed, so rare). Row upsert after the existing drawer upsert, inside `mine_lock`. Early "already filed" return (collection said so on a cold manifest) records the row (mtime/size in hand).
- `format_miner.mine_formats`: **open question Q1** — audit its skip semantics first; if content-derived rather than stat-based, match its existing semantics and document the difference rather than forcing stat-matching.

### 5.4 Invalidation contract (the honest weak point)

The manifest can only lie if **drawers disappear behind its back**. Every in-repo deletion/mutation boundary must drop affected state:

| Surface | Action |
|---|---|
| `mempalace_delete_by_source` (mcp_server ~L3557) | drop row(s) for that source_file |
| `mempalace_delete_drawer` (mcp_server ~L3215) | drop the row matching that drawer's `source_file` (precise — metadata carries it) |
| `mempalace_update_drawer` | same precise row drop |
| `cmd_sync` / `sync_palace` (cli.py ~L1059) — **prunes** drawers for gitignored/deleted/moved sources (**not** mesh sync) | drop rows for pruned source_files (or whole-palace drop if per-source tracking is impractical — state the choice) |
| `repair.py`, `dedup`, `migrate` | whole-manifest drop |

Asymmetry to document in-code: mesh/logstream sync (`mempalace_sync` MCP tool) only **adds** drawers — additions never threaten manifest truth; no invalidation needed.

Escape hatches: `MEMPALACE_NO_MANIFEST=1` env + `mempalace mine --no-manifest` force the legacy path. `dry_run` may **read** the manifest but must never create/write it or its directory.

### 5.5 Concurrency

Mine-vs-mine already serializes on the palace flock (by design). Manifest writes are per-file transactions inside existing critical sections; WAL + busy_timeout covers the multi-process reality; crash mid-write rolls back → worst case re-mine (G4).

## 6. Alternatives Considered

| Alternative | Rejected because |
|---|---|
| Bulk prefetch with query-level caching only | Still O(palace) per run on big collections (C2) |
| Central cache keyed by palace hash (`~/.mempalace/cache/…`) | Splits palace identity across dirs; in-palace travels with copies, precedent exists |
| Full-checksum watermark (hash all 271MB each run) | Airtight but re-reads everything; stat-only (count/max-mtime/total-size) + per-row mtime+size match is the right cost/robustness point |
| Tail-watermark incremental re-mining of growing sessions (old "P3") | Bigger, riskier change to the purge+reinsert safety model; defer until P1 data justifies it |
| P2 subagent exclusion | Dropped by Reevon — verbatim principle wins; P1 already removes the cost |

## 7. Requirements

- **FR-1** — `convo_manifest.py` module with schema above; WAL; schema_version in `meta`.
- **FR-2** — Warm skip path for all three miners; skip predicate = row exists ∧ mtime match ∧ size match ∧ normalize_version == current.
- **FR-3** — Watermark no-op fast path exiting before backend open.
- **FR-4** — Cold rebuild via existing prefetch functions, reusing shared #2183 completeness grouping.
- **FR-5** — Row recording transactional with drawer filing (both new drawers and re-mined replaces) in all three miners.
- **FR-6** — Invalidation hooks per §5.4 table.
- **FR-7** — `--no-manifest` flag + `MEMPALACE_NO_MANIFEST=1` bypass; `dry_run` never writes.
- **FR-8** — Cross-path content-hash dedup served from manifest on warm runs (semantics identical to `prefetch_content_hashes`, incl. (wing, hash) keying and comma-joined multi-hash).
- **NFR-1** — Warm no-op mine < 1s on the CVP directory (G2). **NFR-2** — Zero additional external services; pure local SQLite (local-first). **NFR-3** — Full test suite green; ruff clean.

## 8. Test Strategy (tmp ChromaDB only — the suite is hermetic per `0727958`; never qdrant)

1. Warm skip: second mine performs zero `prefetch_*` calls (monkeypatched counters); `files_skipped == all`.
2. Watermark no-op: banner printed, collection never opened.
3. One changed file → only that file re-mined.
4. NORMALIZE_VERSION bump → only stale rows re-mine; manifest not discarded.
5. Manifest deleted mid-life → rebuild correct.
6. Crash simulation → no row for partial filing.
7. `dry_run` creates nothing; `MEMPALACE_NO_MANIFEST=1` bypasses.
8. Invalidation: delete_by_source / delete_drawer / update_drawer / sync-prune / repair each drop correct state.
9. format_miner wiring (per Q1 outcome).
10. Benchmark: synthetic ~50k-drawer palace; mine #2 (nothing changed) before/after — expect minutes → sub-second; report numbers in the PR.

## 9. Risks & Open Questions

- **Q1** — format_miner skip semantics unaudited (C6). Audit before wiring.
- **Q2** — Multi-machine: two hosts alternating mines on one palace hold separate manifests. Same-content skips stay correct; out-of-band deletions on host B are invisible to host A until an invalidating operation runs there. Accept + document as v2? (Proposed: yes.)
- **Q3** — Stat-only watermark misses mtime-preserving mutations of *individual* unchanged files; per-row mtime+size check has the same theoretical hole as today's `file_already_mined` (no regression, but reviewers should confirm).
- **R1** — Third-party/backend plugins bypassing in-repo deletion APIs can stale the cache. Mitigation: documented `--no-manifest` + rebuild; repair drops manifest.
- **R2** — Reviewers should re-measure C2 without self-contention before treating absolute page costs as targets.

## 10. Rollout

1. Land module + tests (flagged default-on; bypass available).
2. Benchmark on synthetic palace; then live-verify on CVP once its namespace/config is set (user action, pending).
3. Old "P3" (tail watermark for growing active sessions) reconsidered only with P1 production data.
