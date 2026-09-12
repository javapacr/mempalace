# BRD Review Findings — P1 Manifest Skip Cache

**Status:** Review panel output — 2026-09-01. Review-only session; **no repo files were modified** (this file is new and untracked; `docs/brd-p1-manifest-skip-cache.md` untouched).
**Method:** Four independent fresh-context review seats ran in parallel against `develop @ 0727958` and the live CVP qdrant: claims auditor (C1–C8), performance analyst (C2 re-measure, Q1 audit, NFR-1), architect (§5 decisions, invalidation completeness, concurrency), devils-advocate (premise, claims, design holes, tests, rollout).
**Panel verdict:** **Approve with conditions.** No seat calls for a redesign. The BRD's core architecture (in-palace SQLite manifest, warm-once/cold-fallback, per-row stat predicate, #2183 grouping reuse, escape hatches) survives adversarial review. But the panel found **one wrong causal claim (C2), one wrong invalidation claim (§5.4 `mempalace_sync`), two lying-row windows the design does not cover, and a rollout-order hazard** — all fixable in the BRD before implementation is approved.

---

## 1. Executive summary

1. **The problem is worse than the BRD says — and differently caused.** C2's degradation is real but is **not** "on-disk payload makes late pages disk-bound." Raw qdrant scroll paginated by cursor is **flat at any depth** (0.014s/page at offset `total−1000`). The cost comes from an **adapter bug**: `mempalace/backends/qdrant.py:1062` `get()` sets `stop_after = (offset or 0) + limit` (`:1083`), so every page call re-scrolls from the collection start via `_scroll_all` (`:769`), deserializes every row, and discards the first `offset` client-side (`:1095`). The hazard is even documented in-repo (`get_all_metadata` docstring, `:1105–1118`: "re-walk the entire collection from the start… O(n^2)"). Prefetch is therefore **O(palace²)** today (~105M row-visits per scan pass on CVP ≈ **~84 min per no-op mine, clean** — extrapolated from a linear per-row fit across three measured offsets; BRD-era contended numbers ≈ ~3h).
2. **The O(palace) conclusion survives any re-measure.** Even with the adapter fixed (flat ~0.015s/page), two full scans ≈ 458 pages × 2 ≈ **~14s per mine trigger** on CVP — still linear in palace size and still fails G2 (<1s). P1 remains justified; the adapter fix is a complementary **P0 quick win** that the BRD's §6 alternatives table never considered.
3. **The design has a dangerous failure direction it doesn't yet cover: the lying row.** A row that says "mined" when the drawers are gone causes a **permanent silent skip** — recall loss, not slowness. Two windows produce it (§4.1, §4.2 below). Today's prefetch-derived skip **self-heals** from the collection itself, so naive P1 is a regression in this one dimension.
4. **Q1 is resolved:** `format_miner.mine_formats` is stat-based (per-file mtime compare) — wire it to the manifest **as-is** with `extract_mode='format'`, preserving one sentinel nuance (§3.1).
5. **R2 is resolved:** clean re-measure done (§2, C2). Degradation persists without contention; absolute numbers soften ~1.7–1.9×.
6. **Rollout must be reordered:** benchmark before default-on; cold rebuild on CVP under the current adapter holds the flock for ~1h (§4.6).

---

## 2. Claims register adjudication (C1–C8)

| # | Verdict | Evidence & reasoning |
|---|---------|----------------------|
| C1 | **Confirmed** (live-checked) | REST count today: **457,970** points, status green (BRD snapshot: 457,952; Δ 0.004% — live palace, corroborates). Frame the number as a dated snapshot. |
| C2 | **Confirmed shape / CONTESTED mechanism** | Measurement shape matches production code exactly (`palace.py:1580–1596`, `:1635–1650`: `count()` + `get(limit=1000, offset=k·1000, include=["metadatas"])` loops). But attribution is wrong: raw qdrant scroll via `next_page_offset` is **flat** (0.015s / 0.014s / 0.014s at offsets 0 / mid / late, median of 3, no contention); wrapper-faithful replication degrades (0.016s / 5.39s / 11.10s, ≈23–24 µs/row, linear in offset). Cause = adapter re-scroll bug (`qdrant.py:1062`/`:1083` `stop_after`, `:769` `_scroll_all`, `:1095` client-side discard; documented in `get_all_metadata` docstring `:1105–1118`), not on-disk payload. Degradation **persists without contention**; clean numbers ~1.7–1.9× faster than BRD. O(palace) conclusion holds — and is understated (O(palace²) as implemented). |
| C3 | **Confirmed** | `prefetch_mined_set` at `palace.py:1532`, `prefetch_content_hashes` at `:1618`; calls at `convo_miner.py:1063` and `:1071` inside `_mine_convos_impl` (def `:1030`). Gated only on `collection is not None` (`:988–1006`); no change-detection gate; per-file mtime compare happens only after both full scans. "Regardless of how little changed" is correct. |
| C4 | **Confirmed (plausible)** | `scan_convos` (`convo_miner.py:507–586`) is pure `os.walk` + stat gates — never reads content; cost scales with file count, not bytes. 0.44–0.71s / 1319 files warm is credible (~0.4 ms/file). |
| C5 | **Confirmed with nuance** | `miner.py:1519` pre-lock `file_already_mined(check_mtime=True)` per file; `:1562–1564` post-lock re-check inside `mine_lock` only for files that passed. "Once per file, twice for changed files" matches. **Citation fix:** the ">1h of pure skip-checking" quote lives in `palace.py:1564` (`prefetch_mined_set` docstring), not the convo miner's docstring; the equivalent inline comment is `convo_miner.py:1057–1059` (">1h just deciding to skip", 2000-file sweep). |
| C6 | **Confirmed** | `cli.py:801–811` dispatches `--mode extract` → `format_miner.mine_formats` (def `format_miner.py:693`); genuinely distinct third miner. No longer "unaudited" — see §3.1. |
| C7 | **Confirmed with nuance** | `git show 0727958`: only `tests/conftest.py`, +35/−15; scrub at `conftest.py:38`. Leak mechanism statically real: `config.py:768` reads `MEMPALACE_BACKEND`; `backends/registry.py:170–198` env var = priority 3. **Scope fix:** the scrub pops exactly `MEMPALACE_BACKEND` and `MEMPALACE_BACKEND_EXPLICIT` — not "all MEMPALACE_*" (commit message overstates too). Sufficient for the described leak. "Clean run passes" is a runtime result, statically unverifiable (test exists at `tests/test_convo_miner.py:25`). |
| C8 | **Confirmed** | `qdrant.py:52` `_MARKER_FILENAME`; `:1258–1268` `_palace_hash` = sha256[palace.id](:16), prefix `mempalace[_ns]_<hash>`; `:1295–1317` + `:1400` `BackendMismatchError` on namespace mismatch. Live marker verified on disk: `~/.config/mempalace/cvp/qdrant_backend.json` = `{"namespace": null, "palace_hash": "33fd231b0fdf6acf", "remote_prefix": "mempalace_33fd231b0fdf6acf"}`. |

**New claims the panel adds:**

- **C9 (new):** qdrant adapter pagination bug makes prefetch **O(palace²)** — `qdrant.py:1062` `get()` (`stop_after = offset+limit`, `:1083`), `:769` (`_scroll_all` re-scrolls from collection start per page), `:1095` (client-side `rows[offset:]` discard); hazard documented in `get_all_metadata` docstring `:1105–1118`. This is an independent P0 fix candidate; it changes C2's cost model and §6's alternatives analysis.
- **C10 (new):** two mine invocations fire per session end, serialized on the palace flock (`hooks/mempal_precompact_hook.sh:185,192`) — the win from faster mines lands twice per session.

**Register integrity:** high. Every code anchor resolves; both live-checkable claims verify; only defects are the C5 quote misattribution, the C7 scrub-scope overstatement, and C2's mechanism attribution.

---

## 3. Open questions & risks — what the panel resolved

### Q1 — format_miner skip semantics: **RESOLVED. Wire as-is.**

Skip is **stat-based (mtime), per-file backend query** — not content-hash, not re-parse, not unconditional:

- `cli.py:801–811` dispatch; `format_miner.py:817` backend opened before the loop; `:830` reads current mtime; `:839–842` per-file `file_already_mined(collection, source_file, check_mtime=True, extract_mode="format")`; in-lock recheck `:631`.
- Predicate lives in `palace.py:1485–1489` (paginated `where={"source_file"}`), `:1505` normalize_version gate, `:1508–1513` stored-vs-current mtime compare (±0.001s; `None` ⇒ stale), `:1516–1527` #2183 chunk_total completeness — a **no-op for format mode** (its drawers carry no `chunk_total`).

**Recommendation:** wire to the FR-2 predicate (mtime+size+normalize_version, `extract_mode='format'`), keep the post-lock collection recheck. The BRD's cold-rebuild #2183 grouping is already a superset of format-mode needs.

**Mandatory nuance:** sentinel-only sources (durable SKIP statuses; sentinels at `format_miner.py:849–853` / `554–583`) carry **no `source_mtime`**, so `check_mtime=True` never matches them — **they re-extract on every mine today**. The manifest must treat NULL-mtime rows as stale to preserve this behavior exactly. (Convo miner's sentinel *does* stamp mtime — `convo_miner.py:215+` — so aligning format's sentinel is a deliberate follow-up, never a silent behavior change.)

### Q2 — multi-machine coherence: sharpened, severity **high on shared palaces**

Host B deletes via `delete_by_source` → B drops its row; host A's manifest still holds the row → A skips forever → recall loss invisible on A. Additionally, host alternation without shared rows costs a full purge+reinsert churn per swap (correct only because drawer IDs are deterministic). Panel recommendation: either a palace-wide **op epoch** in `meta` (precedent: `wal.py`) that warm runs compare, or documented mandate that cross-host deletes be followed by sync/repair on other hosts. Accept-and-document-as-v2 is defensible **only with** the C-count/UUID reconciliation from §4.1, which bounds the damage.

### Q3 — stat-only holes: sharpened, accept

Concrete vectors: `git stash`/branch checkout with preserved mtime+size, `rsync -a`, `touch -r`. Today's check is **mtime-only** (`convo_miner.py:828–847`); the manifest adds size + normalize_version — **strictly stronger than today**. Watermark collisions additionally require count+max_mtime+total_size all preserved. Low–medium; accept and document.

### R1 — out-of-band mutation: **upgraded from hypothetical to near-documented**

The BRD's own cited bug (#1722; `mcp_server.py:3557` docstring) records users hand-editing `chroma.sqlite3` directly; CVP is **remote qdrant** (C8) where any HTTP client can mutate drawers. This risk, not performance, is the strongest argument for the reconciliation guard in §4.1.

### R2 — C2 re-measure: **RESOLVED** (see §2, C2). Degradation persists clean; mechanism corrected to adapter re-scroll; do not treat BRD absolute numbers as targets

---

## 4. Design holes the panel surfaced

### 4.1 The lying row, window 1: out-of-band deletion (false-fresh) — **severity: critical**

Row present, drawers deleted behind the manifest's back → warm path skips **forever**, silently, with no detection. **Regression vs today:** the current skip set is derived *from the collection* (`prefetch_mined_set`), so out-of-band deletion self-heals on the next mine. G4's "worst case = one slow re-mine" is true only for false-*stale* (lost/corrupt manifest); false-*fresh* is the dangerous direction — e.g. 55k drawers pruned out-of-band = transcripts permanently absent from memory.
**Required fix:** store `collection.count()` (or backend collection UUID) in `meta` at write time; compare on every warm run before honoring skips (count is an O(1) metadata probe — C1 used REST count; it is NOT a payload scan). Mismatch → cold rebuild. Converts unbounded staleness into bounded. **This requires rewording G2/FR-3**: "no backend open" becomes "no paginated scans, no payload reads; at most one O(1) count probe" — the probe does open the backend. The §8 test assertion must then be "exactly one `collection.count()`, zero `get()`/prefetch calls", not "zero count calls"; without this alignment, this fix and §4.4's are mutually inconsistent. (FR-8 amplifies this hole: dedup served from stale manifest hashes also silently drops re-exports at new paths.)

### 4.2 The lying row, window 2: crash mid-re-mine (purge-then-insert) — **severity: critical**

Re-mines are **purge-then-insert**: `_file_chunks_locked` deletes a source's stale drawers *before* batched upserts (`convo_miner.py:663–693`; `miner.py:1573–1580` same shape). Crash between purge and the post-filing row upsert leaves the **old row present over deleted/partial drawers** → permanent false skip. chunk_total cannot help — the warm path never consults drawers. Row-then-drawers ordering is strictly worse.
**Required fix (FR-5 rewrite):** per-file write order = **DELETE row → purge drawers → insert drawers → INSERT row**. Every crash point then yields row-absent → re-mine, and G4 holds universally, including mid-batch crashes of multi-drawer sources (`DRAWER_UPSERT_BATCH_SIZE`; chunk_total stamped at `convo_miner.py:702`; exception cleanup `753–770`). Sentinel-only filings (`_register_file`, `convo_miner.py:1122/1132`) must also record rows, or empty files rescan forever.

### 4.3 Cold rebuild can persist a PARTIAL manifest — **severity: high**

`prefetch_mined_set` / `prefetch_content_hashes` **swallow exceptions** and return partial dicts with a warning (`palace.py:1602`, `:1672`). FR-4's cold rebuild on a mid-scan qdrant timeout would persist rows for a fraction of sources → every unlisted file re-mines next run (full purge+reinsert churn across 458k drawers), or partial hashes → duplicate drawers at new paths.
**Required fix:** any fetch exception ⇒ cold **failure**: persist nothing, fall back to legacy path with a loud warning. Extend test #6 to cover it.

### 4.4 Watermark must never be the sole "nothing new" basis — **severity: high (correctness), trivial cost to fix**

scan_convos already stats every file but discards the stats (`convo_miner.py:559–581`); re-stat is ~free (C4). All-rows-match ⟹ watermark-match, **not conversely**: rename-swaps, intra-tree moves, or size-compensating edits preserve (count, max_mtime, total_size) yet fail the path-keyed per-row check — today's prefetch path catches these, so exit-on-watermark-match would **widen** Q3 beyond current behavior.
**Required fix (§5.2 flow rewrite):** scan → per-row SQLite diff (one keyed read, ms-scale) → compute stale set **and** watermark in the same pass → exit "nothing new" **iff stale set is empty** (no payload reads; at most the one O(1) count probe from §4.1 — G2 must be reworded accordingly). Watermark demoted to banner/telemetry.

### 4.5 §5.4 invalidation table: one wrong claim, two missing boundaries — **severity: high (it's the contract)**

- **WRONG:** the table's "asymmetry" note calls `mempalace_sync` add-only mesh sync. It is **the delete-heavy pruner** (`mcp_server.py:3674`, `tool_sync`). As written, an implementer skips its hook. The genuinely add-only flows are the peer/mesh daemon threads (`mcp_server.py:7732`) and logstream's separate sqlite (`:444`).
- **MISSING:** `dedup.py:142–144` (deletes drawers, **no flock**) and `repair.py:548–591` (`prune_corrupt`, **no flock**) — both must drop manifest state atomically (schema_version bump / whole-drop) or take the flock.
- Verified accurate: `mcp_server.py:3215/3245` (delete_drawer), `:3818/3898` (update_drawer), `:3557` (delete_by_source), `cli.py:1059` + `sync.py:387` (sync prune, under flock; `removable_sources` makes per-source drops practical), `migrate.py:46` (rmtree kills manifest — in-palace placement pays off here), repair rebuilds under flock (`:1474/:2031`), exporter adds nothing, no wing/room deletion tools exist.

### 4.6 Rollout order — **severity: high**

§10 lands **default-on** (step 1) before benchmarking (step 2); CVP live-verify trails. First post-merge CVP mine pays the full cold rebuild — 2× 458k-row scans **while holding the flock** — ~84 min clean / ~3h contended under the *current* adapter, queueing every session-end mine behind it; and if §4.3 bites, it degrades to full-palace churn.
**Required fix:** benchmark first (synthetic + CVP-scale), then default-off or size-gated enablement (e.g. auto-enable only after one successful cold rebuild). Consider landing the adapter fix (C9) first — it shrinks cold-rebuild cost to ~14s/pass-pair and is independently valuable.

### 4.7 Concurrency: peer-writer interleave — **severity: medium, CVP-specific**

Mines hold the palace flock (`convo_miner.py:921`; `miner.py:1886`); MCP mutating tools hold a process-lifetime lease on the same flock (`mcp_server.py:710–790`). **But** qdrant/pgvector are multi-writer backends (`palace.py:413`) and `MEMPALACE_MCP_ALLOW_PEER_WRITER` (`mcp_server.py:418`) legalizes concurrent mine+delete **on exactly the CVP qdrant palace**: interleave mine-upsert → delete-drawers → row-drop → mine-row-write = lying row no stat change ever heals.
**Required fix:** refuse manifest writes when the flock lease isn't held (or per-mine epoch CAS). WAL + busy_timeout is sufficient *given* these guards. Also: dry-run bypasses the lock (`convo_miner.py:901–908`), so FR-7's "never writes" is load-bearing — test it.

### 4.8 Smaller holes

- **FR-8 schema friction:** a comma-joined `content_hash` column cannot serve `(wing,hash)→source` lookups without splitting all rows per run — needs a child table `hashes(wing, content_hash, source_file)` (comma itself is safe — hex).
- **G2 landmine on chroma:** post-loop `_validate_palace_fts5_after_mine` (`convo_miner.py:1219`, `palace.py:1116`) runs unconditionally on non-dry-run mines; it early-returns for non-chroma (`palace.py:1129`) so CVP is safe, but on chroma it's a `PRAGMA quick_check` (linear in DB size). Gate it on "something was mined" or G2 fails on chroma palaces.
- **Schema mismatch loop:** if cold rebuild can't write `meta` (read-only dir, perms), every mine pays cold + overhead — strictly worse than today. Must fall back to legacy path with loud warning, never loop. (NORMALIZE_VERSION itself is sound: prefetch filters `version < CURRENT` at `palace.py:1599–1601`; specify row.normalize_version mirrors *drawer* metadata so stale rows re-mine — no loop.)
- **Missing §6 alternatives:** (a) **adapter pagination fix (C9)** — the strongest unconsidered alternative; even if it doesn't meet G1/G2 long-term, it changes P1's urgency, sequencing, and the honest cost model in §2; (b) **hook debounce/coalescing** — mines are fresh processes per session end (`hooks/mempal_precompact_hook.sh` → `python -m mempalace mine`); a ~30-line debounce kills most aggregate cost with zero invalidation surface. It deserves a §6 row with a disposition, not silence. (For completeness: in-process memoization is genuinely insufficient — each mine is a new process and the prefetch already *is* the per-process memo; dir-mtime-only is unsound — in-place appends don't touch dir mtime. The stat-watermark choice survives.)

---

## 5. Architecture decision adjudication (§5)

| Decision | Verdict |
|----------|---------|
| 5.1 in-palace SQLite sidecar, WAL | **Sound.** Travels with palace copies; `qdrant_backend.json` precedent; migrate's rmtree cleans it for free. Schema fix needed: FR-8 child hash table (§4.8). |
| 5.1 rows-only-after-full-filing invariant | **Unsound as stated** — two lying-row windows (§4.1, §4.2). Sound after FR-5 ordering rewrite + warm-run reconciliation. |
| 5.2 watermark stat-only fast path | **Sound-with-fix** — per-row diff decides; watermark demoted (§4.4). |
| 5.2 NORMALIZE_VERSION per-row | **Sound** (no rebuild loop; row version mirrors drawer metadata). |
| 5.2 cold fallback | **Sound-with-fix** — partial-fetch ⇒ persist nothing (§4.3); single-source the grouping scanner; note prefetch keys groups by source only (`palace.py:1567`) while manifest PK adds wing (cross-wing re-file is deliberate) — rebuild must replicate the last-write-wins chunk_total nuance (`:1592–1596`) exactly. |
| 5.3 projects path (post-lock collection recheck) | **Sound.** format_miner wiring per Q1: as-is + NULL-mtime-stale nuance. |
| 5.4 invalidation contract | **Unsound as documented** — `mempalace_sync` mislabel + 2 missing boundaries (§4.5). Completeness otherwise verified. |
| 5.5 concurrency | **Sound-with-caveats** — peer-writer guard required (§4.7); WAL+busy_timeout sufficient given guards. |
| FR-7 escape hatches / dry-run read-only | **Sound**; load-bearing — test that dry_run never creates the manifest dir. |
| §6 alternatives table | **Incomplete** — missing adapter fix + debounce (§4.8); full-checksum rejection reasoning stands. |
| §8 test strategy | **Gaps:** no "the lie" negative test (row present + collection empty — would fail today, proving §4.1); no partial-fetch test; no schema-migration/mismatch test; no mine-vs-delete race test; no sentinel/NULL-mtime semantics test; benchmark is 50k synthetic chroma vs 458k real remote qdrant (extrapolates poorly); "zero prefetch calls" should assert exactly one `collection.count()` probe and zero paginated `get()` calls (aligned with the §4.1 reconciliation). |
| §10 rollout | **Reorder** (§4.6). |

---

## 6. Must-fix list before implementation approval

1. **FR-5 rewrite — per-file ordering:** DELETE row → purge drawers → insert drawers → INSERT row; sentinel filings also record rows. *(§4.2)*
2. **Warm-run reconciliation:** `collection.count()` (or collection UUID) in `meta`, compared every warm run; mismatch → cold rebuild. **Requires rewording G2/FR-3** to "no paginated scans / no payload reads; at most one O(1) count probe" and aligning the §8 test assertion (exactly one `count()`, zero `get()`). *(§4.1 — bounds the false-fresh lie; makes Q2's accept-as-v2 defensible)*
3. **Cold rebuild atomicity:** any prefetch exception/partial ⇒ persist nothing; legacy fallback + loud warning. *(§4.3)*
4. **Decision-flow rewrite:** per-row SQLite diff is the skip decision; watermark never the sole "nothing new" basis; no payload reads on the no-op path (at most the single O(1) count probe). *(§4.4)*
5. **§5.4 rewrite:** fix `mempalace_sync` (it's the pruner, `mcp_server.py:3674`); add flock-less `dedup` + `prune_corrupt` boundaries; peer-writer guard — no manifest writes without the flock lease/epoch. *(§4.5, §4.7)*
6. **Rollout reorder:** benchmark (synthetic **and** CVP-scale) before default-on; default-off or size-gated enablement; consider C9 adapter fix as PR-0. *(§4.6)*
7. **§6 additions with dispositions:** adapter pagination fix (C9); hook debounce/coalescing. *(§4.8)*
8. **FR-8 schema:** child hash table `(wing, content_hash, source_file)` instead of comma-joined column. *(§4.8)*
9. **FTS5 validation gate:** skip `_validate_palace_fts5_after_mine` when nothing was mined (chroma G2). *(§4.8)*
10. **Test additions:** the-lie negative; partial-fetch; schema migration/mismatch; mine-vs-delete race; sentinel/NULL-mtime; exactly-one-count-probe + zero-get assertion; dry-run-never-writes. *(§5)*
11. **Citation repairs in §2:** C5 quote → `palace.py:1564` (+ `convo_miner.py:1057–1059`); C7 scope → two env vars; C1 → dated-snapshot framing; C2 → rewrite mechanism per panel finding.

---

## 7. What survived attack (fair adjudication)

- In-palace placement; warm-once/cold-fallback architecture; per-row mtime+size predicate (strictly stronger than today's mtime-only).
- **False-stale safety:** purge-before-insert verified in both miners → re-mines never duplicate drawers; G4 genuinely holds in that direction.
- Watermark-vs-full-checksum cost/robustness tradeoff; rejection of in-process memoization and dir-mtime-only alternatives.
- #2183 completeness-grouping factorization (necessary and correct); FR-7 escape hatches; claims register integrity (C1–C8 anchors all real).
- P1's premise itself: skip-check dominates wall time today (~84 min/no-op mine clean on CVP vs a 0.7s directory scan), and even post-adapter-fix ~14s/trigger fails G1/G2. The manifest remains the right end-state.

---

## 8. Panel artifacts

Seat reports (full text): claims auditor `9fc4d634…/run-0`, performance `8da90e78…/run-0`, architect `f0b20027…/run-0`, devils-advocate `c879c8be…/run-0` (session logs under `~/.pi/personal/sessions/subagent/`). Re-measure scratch scripts under `~/.pi/tmp`. Live qdrant touched read-only (count + scroll timing).
