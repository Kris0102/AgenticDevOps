# 0002 — Design clarifications: sequential execution, Reviewer scope, HITL placement, optional Auditor

Status: accepted
Date: 2026-06-07

## Context

Follow-up to ADR 0001 (implementation-bug audit). This one is **design-track**, not
implementation: a `critical-architect` pass over the *blueprint* itself
(`ARCHITECTURE_preview.md`), independent of code state. Surfaced four places where the
spec was ambiguous or made a design call that hadn't been explicitly confirmed:

1. `Microtask.dependencies` exists in the schema but the workflow describes a plain
   sequential queue — ambiguous whether a DAG scheduler is intended.
2. `Plan Reviewer` was specified as a generic "approve OR critique" gate — overlapping
   with the deterministic structural validator the Planner already runs
   (`_normalize_output`), which is redundant probabilistic judgment over grounded checks.
3. HITL gates were specified only at the back end (PR/merge/deploy) — the most
   expensive place to first involve a human, since rejection there wastes an entire
   plan→code→test loop.
4. `Security/Policy Auditor` was specified as a required pipeline agent with no
   off-switch, despite being explicitly listed "Not started" and adding a 7th LLM hop.

User resolved all four directly:

> "Microtask run sequentially. The reviewer will be a LLM criticizing the plan. Is it
> detailed enough etc. HITL will be after each step in the devops after planner coder
> etc. Auditor is optional if want planned to be implemented."

## Decision

1. **Sequential execution is final — `dependencies` is reserved, not active.**
   `ARCHITECTURE_preview.md` §6.2 Phase C now states explicitly: microtasks execute one
   at a time in `idx` order, never parallel/reordered; `Microtask.dependencies` is
   *reserved for a possible future parallel scheduler* and is **not consulted** by the
   v1 loop. Keeps the audit trail linear (one diff, one verification cycle, in order) —
   directly serves the auditability property. A DAG scheduler would be a substantially
   larger commitment (parallel sandboxes, workspace-merge strategy, parallel
   verification loops) that nothing currently calls for.

2. **Plan Reviewer's charge is narrowed to plan *quality and detail* — not structure.**
   `ARCHITECTURE_preview.md` §5.2 now spells out exactly what it judges (is each
   microtask detailed enough to code from without guessing, is the ordering correct,
   are there missing edge cases, are acceptance criteria observable) and explicitly
   excludes structural/schema validation — that's deterministic and already owned by
   the Planner's own `_normalize_output` gate. This keeps the §2.2 deterministic/
   probabilistic split honest: the Reviewer spends its judgment only where judgment is
   actually needed.

3. **HITL gets a generalized mid-run checkpoint mechanism: `policy.hitl_checkpoints`.**
   New `Policy` field (added to both `state.py` and `Blackboard.md` — keeping spec and
   code in sync from the moment of decision, not after drift sets in): a list of phase
   names (`"plan"`, `"code"`, `"test"`, …) that pause the run at the existing
   `status=NEEDS_HITL_APPROVAL` / `hitl.pending_approvals[]` mechanism before
   proceeding. One gate code path, configured per-run by `risk_level` — a
   `risk_level=high` run can gate every phase, a trusted low-risk run can gate only
   `merge`. Documented in `ARCHITECTURE_preview.md` §6.2 Phase B (after plan approval —
   the cheapest veto point, before any compute is spent) and Phase C (after Coder,
   after Tester). `Blackboard.md` §J's `pending_approvals[].type` enum extended with
   `"plan" | "code" | "test"`.

4. **Security/Policy Auditor becomes optional, gated by `policy.security_audit_enabled`
   (new field, default `false`).** `ARCHITECTURE_preview.md` §5.6 now states it's not
   part of the required v1 pipeline — build it when a concrete target repo/org actually
   requires it (same "don't build speculative infra" reasoning as deferring the Context
   Engine, ADR 0001 finding #8). When disabled, Phase D routes `READY_FOR_PR` directly,
   skipping `NEEDS_SECURITY_AUDIT`. `policy.security_scans_required` still applies
   regardless — that's the Tester's deterministic check list, independent of whether
   the dedicated audit *agent* runs. Also reinforced: the Auditor's pass/fail must stay
   deterministic (scanner thresholds) even when enabled — the LLM narrates, never decides
   (§2.2 — an LLM-only security gate is the exact "LLM-only review" grounded verification
   forbids, and wrong here costs more than a wrong test-pass).

## Alternatives considered

- **DAG-based parallel microtask scheduling** (using `dependencies` for real): rejected
  for v1 — bigger commitment than the stated goals require, and conflicts with keeping
  the audit trail trivially linear. Could be revisited explicitly, as its own ADR, if
  sequential throughput becomes a measured bottleneck — at which point `dependencies`
  graduates from "reserved" to "active" with a real scheduler design attached.
- **Single blanket HITL-on-everything flag**: rejected in favor of a list
  (`hitl_checkpoints: List[str]`) — a flat bool can't express "gate planning and merge
  but trust the coder/tester loop," which is exactly the kind of risk-tiered config a
  `risk_level`-driven policy needs to support.

## Consequences

- `Policy` model gained two fields (`hitl_checkpoints: List[str] = []`,
  `security_audit_enabled: bool = False`) — both default to today's behavior (no mid-run
  gates, Auditor off), so existing run-construction code needs no changes.
- Implementing the mid-run HITL checkpoints requires the routers to actually check
  `policy.hitl_checkpoints` against the current phase — this is new router logic, not
  yet built (tracked alongside ADR 0001 finding #1, stop-condition enforcement — both
  are "router needs to read `policy`/`constraints` it currently ignores").
- The Plan Reviewer's narrowed scope means its prompt design can stay smaller and more
  focused than a generic "review this plan" — worth keeping in mind when it moves from
  stub to real implementation.
