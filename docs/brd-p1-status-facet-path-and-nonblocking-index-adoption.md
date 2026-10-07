# BRD — P1: Non-blocking index adoption + facet-backed CLI status

**Status:** Draft v0.1 (for review — no implementation exists yet)
**Scope decision:** P1 only. Both items fall out of the 2026-09-01 qdrant payload-index incident RCA; they are the two *code* actions remaining after the ops-level mitigation (Colima VM resize, recorded here as deployment context, not a code requirement).
**Repo state at writing:** `develop` @ `8613376` (`chore(lens)` suppress; the payload-index pair `4534c57` + `05ae8ec` and the C9 single-pass prefetch `8c25220` are already in).

---

## 1. Problem Statement

Two independent scaling defects on hot paths, both surfaced by the 2026-09-01 incident:

**A. Index adoption blocks.** `QdrantCollection`'s open-path ensure (`4534c57`, repaired by `05ae8ec`) creates missing payload indexes via `create_payload_index()` with `query={"wait": "true"}` (qdrant.py:444). The commit's own design states "index builds run asynchronously server-side; any listing/creation failure degrades to unindexed scans with a warning — never a failed mine." `wait=true` contradicts that: the API call blocks until the build completes. On the CVP drawers collection (457,970 points) the build takes minutes against a **10 s** client timeout (`MEMPALACE_QDRANT_TIMEOUT`, default 10.0, config.py:808) — every *first adoption* open times out client-side while the server keeps building, and the caller's next request (e.g. `status` → `get_all_metadata`) then contends with the in-flight build. This is exactly the traceback Reevon hit on `mempalace status`.

**B. CLI status transfers the whole palace.** `mempalace status` (`miner.py:status` → `get_all()` → `get_all_metadata()`) scrolls **every point's full payload** (verbatim drawer text + metadata) to compute wing/room drawer counts: 14.3 s wall on CVP (457,970 drawers), 3.96 s on personal (100,596) on a warm server. The MCP server does not have this problem — its wing/room listings already use server-side `facet_counts` (mcp_server.py:2220, 2316, 2413–2418). CLI status predates that pattern and never adopted it.

## 2. Measured Evidence (2026-09-01, live palaces + incident timeline)

| # | Claim | Measurement / Source |
| --- | ----- | ----- |
| E1 | CLI status is payload-only but whole-palace: CVP 14.3 s / 457,970 drawers, personal 3.96 s / 100,596 drawers, single pass, warm idle server | `time mempalace status` / `MEMPALACE_QDRANT_NAMESPACE=cvp mempalace --palace ~/.config/mempalace/cvp status`, post-incident |
| E2 | Status requests **no vectors** — the transfer is pure payload (verbatim text): `scroll_points` body `"with_vector": bool(with_vector)` (qdrant.py:534); `_rows(with_vector: bool = False)` (qdrant.py:895); `get_all_metadata` passes no vector arg. Storm pages seen at ~5.8 MB each are payload text, by design (verbatim storage) | code trace, incident logs |
| E3 | MCP wing/room listings use server-side `facet_counts`; CLI status does not | `mempalace/mcp_server.py:2220, 2229, 2316, 2367, 2371, 2413, 2418` vs `mempalace/miner.py:2236, :2273` |
| E4 | `create_payload_index` uses `wait=true` (qdrant.py:444) while the design doc in `4534c57` promises async adoption — intent vs implementation mismatch; combined with 10 s default timeout, every first-adoption open on a large collection client-times out | code + commit message + incident traceback (`miner.py status → get_all → … → scroll_points → TimeoutError`) |
| E5 | Index builds on a 2 vCPU / 4 GiB Colima VM thrash: block reads 435 GB → 1.9 TB (~1 GB/s sustained) at 11% CPU with payload-schema counters frozen at ~99.95% built for 35+ min; after resize to 4 vCPU / 8 GiB the same builds resumed (counters preserved across restart) and converged — CPU 71% during build, then idle 3%, total block reads 78.7 MB | docker stats / collection-info snapshots before & after `colima stop && colima start --cpu 4 --memory 8` |
| E6 | `grey` collection status is a latch, not a fault: reads/filters/status work while grey; it clears on the next write-driven optimizer tick (observed live: personal drawers 100,596 → 100,598 points on a small mine → green) | live collection-info polling |
| E7 | Tail segments below qdrant's `indexing_threshold` never get payload indexes by design (~20–280 points short of totals in `payload_schema.points`) — not a defect, irrelevant to green | collection info payloads |
| E8 | `_FILTER_INDEX_FIELDS` currently covers `metadata.source_file` (keyword) + `document` (text) only; `wing`/`room` are unindexed, so facet calls on them scan server-side | qdrant.py ensure block (~L745–845); collection payload_schema |
| E9 | `facet_counts` rejects local-only filters (`UnsupportedCapabilityError`) and requires the qdrant facet endpoint — backends without it must fall back | qdrant.py `facet_counts` (~L1213); wrapper comment re pgvector `with_document=False` fast path (#1892) |
| E10 | Palace→collection mapping: personal = `~/.config/mempalace/palace` → `mempalace_personal_d5d5e1509bf58474_*`; CVP = `~/.config/mempalace/cvp` → `mempalace_cvp_33fd231b0fdf6acf_*`; namespace must match the palace's `qdrant_backend.json` marker (`MEMPALACE_QDRANT_NAMESPACE=cvp` for CVP) or `BackendMismatchError` fires by design | marker files, live |

> Reviewers: attack E1 and E5 first. E1's CVP 14.3 s was measured on an otherwise-idle server with warm page cache; cold numbers are worse. E5's "converged" claim rests on one before/after pair — the mechanism (working set > VM RAM → mmap thrash) is the part to challenge.

## 3. Goals

- **G1** — First payload-index adoption never blocks an open, on any collection size, on any timeout setting. A failed/slow index build must degrade to today's unindexed behavior with a warning — never fail the caller.
- **G2** — CLI `status` cost scales with **number of wings/rooms**, never with drawer count. CVP status target: ≤2 s warm (from 14.3 s), identical output format.
- **G3** — No behavior change for MCP paths, miners, or the wake-up stack. Pure data-source swap under CLI status; pure flag change under index creation.

## 4. Non-Goals

- No change to verbatim payload storage or drawer content layout (E2 is by design).
- No new retry/backoff machinery around index builds; the qdrant server remains the once-only memory (payload_schema listing stays the source of truth for "already ensured").
- No ops-side requirements (Colima sizing, Docker settings) codified in code — recorded here as deployment context only.
- Cleanup of the ~50 stale collections on the live server — separate housekeeping task.

## 5. Proposed Design

### 5.1 `wait=false` index creation (R1)

`_QdrantRESTClient.create_payload_index` (qdrant.py:439) switches `query={"wait": "true"}` → `query={"wait": "false"}`. Semantics: the PUT returns once accepted; the build proceeds server-side; `list_payload_indexes()` (payload_schema) reports the field immediately, so the once-per-collection ensure never re-creates. The 400/409 skip path (already-indexed races) is unchanged. All existing call sites (open-path ensure, birth-path creation for new collections) inherit the fix — no signature changes.

**Compatibility note:** with `wait=false`, the first search/mine scroll after adoption on a huge collection may run unindexed while the build is in flight (grey). That is precisely today's steady state pre-adoption, bounded by build duration, and it no longer poisons the *open* that triggered it.

### 5.2 Facet-backed CLI status (R2)

`miner.py:status()` replaces `get_all()` with the MCP's aggregation pattern (mcp_server.py:2413–2418): `facet_counts("wing")` for wing totals, `facet_counts("room", where={"wing": w})` per wing (bounded thread pool, same as MCP), `count()` for the header total. Output format byte-identical to today. Fallback: any `UnsupportedCapabilityError`/endpoint-miss from the backend → existing `get_all()` path (chroma and future backends keep working; E9).

### 5.3 Optional: `wing`/`room` keyword indexes (R3, separate commit)

Add `metadata.wing` / `metadata.room` (keyword) to `_FILTER_INDEX_FIELDS`. Facet calls then hit the index instead of scanning. With R1 in place the adoption cost is one extra async build per collection, invisible to callers. Without R3, facets still win (server-side aggregation, no payload transfer) — R3 turns "wins" into "ms".

## 6. Alternatives Considered

- **Raise the default timeout instead of `wait=false`** — rejected: moves the cliff, doesn't remove it; any sufficiently large collection outlives any constant.
- **Drop the open-path ensure entirely, create indexes only in the mine/write path** — rejected: a fully-warm read-only palace never upserts, so indexes would never be adopted (the original `4534c57` rationale stands; only the wait flag was wrong).
- **Cache status output on disk with TTL** — rejected for P1: adds a staleness surface; facet counts are cheap enough not to need it. Revisit only if facet fallback backends dominate.
- **Status over gRPC for smaller pages** — rejected: complexity, and the cost is payload transfer itself, not transport framing.

## 7. Requirements

- **R1** — `create_payload_index` adopts `wait=false`; behavior covered by a regression test proving the request carries `wait=false` and that an already-indexed race (409) still skips cleanly. Follows the captured-live-response test pattern from `05ae8ec` so an envelope/endpoint assumption can't pass CI while wrong.
- **R2** — CLI `status()` uses facet counts + count; identical printed output; graceful fallback to `get_all()` on `UnsupportedCapabilityError` or endpoint miss; tested hermetically (tmp ChromaDB per `0727958` — never the real palaces).
- **R3** — *(optional, own commit)* `wing`/`room` keyword indexes in `_FILTER_INDEX_FIELDS`; ensure stays once-per-collection.
- **R4** — Conventional commits: `fix(qdrant): adopt payload indexes without blocking (wait=false)`; `perf(status): facet-backed wing/room counts for CLI status`; `perf(qdrant): keyword indexes on wing/room for facet listings`.

## 8. Test Strategy (tmp ChromaDB only — the suite is hermetic per `0727958`; never qdrant)

- Unit: request-shape assertions on the REST client (wait flag, body shape), using the captured-response harness from `tests/test_qdrant_filter_indexes.py`.
- Unit: status output equality — same wings/rooms/counts rendered from facet data as from the legacy scan path (golden fixture).
- Fallback: facet-raising backend stub → status falls back to `get_all()` and still renders correctly.
- Full suite: `uv run pytest tests/ -v --ignore=tests/benchmarks` plus `ruff check` / `ruff format --check`.

## 9. Risks & Open Questions

- **Facet endpoint availability** — older qdrant versions lack `/facet`; E9's capability error must be proven to map to a clean fallback, not a warning loop. Verify against the deployed 1.18.3 (endpoint exists) and one <1.14 mock.
- **Facet without R3 indexes** — server-side scan per facet call (1 + n_wings calls). On CVP: ~50 wings → ~50 scans per status. Acceptable only as a bridge; R3 should land close behind R2 or CVP status may not actually improve until then. *Open question: is `status` frequency high enough to care, or should R2 wait for R3?*
- **`wait=false` observability** — a build can now silently lag adoption; the warning path no longer fires because nothing fails. Consider one `logger.debug` when payload_schema reports a field that was just created (cheap, helps incident RCA like today's).
- **Operator expectations** — post-R1, `grey` after first adoption is *normal* (E6). Document in the ensure docstring so the next RCA doesn't re-derive it.

## 10. Rollout

1. `fix(qdrant)` R1 — smallest blast radius, unblocks large-collection users immediately.
2. `perf(qdrant)` R3 — before R2 if the open question resolves as "R2 needs indexes"; otherwise independent.
3. `perf(status)` R2 — CLI-only, fallback-guarded.
4. Deployment note for operators (README or docs): palace-scale qdrant wants **≥8 GiB VM RAM** for index adoption on ≥100k-point collections (E5); `grey` during builds is expected.
