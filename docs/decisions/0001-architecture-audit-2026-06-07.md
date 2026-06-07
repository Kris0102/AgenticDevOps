# 0001 — Architecture audit findings & remediation order

Status: accepted
Date: 2026-06-07

## Context

First `critical-architect` pass over the codebase vs `docs/ARCHITECTURE_preview.md`,
`docs/Blackboard.md`, `docs/Design_preview.md`, `docs/MCP_AND_TOOLS.md`, `CLAUDE.md`,
and the actual code (`src/orchestrator/*`, `src/tool_gateway/*`, `src/db/models.py`,
`src/agents/Planning/planner.py`).

**Verdict:** The architecture is sound at the macro level (star topology, blackboard
ownership, deny-by-default gateway, Docker-per-run sandboxing, grounded verification
intent are all present and largely respected). The flaws found are **not** "wrong
pattern" problems — they're **incomplete enforcement of the system's own non-negotiables**:
stop conditions are declared but not read, `run_state` is declared but never written,
and one component (`ToolGateway`) quietly violates the transaction-ownership rule the
spec assigns to the Orchestrator. Each of these directly undermines one of the four
end-goal properties (auditability / safety / reproducibility / HITL) — not generic
code-quality nits.

## Decision

Track and remediate the following, in this order (sequencing matters — items build on
each other):

### must-fix

1. **Stop conditions declared but never enforced — infinite-loop / runaway-cost risk.**
   `ARCHITECTURE_preview.md` §6.2 states stop conditions are *required* ("Max plan
   iterations", "Max attempts per microtask"). The schema carries the data
   (`Constraints.max_total_iterations`, `Constraints.max_microtask_attempts`,
   `Microtask.attempts`/`max_attempts`, `iteration.global_iterations_used` —
   `src/orchestrator/state.py:63-64,97-98,258-261`), but **no router reads any of it**:
   `plan_verification_router` (`src/orchestrator/orchestrator.py:154-159`) and
   `execution_verification_router` (`:162-167`) only check `review_feedback`/`evidence`
   and can loop `planner ⇄ plan_reviewer` or `coder ⇄ tester` forever. This is a
   **safety** non-negotiable violation — directly threatens runaway LLM cost and
   non-terminating runs once the stub agents are wired up for real.
   → Fix: routers must increment + check `iteration.global_iterations_used` against
   `constraints.max_total_iterations`, and `microtask.attempts` against
   `constraints.max_microtask_attempts` / `microtask.max_attempts`, routing to
   `NEEDS_HITL_APPROVAL` or `FAILED` when exceeded — exactly as §6.2 specifies.

2. **`run_state` table declared, never written — reproducibility non-negotiable broken.**
   `ARCHITECTURE_preview.md` §3.2 lists `run_state(run_id, state_json, version,
   updated_at)` as part of the "minimum required data model" whose explicit purpose is
   *"run state machine persistence (resume/replay)"*. The ORM model exists
   (`src/db/models.py:22-28`), the migration created the table, but
   `grep -rn "RunState"` shows **zero writes** anywhere in the codebase. Right now, if
   the process crashes mid-run, the blackboard is gone — the very thing episodic memory
   is supposed to prevent. This is the #1 prerequisite for the Reflector to ever be
   useful (its whole job depends on recoverable history).
   → Fix: snapshot `BlackboardState` to `run_state` (with incrementing `version`) at
   the end of every node — same place `Run.status` is already updated, so it's one
   more `db_session.merge(RunState(...))` per node, not a new architectural concept.

3. **`ToolGateway` commits mid-transaction — breaks Orchestrator's transaction ownership.**
   `gateway.py:43` calls `self.db.commit()` inside `_log_event`, but the gateway is
   handed a **shared session** owned by `planner_node`'s `with db.session_scope()`
   block (`orchestrator.py:40,43`). `session_scope` is supposed to be the single commit
   boundary ("commit on success, rollback on error" — `db.py:34-42`), mirroring the
   spec's rule that *only the Orchestrator controls episodic-memory writes* (§3.2 "Write
   rules: Only the Orchestrator may mutate the run_state"). With the gateway committing
   early: `tool.executed`/`tool.denied` events become durable immediately, but if
   `planner.invoke()` later raises, the **microtask records, plan artifact, and
   `plan.drafted` event in the same `with` block roll back** — leaving an audit trail
   that shows tool calls happened but no resulting plan. That's a silent **auditability**
   defect: the trail looks complete but is internally inconsistent.
   → Fix: gateway should `session.add(event)` only (matching the pattern already used
   correctly in `bootstrap_node._evt`, `bootstrap_node.py:20-34`) and let the caller's
   `session_scope` own the commit. If immediate durability of `tool.denied` events is
   wanted for safety auditing even on later rollback, give the gateway its **own**
   short-lived session per event — don't half-own a session it was only lent.

### should-fix

4. **`fs.read_file`/`fs.write_file` skip path validation — dead security check.**
   `gateway.py:154` defines `check_requested_path()` to keep file ops inside the
   sandbox workdir, but it is **never called** — confirmed by
   `grep -rn check_requested_path` (only the definition and a stale TODO comment in
   `planner_tools.py:50`). The `fs.read_file`/`fs.write_file` branches
   (`gateway.py:95-107`) pass `args.get("path")` straight to `sandbox_tools` with no
   traversal check. The gateway's entire stated purpose is "deny-by-default... every
   call logged" (`CLAUDE.md` "Key architecture decisions") — an unused guard function
   sitting next to the exact code path it should guard is a latent hole, not a defense.
   → Fix: call `check_requested_path` (or move the check into `sandbox_tools` itself,
   since that's where the workdir boundary actually lives) before routing
   `fs.read_file`/`fs.write_file`; deny + log `tool.denied` on failure, matching the
   pattern already used for command-allowlist denial.

5. **`docs/Blackboard.md` contained a duplicated, broken §6/§7 — fixed in this pass.**
   Lines 266-397 were a draft copy of the "Events" section (shorter §6.1-6.6, no event
   type registry) followed by a **broken markdown code block** that never closed,
   immediately followed by a second, more complete copy of "## 6) Events" (with the
   full §6.7 event-type enum the orchestrator code actually depends on for event-type
   names). Numbering was self-contradictory (`## 6.7` appeared as a sibling of `## 7`,
   not nested under `## 6`). **Removed the draft duplicate and renested 6.7 as `###`.**
   This isn't cosmetic — a future session reading this doc for "what event types exist"
   could easily land in the broken/incomplete copy and miss the real registry.

6. **`reflections` table missing — already known, reinforced here.**
   Confirmed still true (`grep` shows `Reflection` only as an in-memory Pydantic model,
   `state.py:138-145`, never an ORM table). Listed because it compounds finding #2:
   once `run_state` snapshotting exists, reflections-in-memory-only becomes the next
   most obvious "we'll lose this on crash" gap, and the Reflector's value proposition
   (cross-attempt learning) depends on both.

### consider

7. **Memory-folder for cross-session "why" — created in this pass: `docs/decisions/`.**
   The user asked how to persist architectural memory so a brand-new session stays
   up to date. `CLAUDE.md` already does "what's true now" well, but it necessarily
   gets rewritten and loses history. Proposal adopted: lightweight ADR records in
   `docs/decisions/NNNN-title.md` (see `docs/decisions/README.md` for format) —
   append-only, dated, never deleted (superseded records link forward). This record
   is ADR 0001. Going forward, any audit finding that gets actioned (or deliberately
   deferred) should get its own ADR — that's what stops the same debate from being
   re-opened cold in a future session.

8. **Context Engine / Semantic Memory absence is currently *not* a regression.**
   `ARCHITECTURE_preview.md` §3.3/§4.2 specs a Context Engine + pgvector retrieval
   layer; `CLAUDE.md` lists it "Not started". Worth noting explicitly: until it exists,
   the Planner's ReAct loop re-reads the sandbox directly via `sandbox.run`/`fs.read_file`
   — which is **more** aligned with the "Sandbox is the Source of Truth for Code"
   non-negotiable (§2.3: "Orchestrator must never treat Semantic Memory as the repo")
   than a half-built retrieval layer would be. Don't prioritize building it before
   items 1-3 above; a broken stop-condition or transaction boundary will hurt far more
   than missing semantic search at this stage. Tech-stack choice itself (pgvector in
   Postgres for "early/recommended" scale) still looks right for this project's size —
   no replacement proposed.

## Alternatives considered

- **Replacing the Postgres-JSON blackboard with full event-sourcing** (rebuild state by
  replaying `events` instead of snapshotting `run_state`): rejected for now — it would
  solve #2 "for free" but is a much bigger change to the state-management model than
  the spec calls for, and the spec's `run_state` snapshot + `events` audit log
  combination already gives both resumability *and* auditability without that
  complexity. Revisit only if snapshot/replay drift becomes a real observed problem.
- **Giving the gateway its own DB session entirely** (decouple event logging from the
  node's transaction): considered as the fix for #3, noted as a valid variant — but
  the simpler fix (stop calling `commit()`, let the lent session's owner commit) costs
  less and matches the pattern `bootstrap_node` already uses correctly.

## Consequences

- Items 1-3 should land before the Coder/Tester/Reflector stubs are implemented for
  real — wiring real agents into routers that don't enforce stop conditions, or onto
  a transaction boundary that's already silently violated, multiplies the blast radius
  of both bugs.
- Item 7 (`docs/decisions/`) changes nothing about the code; it's pure process — but it
  is the direct answer to "how do I keep a new session up to date," so future sessions
  should be pointed here first (see `CLAUDE.md` reference added alongside this ADR).
