# 0011 — Real Plan Reviewer + planning-loop iteration cap

Status: accepted
Date: 2026-06-19

## Context

The Plan Reviewer was a stub that always returned `NEEDS_CODE` (rubber-stamp). PR #17
("Feat reviewer agent") replaced it with a real two-phase reviewer. That immediately
made a previously-dormant bug live: CLAUDE.md known-bug #1 / audit 0001 — there was no
`max_total_iterations` enforcement on the `planner → plan_reviewer → planner` loop. A
stub that always approved could never loop; a real reviewer that can reject repeatedly
can spin planner↔reviewer forever (and burn LLM calls each cycle).

Separately the audit-registry promised durable `plan.reviewed` / `plan.approved` events
(Blackboard §6.7), but `EventType` had only `PLAN_DRAFTED`. The reviewer emitted just
`RUN_STATUS_CHANGED`, so the audit log carried no record that a review happened, nor its
decision or notes — asymmetric with the planner's `PLAN_DRAFTED`.

## Decision

**Reviewer shape.** Two phases mirroring the planner: an optional independent
investigation (read-only tools — `FS_READ_FILE`, `SANDBOX_RUN` with a grep/ls/cat/find
allowlist, `SEMANTIC_SEARCH`) followed by a structured approve/reject (`ReviewerOutput`
= decision + notes). The verdict is appended to the plan as a `ReviewFeedback`
(`source=REVIEWER`); `APPROVE → NEEDS_CODE`, else `NEEDS_PLAN`.

**Loop cap lives in the node, not the router.** `ReviewerRunner.invoke` checks
`Iteration.planner_reviewer_iteration_current >= Constraints.max_total_iterations`
*before* invoking the LLM; on exhaustion it sets `FAILED`, persists it, and emits
`RUN_FAILED`. Routers stay pure (read-only) per the bootstrap/router invariant — a
router can't set `FAILED` in the DB without desyncing it from the blackboard, and the
runner already owns the `session_scope`. The counter is advanced via the node-return
dict (`result["iteration"] = iteration.model_copy(update=...)`), never by mutating the
state object in place — LangGraph checkpoints state from returned deltas, so an in-place
`+= 1` would not survive the superstep and the cap would never trip.

**Durable review event.** Added `EventType.PLAN_REVIEWED = "plan.reviewed"`; the reviewer
emits it with `decision`, `notes`, `next_status`, and the iteration count. `RUN_FAILED`
covers the cap path. (`plan.approved` deferred — approve is already captured as
`PLAN_REVIEWED` decision=approve; a separate event is redundant until HITL needs it.)

## Alternatives rejected

- **Cap in `_plan_review_router`.** Rejected: routers must be side-effect-free; setting
  `FAILED` there would leave the DB `Run.status` stale (router can't persist) and split
  the failure decision across router + node.
- **Counter on the `Plan` model.** Rejected: `Plan` is replaced via `model_copy` on every
  re-plan, so a counter there resets and loses history; it also creates a second source
  of truth for "how many loops" alongside the `Iteration` state field.

## Consequences / follow-ups

- `Iteration` currently carries two global counters (`global_iteration` +
  `global_iterations_used`) that nothing increments — redundant, collapse to one.
- The coder↔tester↔reflector loop has no equivalent cap yet
  (`Iteration.coder_tester_reflector_iteration_current` is defined but never bumped).
- The review verdict is now persisted to the `artifacts` DB table (`kind="review"`,
  id `art_<run_id>_review_<n>`) alongside the checkpoint-only `state.Artifact` in
  `artifacts_index`. The reviewer's `.txt` debug dump (`review_output.py`) remains
  on-disk only — fine, it's a debug aid, not audit state.
