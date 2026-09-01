# BRD — P2: Repo-wing diary hint (anchor/monorepo entries file under the repo's wing)

**Status:** Draft v0.1 (for review — no implementation exists yet)
**Scope decision:** P2. Protocol/hint-level change only — zero server-side classification logic, zero schema changes. The server stays deliberately dumb about repos; the agent already has the context (per the hook's own design note). **Save-hook surface (R3) dropped by Reevon** — pi sessions don't run the Claude Code/Codex save hook, so the protocol text + tool description are the only live carriers.
**Repo state at writing:** `develop` @ `8613376`; live personal palace has organic repo-named wings already in use.

---

## 1. Problem Statement

When an agent writes a diary entry via `mempalace_diary_write`, the wing defaults to `wing_{agent_name}` (mcp_server.py:4143–4145) or whatever the agent improvises (`sessions`, …). Nothing in the protocol tells the agent to consider *where the work happened*. Result: session records for repo-anchored work get scattered across agent/session wings, and the per-repo memory that `pi-extensions` and `pi-mempalace-github` wings were created for only accumulates when an agent happens to think of it.

Concretely (2026-09-01, this session): a full qdrant-incident RCA session run entirely inside the mempalace repo was diary-filed to `wing=sessions` — the repo's own wing (`pi-mempalace-github`) exists and would have been the right home.

Working in an **anchor/monorepo** (a long-lived project repo you return to across sessions — mempalace, pi-extensions, TML CVP) is exactly the case where repo-scoped recall matters most: "what did we decide in this repo last time" should be one wing filter away (`mempalace_search(wing=...)`, `mempalace_diary_read(wing=...)`).

### Architectural basis (why the repo wing wins)

The palace structure defines wings as **entities** (person/project) and rooms as **time-slices** (days, sessions). A session is therefore a room-level concern — and it is already preserved on every drawer via timestamps and `source_file` provenance. A repo is a project — the exact entity a wing exists for. Filing repo-anchored diaries under a `sessions` wing inverts the model: a time-slice parked at the entity level, breaking the one precise recall path (`wing=` filters). The `sessions` wing's architectural job is the *mined-transcript archive* (rooms: technical/problems/architecture), not a home for deliberate diary records. Exception: personal/cross-repo entries (agent identity, general lessons) stay in the agent's own wing.

## 2. Measured Evidence

| # | Claim | Measurement / Source |
| --- | ----- | ----- |
| D1 | The standing diary instruction is wing-agnostic: `PALACE_PROTOCOL` rule 4 says only "AFTER EACH SESSION: call mempalace_diary_write to record what happened…" | `mempalace/mcp_server.py` ~L2274 (included in every status/wake-up response) |
| D2 | Default wing is per-agent, not per-repo: `wing=""` → `f"wing_{agent_name.replace(' ', '_')}"`; an explicit `wing` param exists but nothing guides its value | `tool_diary_write`, mcp_server.py:4118–4145 |
| D3 | Repo-named wings already exist organically in the live personal palace — `pi-extensions` (room `diary`), `pi-mempalace-github` (room `diary`), `github` — filed ad hoc by agents, no codified convention | `mempalace status` wing listing, 2026-09-01 |
| D4 | Classification is agent-side by design: the save hook's own comment states "The AI does the classification — it knows what wing/hall/closet to use because it has context about the conversation. No regex needed." | `hooks/mempal_save_hook.sh` header |
| D5 | Live miss: a repo-anchored RCA session (cwd = mempalace repo, full session) diary-filed to `wing=sessions` | diary entry `diary_sessions_20260901_111335484737_c604eb0945ba` |
| D6 | Secondary touch points: `mempalace_diary_write` appears in the TOOLS read/write lists (mcp_server.py:435, 477) with a parameter description; the mempalace/pi skills restate protocol guidance outside the repo | TOOLS dict, `~/.pi/agent/skills/mempalace*/SKILL.md` |

> Reviewers: D5 is the load-bearing claim — one live example. If agents already reliably pick repo wings without a hint, this BRD is over-engineering; check the last N diary entries per repo-anchored session before building.

## 3. Goals

- **G1** — An agent writing a diary from inside an anchor/monorepo has an explicit, in-protocol instruction to file under that repo's wing; default behavior for non-repo work is unchanged.
- **G2** — The convention matches what already exists organically (D3): plain repo-based wing names, human-readable, no `wing_` prefix, `-github`-style host suffix only for disambiguation.
- **G3** — Zero server-side logic: the hint ships as protocol/tool text. The server must never try to infer repo from paths (the MCP server has no reliable cwd contract, and D4's classification philosophy is intentional).

## 4. Non-Goals

- No auto-derivation of wing from `git remote` inside `tool_diary_write`.
- No migration of existing misplaced entries (the `sessions`-filed RCA above stays; it is findable via search).
- No per-repo diary schema or room changes — rooms stay `diary`, topics stay free-form.
- No changes to `mempalace_diary_read` semantics (it already accepts a `wing` filter).

## 5. Proposed Design

### 5.1 Hint in `PALACE_PROTOCOL` (primary)

Extend rule 4 with one sentence:

> "If the session's work is anchored in a long-lived repo (anchor/monorepo you return to across sessions), file the diary to that repo's wing — `wing=<repo-wing>` (e.g. `pi-mempalace-github`, `pi-extensions`); keep personal or cross-repo entries in your agent wing."

### 5.2 Tool description (secondary)

`mempalace_diary_write`'s `wing` parameter description gains: "for repo-anchored work, use the repo's wing (see protocol rule 4); defaults to your agent wing."

### 5.3 Save-hook reason text — DROPPED

Originally proposed a clause in `mempal_save_hook.sh`'s save-reason output. Dropped: the hook is Claude Code/Codex-only and unused in this stack (pi + MCP). Recorded so a future contributor doesn't re-add it as a "missing" surface.

### 5.4 Naming convention (documentation, not code)

- Wing = the repo's established wing name: repo name as-is (`pi-extensions`), suffixed with the forge host when the bare name collides or the existing wing already carries it (`pi-mempalace-github`).
- Anchor test: do you return to this repo across sessions and mine it? If yes → repo wing. Scratch/one-off dirs → agent/sessions wing.
- One session, many repos → file under the session's primary repo; cross-cutting findings go to the agent wing.

**Read side (symmetry rule):** repo question → `wing=<repo-wing>` filter (`mempalace_diary_read` / `mempalace_search`); agent-personal question → agent wing; unknown location → unfiltered semantic search (spans all wings). Writes by where the work lives; reads by what you're asking about.

## 6. Alternatives Considered

- **Server-side cwd→wing inference** — rejected: D4; the server can't reliably know the client's cwd across hosts/transports, and guessing wrong splits diaries silently.
- **Rename/normalize existing organic wings** — rejected: churn without payoff; the hint targets future entries, and search spans all wings anyway.
- **Config file mapping repos→wings** — rejected for P2: yet another config surface for what a one-line agent instruction handles; revisit only if agents demonstrably misclassify after the hint ships.
- **Separate `repo` metadata field on diary entries** — bigger schema change, no recall win over wing-filtering for this use case.

## 7. Requirements

- **R1** — `PALACE_PROTOCOL` rule 4 carries the repo-wing hint (5.1 wording or equivalent).
- **R2** — `mempalace_diary_write` tool description mentions the convention (5.2).
- **R4** — The convention (5.4) is documented once in the BRD and reflected in the mempalace skill docs if they duplicate protocol text.

## 8. Test Strategy

- Protocol snapshot: any test asserting `PALACE_PROTOCOL` content is updated; add an assertion that rule 4 mentions the repo-wing hint (string-contains, hermetic).
- Tool description: assert the `wing` param help text mentions repo wings (TOOLS dict test, same pattern as existing tool-catalog tests).
- Hook: dropped (5.3) — no hook test needed.
- No behavioral server tests required — no runtime logic changes.

## 9. Risks & Open Questions

- **Wing proliferation / typos** (`pi-mempalace` vs `pi-mempalace-github` vs `mempalace`) — mitigated by G2's "match existing wings" rule and by status wake-up listing current wings; accept some mess.
- **"Anchor" is judgment, not a flag** — an agent may under- or over-apply the hint. Accepted: wrong-wing entries remain fully searchable (semantic search ignores wing unless filtered).
- **Skills drift** — the pi/Claude skill docs restate protocol text outside this repo; if they go stale, two sources of truth disagree. *Open question: do the mempalace skills inline the protocol, or link it?*
- **Hook text only reaches Claude Code/Codex users** — pi sessions get the hint via PALACE_PROTOCOL at wake-up; acceptable coverage gap for P2.

## 10. Rollout

1. Single commit: `feat(mcp): repo-wing diary hint in protocol and tool help` (R1–R2 together — they are one sentence of intent in two surfaces).
2. Update the mempalace/pi skill docs only if they duplicate rule 4 (R4).
3. Success signal (no code): spot-check that new repo-anchored diary entries land in repo wings within a couple of weeks; if not, escalate the alternatives (config mapping) rather than growing the hint text.

## 11. Sibling change — pi-mempalace extension (pi-extensions repo)

This BRD is necessary but **not sufficient** for pi sessions. Two of the surfaces an agent actually sees in pi are generated by the pi-mempalace extension, not by this repo:

1. **Checkpoint instruction hardcodes `Target wing: sessions`** (observed live 2026-09-01: "[MemPalace checkpoint — 15 exchanges] … Target wing: sessions") — actively steering curation/diary writes *away* from repo wings, fighting this BRD's protocol hint. The line must become repo-aware: "Target wing: the anchor repo's wing if this session's work is repo-anchored (protocol rule 4), else sessions."
2. **Wake-up context carries no protocol text** — the extension's `[MemPalace Session Context]` block (L0/L1) does not include PALACE_PROTOCOL, so a pi agent only learns rule 4 if it calls `mempalace_status`. A one-line repo-wing hint in the extension's wake-up/checkpoint text closes the gap.

Also outstanding in that repo: the `diaryWrite` write mechanism (daemon-HTTP `diary_write` job vs new mempalace CLI subcommand — oracle-reviewed 2026-08-08, `add-drawer` CLI ruled invalid). Whichever mechanism wins must apply the same wing convention. Track both in the pi-extensions repo; P2's mempalace-side surfaces land independently and remain correct on their own.
