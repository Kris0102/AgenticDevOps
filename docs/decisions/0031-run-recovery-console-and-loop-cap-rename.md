# 0031 — Run recovery console, loop-cap rename, and the rejected run-wide execution cap

Status: accepted
Date: 2026-07-11

## Context
GH #64 shipped a run-settings view that could change a run's per-agent **model** mid-run
(patch the checkpointed `RunConfig.llm.<agent>` via `graph.update_state`, read on that
agent's next node). Everything else on the view — knobs (timeouts, tool-step budgets) and
loop-caps — was read-only for the life of the run.

Debugging a real incident exposed the gap. A run stalled on a genuine transient (wifi drop),
and on `/continue` the Coder kept re-timing-out because `coder_timeout_seconds=120` was too
short for its model (a 122B). The timeout was the actual fix, but it was **frozen** — the only
stalled-run levers were the model and the additive token budget. Kill-and-restart was the only
option. A stalled/paused run needs an in-place way to tune the very knob/cap that caused the
stall before re-driving.

Two adjacent problems surfaced while reading the code:
- `Constraints.max_total_iterations` is misleadingly named. It only ever bounded the
  **planner↔reviewer** planning loop (`reviewer_node`, against
  `Iteration.planner_reviewer_iteration_current`) — never a whole-run iteration budget. The
  UI even labelled it "Max planner↔reviewer loops" while the backend field said "total".
- CLAUDE.md known-bug #1 listed a missing run-wide cap on the coder↔tester↔reflector loop
  (`Iteration.coder_tester_reflector_iteration_current` defined but never incremented).

## Decision

**1. Recovery console — edit knobs + loop-caps on a stalled/paused run.**
`PATCH /runs/{id}/config {field, value}` → `Service.patch_run_config` patches the checkpointed
`RunConfig.knobs` or `Constraints` via `graph.update_state` (the exact mechanism as the model
patch / `raise_token_budget`), read by the relevant agent on its **next node**. Editable:
- `knobs.*` (any knob) and `constraints.max_planner_reviewer_iterations` /
  `constraints.max_microtask_attempts`.
- `max_total_tokens` is **excluded** — it keeps its own additive `/continue` path (ADR 0025);
  one owner, no absolute-vs-additive confusion.

**Gated on `run_state ∈ {STALLED, PAUSED_HITL}`** (`RunConfigView.expert_editable`). Refused
while actively RUNNING (a live driver would race the checkpoint write mid-superstep) and while
terminal (frozen). Models stay editable in any non-terminal state, unchanged from GH #64.

Guarded in `_verify_constraint_value`: a loop-cap can't be set at/below the count the run has
**already reached** — `max_planner_reviewer_iterations` vs `planner_reviewer_iteration_current`,
`max_microtask_attempts` vs the in-flight microtask's `attempts` — because the next node's
guard (`>= limit`) would instantly re-trip and re-fail on continue. Plus per-cap hard ceilings.

**2. Rename `max_total_iterations` → `max_planner_reviewer_iterations`** across `Constraints`,
`RunDefaults`, `reviewer_node`, `service`, the settings schema, and the dashboard. It says what
it is. A Pydantic `validation_alias` (tagged `BACKCOMPAT (remove after DB wipe)`) accepts the old
key so pre-rename checkpoints/settings blobs keep loading; to be deleted at the next DB wipe.
`max_microtask_attempts` is documented as the **per-microtask** coder↔tester↔reflector cycle cap.

## Alternatives considered

**A run-wide cap on the coder↔tester↔reflector loop — REJECTED.**
Initially implemented (`Constraints.max_coder_tester_reflector_iterations`, incremented once per
coder cycle in `coder_node`, FAILED on exhaustion) to close known-bug #1. Reverted. Rationale:
with a **fixed plan** (no re-planning that grows the microtask count), total coder cycles are
already bounded by `max_microtask_attempts × microtask_count` — the per-microtask cap
(`_check_stop_limit`) makes a separate run-wide counter redundant. It would only earn its keep
if the microtask count could grow mid-run (a re-plan adding tasks), which the current graph does
not do. Adding a second, overlapping cap over-complicated the config surface (extra field,
schema entry, editable-set, verify branch, frontend wiring) for a guard the existing per-microtask
cap already provides. If re-planning that grows the plan is ever added, revisit this — the run-wide
cap becomes justified then. `Iteration.coder_tester_reflector_iteration_current` stays a dead
field (as it was), not enforced.

**Editing knobs/caps while RUNNING — REJECTED.** `graph.update_state` races the driver's
per-superstep checkpoint write; an in-flight step can silently clobber the patch. You tune a
stalled/paused run before re-driving anyway, so gating on stalled/paused loses nothing and
removes the race. (The model patch carries the same caveat but is allowed while running because
that was GH #64's explicit ask — "change the model during the run"; it's a single field read
once at the next node, lower-stakes than the numeric guards.)

## Consequences / open
- The zombie `run_state: running`-with-no-worker mislabel (a `--reload`-killed BackgroundTask
  leaves a run that reports RUNNING with no driver, so the UI offers no recovery) is **not**
  fixed here — see problems-log 2026-07-11. The recovery console assumes the run correctly
  reads STALLED; a server restart is still the manual workaround for the zombie case.
- BACKCOMPAT alias for `max_total_iterations` is temporary — remove on the next DB wipe.
