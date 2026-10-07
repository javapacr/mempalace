# Upstream sync — divergence inventory

This is the standing record of how this fork differs from `upstream`
(MemPalace/mempalace). The sync protocol itself lives in
[CLAUDE.md](../CLAUDE.md) under "Repo intent — personal-use fork". This doc
is updated on every sync.

| | |
| --- | --- |
| Sync base (upstream/develop) | `d439d1e` (v3.11.0+) |
| Previous sync base | `8c4865f` |
| Synced on | 2026-10-07, branch `chore/sync-upstream-d439d1e` |
| Stack | `d439d1e` + one commit per D-item, in order D7, D2, D3, D5, D6, D1, D4, D8, each with a `Fork-Item: D<n>` trailer; D9 has no commit. Restacked 2026-10-07, branch `refactor/fork-patch-stack`. |
| Merge-based history | Archived at tag `archive/develop-merge-f2e8893` (`f2e8893`): merge `070e035` and the transplant/fix commits `c74b4d2` (D5, C6), `a1b9297` (D6, C7), `cb545fa` (D5, R11). |
| Gates (W1 tree) | pytest 0 failed, coverage 83.99% (floor 83.43%, intent decision D7), rc=0; `ruff check .` rc=0; `ruff format --check .` rc=0 |
| Gates (final tree, after `cb545fa`) | pytest 6278 passed, 0 failed, coverage 83.98%, rc=0; `ruff check .` rc=0; `ruff format --check .` rc=0 |

## Divergence inventory (D-items)

The status column records each item **as landed** at the sync base above.
Each D-item is one stack commit, found with `git log --grep='^Fork-Item: D<n>$' upstream/develop..develop`; the archived commits are the pre-stack history, reachable at tag `archive/develop-merge-f2e8893`.

| D | Stack commit | Divergence | Archived commits (`archive/develop-merge-f2e8893`) | Status after this sync | Where it lives now |
| --- | --- | --- | --- | --- | --- |
| D1 | `Fork-Item: D1` | Qdrant `get()` limit pushdown | 4d65cd2 | **Superseded** by upstream 720f3d8 (`_scroll_all(max_rows=)`); untouched this sync. The guard test is kept. | `tests/test_qdrant_limit_pushdown.py` (guard, green) |
| D2 | `Fork-Item: D2` | Qdrant payload indexes on `metadata.source_file` / `wing` / `room`, adopted non-blocking (`wait=false`) | 4534c57, 05ae8ec, 3ea194a, b2a74a5 | **Fork-only**, untouched this sync. | `mempalace/backends/qdrant.py` (`_FILTER_INDEX_FIELDS`, `_ensure_filter_indexes`, `create_payload_index`), `tests/test_qdrant_filter_indexes.py` |
| D3 | `Fork-Item: D3` | CLI status: `get_all_metadata` fast path + facet-backed wing/room counts | a37602a, 55e13bd, 22484cc | Fast path **upstreamed** (d27e515). Facet path (inv-D3-facet) is **fork-only**, untouched this sync. | `mempalace/miner.py` `status()` (facet block ahead of the shared fast path), `tests/test_cli_status_fast_path.py` |
| D4 | `Fork-Item: D4` | L1 wake-up single-pass candidate fetch | 6cdc5e4 | **Superseded** by upstream 0da09a2 (`BaseCollection.get_recent`); untouched this sync. The guard is retargeted to the `get_recent` contract. | `tests/test_l1_wake_up_fast_path.py` (guard) |
| D5 | `Fork-Item: D5` | Bulk prefetch single pass (C9): `prefetch_mined_set` / `prefetch_content_hashes` via `get_all_metadata` | 8c25220 → 3802060 → c74b4d2 → cb545fa | **Fork-only**. Re-applied (C6) under upstream's `_scan_collection_metadata`: the non-Chroma fallback `_paged_metadata` is one `_iter_all_metadata` pass, and Chroma uses upstream's sqlite stream; if that stream fails, Chroma pages exactly as upstream does (`cb545fa`, R11), so D5 changes only the non-Chroma path. Contract adopted on qdrant: a failed registry read raises `MinedSetUnavailable` instead of returning a partial dict. | `mempalace/palace/mined.py`, `tests/test_prefetch_single_pass.py` |
| D6 | `Fork-Item: D6` | Repo-wing diary hint (protocol rule 4 + diary_write tool help) | 93204dc, 0a6c66a → 17bd277 → a1b9297 | **Fork-only**, intact after C7. Its tests moved to `tests/mcp/test_protocol.py`. | `mcp_server/{schemas,tools_read,tools_diary}.py`, `integrations/openclaw/SKILL.md`, `tests/mcp/test_protocol.py::TestHandleRequest` |
| D7 | `Fork-Item: D7` | Hermetic test env: scrub every `MEMPALACE_*` var, plus `XDG_CONFIG_HOME` (added in the 8c4865f sync, ccfdbd7) | 0727958, 020109a, ccfdbd7 | **Fork-only**, untouched this sync. | `tests/conftest.py` |
| D8 | `Fork-Item: D8` | Fork docs + tooling: BRDs, backlog, CLAUDE.md intent, this doc, Taskfile, lens suppression | f2e8893, d662218, 01be3cc, 79992c7, 4470229, e47dbc8, bfda597, 60500f1, 2e7f230, 61889ae, 3493cfa, e341e1c, 8613376 | **Fork-only** (not code; never promoted) | `CLAUDE.md`, `docs/backlog.md`, `docs/brd-*.md`, `docs/upstream-sync.md`, `Taskfile.yml`, `.gitignore` (`.mcp-palace/`), the `pi-lens-ignore` comment in `mempalace/backends/qdrant.py` |
| D9 | none (no remaining delta) | ci/pre-commit ruff pin 0.16.6 | c285504 | **Upstreamed** (37e1415). The stale `uv.lock` follow-up is retired: the lock at `d439d1e` pins ruff 0.16.6. | — |

## Collision recipes (historical: merge sync 8c4865f..d439d1e)

Executed under the merge protocol, before the stack existed; the SHAs below
are in `archive/develop-merge-f2e8893`. Under the rebase protocol the same resolutions apply inside the item's own commit: C6 while rebasing the `Fork-Item: D5` commit, C7 while rebasing the `Fork-Item: D6` commit. During a rebase the sides swap: the merge-era `-X theirs` (upstream's hunk) is `-X ours` while replaying a stack commit.

| C | File | Kind | Recipe as executed | Gotchas |
| --- | --- | --- | --- | --- |
| C6 | `mempalace/palace/mined.py` | content, D5 | `git merge -X theirs` takes upstream's hunk in `_scan_all` and `prefetch_content_hashes`. Then `c74b4d2` sets `_paged_metadata` to `yield from _iter_all_metadata(collection)`, reverts the two auto-merged D5 docstring hunks to upstream's text, and retargets `TestPartialFetchSwallowPreserved` → `TestScrollFailureRaisesMinedSetUnavailable::test_scroll_failure_raises_mined_set_unavailable` (docstring point 5 too). Then `cb545fa` (R11) keeps upstream's `count()`+offset loop verbatim for a `ChromaCollection` inside `_paged_metadata`, pinned by `TestChromaFallbackKeepsUpstreamPaging`. | **Fragment exec:** `palace/*.py` are exec'd into `mempalace.palace`, so patch through `mempalace.palace`; upstream's raise test patches `mempalace.palace._paged_metadata`. Do not wrap the fallback in `try/except`. The legacy oracles already use `_meta_is_current`. **Recurs every sync (epic R7):** each #2684-family perf commit widens the Chroma/qdrant gap here. |
| C7 | `integrations/openclaw/SKILL.md` | content, D6 | `-X theirs` takes upstream's `mempalace_search` line, and the D6 rule-4 sentence auto-merges. Then `a1b9297` deletes the 6 blank lines after tool-list `###` headings, leaving SKILL.md `1 1` against upstream. | `git checkout --theirs <file>` would drop the rule-4 sentence. |

Recipes C1–C5 (238ca21..8c4865f) are in this file at 01be3cc.

Auto-merged files that got a semantic review this sync: `schemas.py`, `tools_read.py`, `tools_diary.py`, `miner.py`, `tests/conftest.py`, `tests/mcp/test_protocol.py`. All intact.

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

- **inv-D2**: qdrant payload indexes and non-blocking adoption.
- **inv-D5**: C9 single-pass prefetch (now a small diff against `palace/mined.py`).
- **inv-D3-facet**: facet-backed CLI status counts.
- **inv-D6**: repo-wing diary hint.
- **inv-D7**: hermetic conftest scrub (`MEMPALACE_*` + `XDG_CONFIG_HOME`). The `XDG_CONFIG_HOME` half fixes an upstream gap that makes `tests/mcp/test_write_tools.py::TestWriteTools::test_check_duplicate_short_circuits_when_vector_disabled` fail wherever `XDG_CONFIG_HOME` is exported.

## Follow-ups

- **qdrant `get_recent` override** using `order_by filed_at`. It would bring L1 wake-up back to one bounded read (see recipe C1 at 01be3cc: about 5,000 vs 2,000 rows) and give true newest-first rather than an insertion-order window.
- **qdrant port of `prefetch_complete_mtimes`** via `get_all_metadata` (intent I8, BRD-P1). The project-mine skip check is still one `get(where=source_file)` per file on qdrant.
- **T7 hook-output wording:** the stop hook now reports `drawers_filed` and `messages_folded` separately. Nothing is known to parse it.
- **Coverage below pyproject's 85%** (pre-existing): BASE `01be3cc` measures 83.43%, this sync 83.99% (W1 tree) / 83.98% (final tree), upstream CI gates at 80. Gated as no-regression (intent decision D7, not inv-D7).
- **D5 test gaps** (W1 tester P2): no test pages `_page_all_metadata_via_get` past 1000 rows, and none drives qdrant wrapped in `EmbeddingCollection` through the prefetch helpers. On qdrant, a failed registry scan now aborts `mine_convos` with `MinedSetUnavailable` (upstream's contract).

## Retire the fork

The fork can be retired (installs switch to an upstream clone, intent D8) once every fork-only **code** item below is merged upstream:

| Item | What | Upstream PR |
| --- | --- | --- |
| inv-D2 | qdrant payload indexes + non-blocking adoption | none |
| inv-D3-facet | facet-backed CLI status | none |
| inv-D5 | C9 single-pass prefetch | none |
| inv-D6 | repo-wing diary hint | none |
| inv-D7 | hermetic conftest scrub | none |

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
