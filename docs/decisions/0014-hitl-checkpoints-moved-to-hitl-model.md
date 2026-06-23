# 0014 — `hitl_checkpoints` moved from `Policy` to the `HITL` state model

Date: 2026-06-23
Status: Accepted (supersedes the `Policy.hitl_checkpoints` part of ADR 0002)

## Context
ADR 0002 added `hitl_checkpoints: List[str]` to `Policy` as the per-run list of
phases that pause at `NEEDS_HITL_APPROVAL`. In practice every consumer of that field
is HITL machinery (`HitlGate.apply`, the `hitl` node), and the rest of the HITL runtime
state already lives on the `HITL` model (`required`, `pending_approvals`). Keeping the
checkpoint list on `Policy` split one concept across two models and forced
`HitlGate.apply` to reach past `state["policy"]` for config it owns.

A new `Phase` enum (`PLAN`/`CODE`/`TEST`/`DEPLOY`) was also introduced in `state.py`
(not `hitl.py`, to avoid a state↔hitl import cycle — `hitl.py` imports it back). It is
the natural type for the checkpoint list.

## Decision
- Move the field to `HITL`: `HITL.hitl_checkpoints: List[Phase]`.
- Retype `List[str]` → `List[Phase]` (was stringly-typed).
- `create_initial_blackboard_state` now takes a `hitl: HITL` arg; callers
  (`main.py`, `api/service/service.py` via `RunRequest.hitl`) build the `HITL`
  with the checkpoints and pass it in, instead of stuffing them into `Policy`.
- `HitlGate.apply` compares `phase not in hitl.hitl_checkpoints` (enum-to-enum),
  not `phase.value not in ...` (which only worked by `str`-enum coincidence).

## Consequences
- One model owns HITL config + runtime state. `Policy` no longer carries it.
- Stronger typing: an invalid checkpoint name fails at `Phase(...)` construction
  (in `main.py` arg parsing), not silently inside the gate.
- Docs that said `policy.hitl_checkpoints` updated to `hitl.hitl_checkpoints`
  (CLAUDE.md, ARCHITECTURE_preview.md, orchestrator/state/hitl comments). ADR 0002's
  reasoning for *having* a per-phase checkpoint list still stands — only its home moved.
