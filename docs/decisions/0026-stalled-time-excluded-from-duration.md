# 0026 — Exclude STALLED time from displayed run duration/Gantt

Date: 2026-07-07
Status: Accepted (implemented)

## Context
Reported bug: while a run sits STALLED, "the timer keeps running," so the runtime shown
once the run finishes is wrong. Investigation (see `RunOverview.tsx`, `service.py`)
found the backend has no runtime/duration field at all (`Run` only has `created_at`/
`updated_at`); the only "duration" the user sees is the dashboard's
`RunOverview` duration tile, computed client-side as `last_event.ts − first_event.ts`
over the durable `events` table, plus a phase Gantt strip (`buildPhases()`) that slices
the same span by `run.status_changed` boundaries.

STALLED itself is never persisted (`RunStatus`/`Run.status` has no STALLED member — it's
derived at read time in `_derive_state` from "no in-process worker is driving this
checkpoint", per ADR 0021). The only durable trace of a stall was the `run.stalled`
event `_mark_stalled` writes on token-budget exhaustion, transient LLM errors, or an
uncaught crash. There was no counterpart marking when a stall *ends*, so nothing could
exclude that span: the moment a human called `/continue` and new events started
flowing again, the entire idle wall-clock gap (minutes to potentially days) got silently
folded into `last − first`, and attributed in full to whichever phase happened to be
active when the stall hit.

Checkpoints (LangGraph `PostgresSaver`) were checked and ruled out as a contributing
factor — nothing in this codebase reads a checkpoint's internal timestamp for duration
purposes; `continue_run()`'s `graph.invoke(None, ...)` resume doesn't touch any
duration-relevant field.

## Decision
Give the stall span an explicit start **and** end marker, then have the dashboard
subtract that span:

- New `EventType.RUN_RESUMED = "run.resumed"` (`orchestrator/state.py`), emitted by
  `continue_run()` (`api/service/service.py`) immediately before re-driving the graph —
  symmetric with the existing `run.stalled` emitted by `_mark_stalled`.
- `RunOverview.tsx` pairs each `run.stalled` with the next chronological `run.resumed`
  into `[stallStart, stallEnd]` intervals. The duration tile subtracts the summed overlap
  of these intervals from `last − first`. `buildPhases()` clips the same overlap out of
  whichever `[from, to)` segment it falls in, so idle time waiting on a human no longer
  inflates that phase's reported share.
- An unpaired trailing `run.stalled` (run is *currently* stalled) needs no special case:
  there's no later event yet for it to inflate, so the existing duration calc already
  stops growing at that point.

## Alternatives considered
- **Backend-computed duration field on `Run`** (e.g. `active_ms` accumulated server-side)
  — more robust long-term, but a bigger surface change (new column/migration, updates on
  every stall/resume) for a bug that's purely a display-layer miscalculation today.
  Revisit if a second consumer of "actual active time" shows up beyond this one tile.
- **Heartbeat-based stall detection** to also cover a full worker-process crash (OOM-kill,
  host reboot) where `_drive`'s except clauses never run and no `run.stalled` event is
  written — rejected for now: no heartbeat infra exists, and this is a narrower edge case
  than the explicit stalls (token budget, transient LLM errors) already covered. Left as a
  known, documented gap rather than solved.

## Consequences
- Duration and the phase Gantt now reflect actual active work time across a
  stall→continue cycle instead of ballooning by however long the run sat idle.
- A run stalled by a silent full-process crash (no `run.stalled` event ever written)
  still has that gap counted — accepted limitation, would need a heartbeat mechanism to
  close.
- No backend schema change, no migration; the fix is one new event type plus one emission
  site, and pure-function changes in the dashboard (verified against synthetic event data:
  a run with 20s + 10s of real work and a 1hr stall in between now reports 30s, not
  3640s).
