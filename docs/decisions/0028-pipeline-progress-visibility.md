# 0028 — Dashboard: surface planner-loop and microtask-loop iteration progress

Status: accepted
Date: 2026-07-08

## Context

The dashboard gave no signal of *where in the pipeline* a run currently is in terms of
iteration counts. When the reviewer rejects a plan, `planner → plan_reviewer → planner`
runs again, but the UI never showed "this is review pass 2 of 5". Similarly, it wasn't
obvious which microtask (of N) is being worked, nor which attempt (of the per-microtask
cap) is in progress — the "now working on" banner showed a bare attempt number with no
denominator.

The backend already tracked everything needed: `Iteration.planner_reviewer_iteration_current`
(incremented on every reviewer pass, capped by `Constraints.max_total_iterations`) and
`Microtask.attempts`/`Microtask.max_attempts` plus `Microtask.idx`. None of it reached the
frontend — `RunResponse` exposed only the raw `status` string, and the dashboard guessed a
coarse phase by string-matching it client-side.

One dead end worth recording: `Iteration.coder_tester_reflector_iteration_current` also
exists on the model but is never incremented anywhere in `src/`. We deliberately did not
wire it up — `Microtask.attempts` already *is* "iteration within the microtask" (one coder
attempt = one pass through coder→tester→[reflector]), so reusing it avoids resurrecting an
unused field for no added information.

## Decision

Add a `progress` block to the run-status API response, sourced entirely from the existing
LangGraph checkpoint (`snapshot.values` in `Service._response()`) — no new DB columns or
migration, since the checkpoint already carries `iteration`, `constraints`, and the full
`microtasks` list.

- **Backend** (`responses.py`, `service.py`): new `MicrotaskProgress`
  (`idx`/`total`/`title`/`status`/`attempts`/`max_attempts`) and `PipelineProgress`
  (`planner_reviewer_iteration`/`planner_reviewer_max`/`microtask`) models, exposed as
  `RunResponse.progress`. `Service._progress()` resolves the "current" microtask via
  `current_microtask_id`, falling back to the first `IN_PROGRESS`/`EXECUTING`/`VERIFYING`
  one, and fills `max_attempts` with the same fallback the coder node already uses.
- **Frontend**: rather than badges on `StatusBar`, the progress lives in `PipelineGraph` as
  a new panel above the graph — two capacity-meter cards ("plan cycle" and "microtask
  loop"), each rendered as filled/unfilled dot segments up to the cap (`Meter`, falls back
  to text for caps too large to render as dots). The meter is framed as *headroom before a
  hard limit*, not a progress-to-goal bar, since hitting the cap fails the run. The
  microtask card falls back to the durable microtask board (`GET /runs/{id}/microtasks`,
  polled) once nothing is in-flight, so a finished run still shows "N/M completed" instead
  of a stale "in progress" placeholder. `RunOverview`'s "now working on" banner gained the
  missing denominators (`#idx/total`, `attempt a/b`), fed by `progress` threaded down from
  `App.tsx`'s existing polled `run` state — no new fetch loop for that piece.

## Alternatives considered

- **Wire up `coder_tester_reflector_iteration_current` instead of reusing
  `Microtask.attempts`.** Rejected: no behavioral difference from what `attempts` already
  expresses for a single microtask; reviving unused state for a synonym adds maintenance
  burden with no new information.
- **Two badges on `StatusBar`** (the original design). Superseded during implementation:
  a compact badge can't carry both the iteration count *and* what's happening right now
  (drafting vs. reviewing vs. awaiting human review) without crowding the status bar: a
  dedicated panel above the pipeline graph had room for that plus the capacity meter.
- **Persist `max_attempts` on `MicrotaskRecord`** instead of reading it from the checkpoint.
  Rejected: the checkpoint already carries live `Microtask` objects with `max_attempts`
  intact; a DB column would duplicate that for no benefit to a live run.
- **Derive progress purely client-side from `events`/`microtasks`**, matching how the Gantt
  strip works. Rejected as the primary mechanism: the planner↔reviewer iteration count has
  no durable event today that's distinguishable by ordinal without re-deriving it —
  surfacing the number the backend already tracks in `Iteration` is more direct.

## Consequences

Makes it easier to see mid-run why a run is taking long (stuck rejecting plans vs. grinding
through microtask retries), diagnose runaway loops before they hit the `max_total_iterations`
/ `max_microtask_attempts` caps, and give HITL reviewers more context at the gate.

Costs: two new response fields to keep in sync between `responses.py` and `api.ts`
(existing pattern already requires this for every other field); the microtask card now
polls `GET /runs/{id}/microtasks` every 3s from `PipelineGraph` in addition to the existing
run-status poll. No schema/migration cost. Does not address the dead
`coder_tester_reflector_iteration_current` field — a future cleanup could remove it from
`Iteration` now that this ADR documents why it stays unused.
