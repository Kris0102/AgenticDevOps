# 0006 — Mid-run HITL gates via native LangGraph `interrupt()`

Status: accepted
Date: 2026-06-08

## Context
`policy.hitl_checkpoints`, the `HITL` model, and `RunStatus.NEEDS_HITL_APPROVAL` were
fully spec'd (`ARCHITECTURE_preview.md` §6.2, ADR 0002, `Blackboard.md` §3.J) but
**declared and never read** — ADR 0002 deferred the router logic as "not yet built".
We needed a human to be able to approve/reject the run at each phase boundary named in
`policy.hitl_checkpoints` (`plan`/`code`/`test`), pausing the run until a decision
arrives. This is the auditability + safety veto-point property in CLAUDE.md "Key
architecture decisions". The checkpointer migration (ADR 0005) had just landed, so a
pause/resume primitive was available.

## Decision
Use LangGraph's **native `interrupt()` / `Command(resume=...)`** (langgraph 1.2.2,
already installed) as the single HITL mechanism.

- A phase node funnels its would-be result through `HitlGate.apply(phase, result, state)`
  (`src/orchestrator/hitl.py`). If `phase ∈ policy.hitl_checkpoints` **and** the node
  produced the forward status that gate guards (`GATES[phase]["trigger"]`), the status
  is rewritten to `NEEDS_HITL_APPROVAL`, a `pending_approval` is appended, and a
  `hitl.requested` event is emitted.
- The phase routers send `NEEDS_HITL_APPROVAL` to a single shared `hitl` node
  (`OrchestratorGraph._hitl`), which calls `interrupt()` to suspend the graph. Reject
  without a note re-`interrupt()`s rather than routing on un-actionable input.
- Resume via `graph.invoke(Command(resume={decision, note, reviewer}))`.
  `HitlGate.resolve` marks the approval, emits `hitl.decided`, and returns the next
  status: **approve** re-emits the gate's trigger status (advance); **reject** loops back
  to the owning phase carrying the mandatory note into that phase's feedback channel
  (`plan` → `plan.review_feedback`; `code`/`test` → a `Reflection` for the reflector).
- `main.py` gains `--hitl-checkpoints`, `--approve`, `--reject`, `--note`. On resume, if a
  task interrupt is pending and a decision flag is set, it passes a `Command` instead of
  `None`. Reject requires a note (enforced at CLI and in the `hitl` node).

Reject routing is stage-dependent and reuses existing loops, preserving the invariant
that **only the reflector routes back to the coder** (ADR-less but established this
session): `code`/`test` rejects go through the reflector, never straight to the coder.

## Alternatives considered
**Status-halt at END + `update_state(as_node=...)` resume.** Reuse the existing
status-routers (which already return END for `NEEDS_HITL_APPROVAL`) as the "pause", then
on approval call `graph.update_state(config, {...}, as_node=<gated_node>)` to re-arm the
conditional edge and `graph.invoke(None)` to continue. Rejected because: (1) re-arming an
edge after the graph already reached END is a subtle, fragile dance; (2) it conflates
DONE vs PAUSED — both land at END, so a paused run is indistinguishable from a finished
one; (3) it scatters `NEEDS_HITL_APPROVAL` emission and next-status stashing across every
phase node. `interrupt()` suspends in place, keeps PAUSED observable
(`graph.get_state().next == ('hitl',)`, `task.interrupts` non-empty), and needs no
edge re-arming. The only costs — one `Command` branch in `main.py` resume and bumping the
`langgraph` pin to `>=1.0` — are trivial.

## Consequences
- One gate code path (`HitlGate`) for all three checkpoints, configured per-run by
  `policy.hitl_checkpoints` — satisfies ADR 0002's "one gate mechanism" intent.
- PAUSED is now a first-class, observable state distinct from DONE/FAILED.
- New `EventType.HITL_REQUESTED` / `HITL_DECIDED` give the gate a full audit trail.
- Requires `langgraph>=1.0` (was `>=0.0.27`; 1.2.2 installed).
- `GATES["test"]["trigger"]` is provisional (`DONE`) until the real Tester defines its
  green-check status — revisit when the Tester stub becomes a real agent.
- The `code`/`test` reject path depends on the reflector consuming a seeded `Reflection`;
  the reflector is still a stub, so the loop is wired but not yet acted on.
