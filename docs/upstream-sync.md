# Upstream sync — divergence inventory

This is the standing record of how this fork differs from `upstream`
(MemPalace/mempalace). The sync protocol itself lives in
[CLAUDE.md](../CLAUDE.md) under "Repo intent — personal-use fork". This doc
is updated on every sync.

| | |
| --- | --- |
| Sync base (upstream/develop) | `8c4865f` (v3.10.0) |
| Previous sync base | `238ca21` |
| Synced on | 2026-09-26, branch `chore/sync-upstream-8c4865f` |
| Merge commit | `b092219 merge: sync upstream/develop (238ca21..8c4865f)` |
| Transplant / fix commits | `3802060` (inv-D5, C3), `07d18f6` (inv-D4 guard, C1), `17bd277` (inv-D6 tests, C5), `ccfdbd7` (inv-D7 extension) |

## Divergence inventory (D-items)

The status column records each item **as landed** at the sync base above.

| D | Divergence | Fork commits | Status after this sync | Where it lives now |
| --- | --- | --- | --- | --- |
| D1 | Qdrant `get()` limit pushdown | 4d65cd2 | **Superseded** by upstream 720f3d8 (`_scroll_all(max_rows=)`). The guard test is kept. | `tests/test_qdrant_limit_pushdown.py` (guard, green) |
| D2 | Qdrant payload indexes on `metadata.source_file` / `wing` / `room`, adopted non-blocking (`wait=false`) | 4534c57, 05ae8ec, 3ea194a, b2a74a5 | **Fork-only** | `mempalace/backends/qdrant.py` (`_FILTER_INDEX_FIELDS`, `_ensure_filter_indexes`, `create_payload_index`), `tests/test_qdrant_filter_indexes.py` |
| D3 | CLI status: `get_all_metadata` fast path + facet-backed wing/room counts | a37602a, 55e13bd, 22484cc | Fast path **upstreamed** (d27e515). Facet path (inv-D3-facet) is **fork-only**. | `mempalace/miner.py` `status()` (facet block ahead of the shared fast path), `tests/test_cli_status_fast_path.py` |
| D4 | L1 wake-up single-pass candidate fetch | 6cdc5e4 | **Superseded** by upstream 0da09a2 (`BaseCollection.get_recent`). The guard is retargeted to the `get_recent` contract. | `tests/test_l1_wake_up_fast_path.py` (guard) |
| D5 | Bulk prefetch single pass (C9): `prefetch_mined_set` / `prefetch_content_hashes` via `get_all_metadata` | 8c25220 → 3802060 | **Fork-only**. Now lives in `palace/mined.py`, under upstream's #2567 scoping. | `mempalace/palace/mined.py`, `tests/test_prefetch_single_pass.py` |
| D6 | Repo-wing diary hint (protocol rule 4 + diary_write tool help) | 93204dc, 0a6c66a → 17bd277 | **Fork-only**. Its tests moved to `tests/mcp/test_protocol.py`. | `mcp_server/{schemas,tools_read,tools_diary}.py`, `integrations/openclaw/SKILL.md`, `tests/mcp/test_protocol.py::TestHandleRequest` |
| D7 | Hermetic test env: scrub every `MEMPALACE_*` var, plus `XDG_CONFIG_HOME` (added this sync) | 0727958, 020109a, ccfdbd7 | **Fork-only** | `tests/conftest.py` |
| D8 | Fork docs + tooling: BRDs, backlog, CLAUDE.md intent, this doc, Taskfile, lens suppression | 8613376, 60500f1, e47dbc8, 4470229, e341e1c, 3493cfa, 61889ae, 79992c7, bfda597, 2e7f230 | **Fork-only** (not code; never promoted) | `CLAUDE.md`, `docs/backlog.md`, `docs/brd-*.md`, `docs/upstream-sync.md`, `Taskfile.yml` |
| D9 | ci/pre-commit ruff pin 0.16.6 | c285504 | **Upstreamed** (37e1415) | — |

## Collision recipes (as executed for 238ca21..8c4865f)

| C | File | Kind | Recipe as executed | Gotchas |
| --- | --- | --- | --- | --- |
| C1 | `mempalace/layers.py` | content | `git checkout --theirs`: take upstream's `_fetch_candidates` / `get_recent` whole. The fork's only layers change was D4. Then retarget `tests/test_l1_wake_up_fast_path.py` in its own commit (07d18f6): the mock gets `get_recent(*, limit, where, order_field, include)`, and the test asserts one call with `limit == MAX_SCAN`, `order_field == "filed_at"` and zero `get()` calls. | Upstream opens with `read_only=True`, so a `_get_collection` patch must accept `read_only`. A 2-arg lambda raises a TypeError that `generate()` swallows into "No palace found", making the guard silently vacuous; assert the output is not a failure header first. **qdrant cost:** qdrant has no `get_recent` override, so the base default pages `get()` in 500-row offset steps. L1 wake-up therefore scrolls about 5,000 rows where the fork did one 2,000-row get: bounded, but about 2.5× the rows, and the mock can't see it. See Follow-ups. |
| C2 | `mempalace/miner.py` | content (`status()`) | Hand-resolve **only** the conflict hunk: HEAD side (facet block, then the fast path). The fast path is byte-identical on both sides, and the script asserted upstream's side is a suffix of ours. Verified with `git diff 8c4865f HEAD -- mempalace/miner.py` showing no removed lines. | Never `--ours` the whole file: that silently drops upstream's non-conflicting T7 miner hunks. |
| C3 | `mempalace/palace.py` | modify/delete | `git rm` (upstream split it into the `mempalace/palace/` package). Then re-apply C9 in 3802060: add `_page_all_metadata_via_get` + `_iter_all_metadata` to `palace/mined.py`, and replace only the body of `_scan_all()` and `prefetch_content_hashes`'s count+offset loop. Upstream's `_absorb` / `_meta_is_current`, the `_PREFETCH_SCOPE_THRESHOLD` scoped `$in` get and its `groups.clear(); _scan_all()` fallback stay verbatim. | **Fragment exec (E15):** `palace/*.py` are exec'd into the `mempalace.palace` namespace, and `mined.py` raises ImportError if imported directly. Keep test imports and patch targets on `mempalace.palace`. **Chunker-version fixtures (E16):** upstream's `_meta_is_current` treats exchange rows without `convo_chunker_version >= CONVO_CHUNKER_VERSION` as stale, so fixture metadata must set it (legacy rows deliberately omit it). The legacy-algorithm oracles use `_meta_is_current`, not the bare `normalize_version` check. |
| C4 | `tests/test_cli_status_fast_path.py` | add/add | First confirm upstream's test names (TestFastPath ×4, TestFallback ×2) are a subset of ours, then `git checkout --ours`. | — |
| C5 | `tests/test_mcp_server.py` | modify/delete | `git rm` (upstream 7010a09 split it into `tests/mcp/`). Then move the 2 diary-hint tests verbatim into `tests/mcp/test_protocol.py::TestHandleRequest` (17bd277), placed before the next section comment so the formatter doesn't move it. | — |

Auto-merged files that got a semantic review this sync: `backends/qdrant.py`, `mcp_server/schemas.py`, `mcp_server/tools_read.py`, `mcp_server/tools_diary.py`, `integrations/openclaw/SKILL.md`, `tests/conftest.py`, `CLAUDE.md`, `.github/workflows/ci.yml`. All intact.

## Promotion candidates

Each needs the owner's explicit say-so before any upstream PR:

- **inv-D2**: qdrant payload indexes and non-blocking adoption.
- **inv-D5**: C9 single-pass prefetch (now a small diff against `palace/mined.py`).
- **inv-D3-facet**: facet-backed CLI status counts.
- **inv-D6**: repo-wing diary hint.
- **inv-D7**: hermetic conftest scrub (`MEMPALACE_*` + `XDG_CONFIG_HOME`). The `XDG_CONFIG_HOME` half fixes an upstream gap that makes `tests/mcp/test_write_tools.py::TestWriteTools::test_check_duplicate_short_circuits_when_vector_disabled` fail wherever `XDG_CONFIG_HOME` is exported.

## Follow-ups

- **qdrant `get_recent` override** using `order_by filed_at`. It would bring L1 wake-up back to one bounded read (see C1: about 5,000 vs 2,000 rows) and give true newest-first rather than an insertion-order window.
- **Stale `uv.lock` ruff pin upstream:** `pyproject.toml` pins `ruff==0.16.6` but `uv.lock` at 8c4865f pins 0.16.1. This sync ran with `UV_FROZEN=1`, so the lockfile stays byte-identical to upstream, and the local venv therefore runs ruff 0.16.1.

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
