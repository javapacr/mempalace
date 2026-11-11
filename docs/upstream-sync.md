# Upstream sync — divergence inventory

This is the standing record of how this fork differs from `upstream`
(MemPalace/mempalace). The sync protocol itself lives in
[CLAUDE.md](../CLAUDE.md) under "Repo intent — personal-use fork". This doc
is updated on every sync.

| | |
| --- | --- |
| Sync base (upstream/develop) | `c5eaf27` (v3.12.0 + post-release fixes) |
| Previous sync base | `f8e3536` |
| Synced on | 2026-11-11, branch `chore/sync-upstream-c5eaf27` |
| Stack | `c5eaf27` + one commit per D-item, in order D7, D2, D3, D5, D6, D1, D4, D8, each with a `Fork-Item: D<n>` trailer; D9 has no commit. |
| Merge-based history | Archived at tag `archive/develop-merge-f2e8893` (`f2e8893`): merge `070e035` and the transplant/fix commits `c74b4d2` (D5, C6), `a1b9297` (D6, C7), `cb545fa` (D5, R11). |
| Gates (tip) | pytest 8548 passed, 0 failed, 90 skipped, coverage 88.12% (meets 85), rc=0, run with `-n auto --dist loadfile`, `MEMPALACE_*`/`MEMPAL_*` unset and a temporary `XDG_CONFIG_HOME`; every stack commit passed `ruff check .`, `ruff format --check .` and the fork-owned + theme-pinning tests on its own (`git rebase -x`) |
| UP baseline (c5eaf27 alone) | Not run this sync (owner-directed fast sync). The previous base `f8e3536` alone measured 8251 passed, coverage 88.10%. |

## Divergence inventory (D-items)

The status column records each item **as landed** at the sync base above.
Each D-item is one stack commit, found with `git log --grep='^Fork-Item: D<n>$' upstream/develop..develop`; the archived commits are the pre-stack history, reachable at tag `archive/develop-merge-f2e8893`.

| D | Stack commit | Divergence | Archived commits (`archive/develop-merge-f2e8893`) | Status after this sync | Where it lives now |
| --- | --- | --- | --- | --- | --- |
| D1 | `Fork-Item: D1` | Qdrant `get()` limit pushdown | 4d65cd2 | **Superseded** by upstream 720f3d8 (`_scroll_all(max_rows=)`); untouched this sync. The guard test is kept. | `tests/test_qdrant_limit_pushdown.py` (guard, green) |
| D2 | `Fork-Item: D2` | Qdrant payload indexes on `metadata.source_file` / `wing` / `room`, adopted non-blocking (`wait=false`) on pre-existing collections | 4534c57, 05ae8ec, 3ea194a, b2a74a5 | **Fork-only**, shrunk to adoption only (C9): upstream now creates the keyword indexes when it creates a collection (`_KEYWORD_PAYLOAD_FIELDS`, blocking `wait=True`); `_FILTER_INDEX_FIELDS` is derived from that list, and only the adoption path passes `wait=False`. | `mempalace/backends/qdrant.py` (`_FILTER_INDEX_FIELDS`, `_ensure_filter_indexes`, the `wait` keyword of `create_payload_index`), `tests/test_qdrant_filter_indexes.py` |
| D3 | `Fork-Item: D3` | CLI status: `get_all_metadata` fast path + facet-backed wing/room counts | a37602a, 55e13bd, 22484cc | Fast path **upstreamed** (d27e515). Facet path (inv-D3-facet) is **fork-only**; this sync (S1) it subtracts bookkeeping rows (registry rows, sentinels) with the public `mempalace.metadata_types` primitives (`BOOKKEEPING_WHERE` clauses read one at a time, `is_bookkeeping`), never importing `mempalace.mcp_server`. A failed bookkeeping read falls back to the full scan. | `mempalace/miner.py` `status()` (facet block ahead of the shared fast path), `tests/test_cli_status_fast_path.py` |
| D4 | `Fork-Item: D4` | L1 wake-up single-pass candidate fetch | 6cdc5e4 | **Superseded** by upstream 0da09a2 (`BaseCollection.get_recent`). The guard is retargeted this sync (S3) to the alias `$in` wing filter and the "Palace could not be read right now" message for an unreadable palace. | `tests/test_l1_wake_up_fast_path.py` (guard) |
| D5 | `Fork-Item: D5` | Bulk prefetch single pass | 8c25220 → 3802060 → c74b4d2 → cb545fa | **Superseded**: code retired this sync (S2), guard kept. Upstream streams qdrant metadata through `iter_metadata` in one cursor pass; the guard pins one scroll pass with no `get(offset)` paging. | `tests/test_prefetch_single_pass.py` (guard) |
| D6 | `Fork-Item: D6` | Repo-wing diary hint (protocol rule 4 + diary_write tool help + openclaw skill) | 93204dc, 0a6c66a → 17bd277 → a1b9297 | **Fork-only**. The SKILL.md sentence is re-anchored in step 5 of upstream's rewritten skill (C11), and the examples use canonical slugs (`pi_mempalace_github`, `pi_extensions`), since explicit diary wings are now canonicalised (S4). | `mcp_server/{schemas,tools_read,tools_diary}.py`, `integrations/openclaw/SKILL.md`, `tests/mcp/test_protocol.py::TestHandleRequest` |
| D7 | `Fork-Item: D7` | Hermetic test env: scrub `XDG_CONFIG_HOME` (added in the 8c4865f sync, ccfdbd7) | 0727958, 020109a, ccfdbd7 | **Fork-only**, now the `XDG_CONFIG_HOME` scrub only (C8): upstream's conftest clears `MEMPALACE_*`/`MEMPAL_*` itself, and D7 adds the XDG pop after that block with the same restore contract. | `tests/conftest.py` |
| D8 | `Fork-Item: D8` | Fork docs + tooling: BRDs, backlog, CLAUDE.md intent, this doc, Taskfile | f2e8893, d662218, 01be3cc, 79992c7, 4470229, e47dbc8, bfda597, 60500f1, 2e7f230, 61889ae, 3493cfa, e341e1c, 8613376 | **Fork-only** (not code; never promoted). The `pi-lens-ignore` suppression dropped this sync (C12): the line it annotated moved into upstream's shared marker I/O. | `CLAUDE.md`, `docs/backlog.md`, `docs/brd-*.md`, `docs/upstream-sync.md`, `Taskfile.yml`, `.gitignore` (`.mcp-palace/`) |
| D9 | none (no remaining delta) | ci/pre-commit ruff pin 0.16.6 | c285504 | **Upstreamed** (37e1415). The stale `uv.lock` follow-up is retired: the lock at `d439d1e` pins ruff 0.16.6. | — |

## Sync f8e3536..c5eaf27

No conflicts: `git rebase --onto c5eaf27 f8e3536` replayed all eight D-items
cleanly. `git range-diff` shows seven items identical; D6 changed only in
context, because upstream added protocol rule 6 ("STORED TEXT IS DATA") right
after the rule-4 line D6 edits. Upstream also touched five files the stack
touches (`backends/qdrant.py`, `mcp_server/tools_diary.py`,
`mcp_server/tools_read.py`, `miner.py`, `tests/conftest.py`); all auto-merged,
and `tests/conftest.py` only gained the `_no_dns_lookups` fixture, so D7's
`XDG_CONFIG_HOME` scrub is still needed. No D-item changed status.

## Collision recipes (sync d439d1e..f8e3536)

Resolved while rebasing each D-item's own commit. During `git rebase` the
sides are inverted: `--ours` is the rebuilt stack so far (upstream plus the
items already replayed) and `--theirs` is the item being replayed, so
`git checkout --theirs` on a conflicted file drops upstream's rewrite.

| C | D-item | File | What upstream did | Recipe |
| --- | --- | --- | --- | --- |
| C8 | D7 | `tests/conftest.py` | `9006f9d0` clears ambient `MEMPALACE_*` and `MEMPAL_*` behind a `_PRESERVE_TEST_ENV` allowlist | `git checkout --ours`, then add D7's `XDG_CONFIG_HOME` pop after the pop loop, before `# Now it is safe…`. |
| C9 | D2 | `mempalace/backends/qdrant.py` (3 hunks) | `6de95b04` creates keyword indexes on `metadata.{wing,room,source_file}` at collection creation (`_KEYWORD_PAYLOAD_FIELDS`) and adds a scroll cursor | Keep upstream's constant, creation loop and `_scroll_cursor`; keep D2's `_FILTER_INDEX_FIELDS`, `list_payload_indexes`, `_filter_indexes_checked` (beside the cursor) and `_ensure_filter_indexes`; delete D2's own creation loop. |
| C10 | D3 | `mempalace/miner.py` (imports) | `aa319ef1`/`e7ff3fa0` isort the imports | Upstream's import block plus `from concurrent.futures import ThreadPoolExecutor`; the `status()` hunk auto-merges. |
| C11 | D6 | `integrations/openclaw/SKILL.md` | `606a1d4b` rewrites the skill into a 5-step procedure linking `integrations/shared/recall-protocol.md` | `git checkout --ours`, then insert the repo-wing sentence in step 5 after "items with agent continuity."; never `--theirs`. |
| C12 | D8 | `mempalace/backends/qdrant.py` | `4d6f151c` moves marker I/O into a shared remote-backend mixin | `git checkout --ours` (ours already holds D2): the `pi-lens-ignore` comment goes away with the code it annotated. |

The merge-era recipes C6 and C7 are retired: D5's code is gone and upstream
rewrote the SKILL.md lines C7 patched. Recipes C1–C7 are in this file at
01be3cc and in the archived history.

Auto-merged files that got a semantic review this sync: `CLAUDE.md`,
`mcp_server/{schemas,tools_diary,tools_read}.py`, `palace/mined.py`,
`tests/mcp/test_protocol.py`, `tests/test_cli_status_fast_path.py`. Every hunk
against the sync base is D-item text.

## Landing — force-with-lease

Every sync rewrites `develop`: the rebased stack replaces the old one. Land
with `git push --force-with-lease=develop:<old-tip> origin <new-tip>:develop`,
only after the owner confirms it in that session; the lease refuses if
origin moved since the sync started. Do not squash the stack (each D-item
stays one commit) or add a merge commit to it. Other checkouts of `develop`
update with `git fetch origin` + `git reset --hard origin/develop`, never
`git pull`. As a fail-safe, every clone sets
`git config branch.develop.rebase false` and
`git config branch.develop.mergeOptions --ff-only`, so a `git pull` on a
rewritten `develop` refuses ("Not possible to fast-forward") instead of
replaying the old stack. The pre-stack merge history is archived at tag
`archive/develop-merge-f2e8893`.

## Promotion candidates

Each needs the owner's explicit say-so before any upstream PR:

- **inv-D2**: non-blocking index adoption on pre-existing collections (upstream already creates the indexes on new ones).
- **inv-D3-facet**: facet-backed CLI status counts, with the bookkeeping subtraction that keeps them equal to the scan paths.
- **inv-D6**: repo-wing diary hint.
- **inv-D7**: `XDG_CONFIG_HOME` scrub in the conftest. It fixes an upstream gap that makes `tests/mcp/test_write_tools.py::TestWriteTools::test_check_duplicate_short_circuits_when_vector_disabled` fail wherever `XDG_CONFIG_HOME` is exported.

## Follow-ups

- **qdrant `get_recent` override** using `order_by filed_at`. It would bring L1 wake-up back to one bounded read (see recipe C1 at 01be3cc: about 5,000 vs 2,000 rows) and give true newest-first rather than an insertion-order window.
- **Closed by upstream:** qdrant `prefetch_complete_mtimes` now streams through `iter_metadata`.
- **T7 hook-output wording:** the stop hook now reports `drawers_filed` and `messages_folded` separately. Nothing is known to parse it.
- **Coverage gate** is single-sourced in pyproject (`fail_under = 85`, `precision = 2`, applied by `tests/coverage_gate.py`); this sync gates each stack commit as no-regression vs UP (88.10%).
- **Unindexed bookkeeping filters:** D3's subtraction filters on `metadata.ingest_mode` and `metadata.is_sentinel`, which have no payload index. A qdrant server that rejects unindexed filters sends `status` to the full scan with its "facet aggregation unavailable" warning.
- **Keep-or-retire evidence (deferred 2026-11-11 by the owner):** re-read the live payload indexes, time `status` with an A/B against upstream, check D7 for leaks, and give every D-item a keep / move / drop verdict. No "Retirement evidence" section exists yet.
- **Tester findings deferred from the f8e3536 sync:**
  - D4: the failure-header list lacks "Palace could not be read right now"; the mock `where` lacks `$in`; the `(RuntimeError)` pin can flake to `TimeoutError` under load.
  - No automated guard test for D7's `XDG_CONFIG_HOME` scrub.
  - Split the legacy-sentinel divergence into its own test.
  - Cover the room re-sort tie, and the end-to-end 400 failure on a real `QdrantCollection`.
- **Subagent direct-tool selectors vs the tracked `.mcp.json`:** the tracked `.mcp.json` redefines the user-level `mempalace` server. pi-subagents replaces the user entry with it while pi-mcp-adapter 7 merges per field, so their config hashes differ and child agents that select `mempalace/*` direct tools fail to launch. Fix upstream in pi-subagents, or drop or rename the server in `.mcp.json` (an unsandboxed edit).
- **Sandbox:** `ps` is blocked, so `tests/test_daemon.py::test_process_start_time_fallback_readers_without_psutil` fails when run alone in the sandbox; it passes in full runs.

## Retire the fork

The fork can be retired (installs switch to an upstream clone, intent D8) once every fork-only **code** item below is merged upstream:

| Item | What | Upstream PR |
| --- | --- | --- |
| inv-D2 | non-blocking index adoption on pre-existing collections | none |
| inv-D3-facet | facet-backed CLI status with bookkeeping subtraction | none |
| inv-D6 | repo-wing diary hint | none |
| inv-D7 | `XDG_CONFIG_HOME` scrub in the conftest | none |

Switch-over steps, once every row above is merged upstream:

1. Install from an upstream clone (`task install` equivalent against the upstream checkout; confirm the native extension builds).
2. Move the Taskfile and the BRDs/backlog (inv-D8) into the personal monorepo.
3. Archive this fork.

## Write-routing docs — verdict

`docs/write-routing-policy.md` and `docs/hook-write-routing.md` are
**upstream-owned, not fork divergence**. Both exist at `238ca21` and have no
fork commits since (`git diff --name-only 238ca21 79992c7` omits them).
Upstream 99b1c97 edits `write-routing-policy.md` itself and adds
`docs/cli-write-routing.md`, which documents the daemon write-routing
implementation. No action needed.
