# 0003 — Centralize Event-row construction in `EventLogger`; close audit finding #3

Status: accepted
Date: 2026-06-07

## Context
`docs/decisions/0001-architecture-audit-2026-06-07.md` (must-fix #3) flagged that
`ToolGateway._log_event` called `self.db.commit()` inside a session it only *borrowed*
from `planner_node`'s `db.session_scope()` — breaking the Orchestrator's commit-boundary
ownership (a `tool.executed` event could become durable while the rest of that node's
writes rolled back, corrupting the audit trail).

Separately, `Event` row construction (generate `evt_{uuid.hex[:12]}` id, set
`ts`/`run_id`/`actor`/`event_type`/`payload_json`, `session.add(...)`) was duplicated
verbatim in three places: `bootstrap_node._evt`, an inline block in `orchestrator.py`'s
`planner_node`, and `gateway.py::_log_event`.

## Decision
Added `db/event_log.py::EventLogger` — a single `staticmethod log(session, run_id,
actor, event_type, payload)` that builds the `Event` row and calls `session.add()`
**only**, never `commit()`. All three call sites now route through it; `bootstrap_node._evt`
is deleted, the inline `orchestrator.py` block is replaced, and `gateway._log_event`
became a one-line delegate with its `self.db.commit()` removed — fixing audit finding #3
as a side effect of removing the duplication, not as a separate patch.

`Artifact`/`MicrotaskRecord`/`Sandbox` writes were deliberately **not** centralized —
each occurs in exactly one call site, so wrapping them would add indirection without
removing duplication (avoid abstraction beyond what the code needs).

## Alternatives considered
- **Leave `_log_event` as-is, just delete its `.commit()`** — would have fixed #3 but
  left the duplication (and the next new node would likely copy-paste `_evt` again,
  re-introducing the same commit-ownership mistake by example).
- **One generic `Persistence`/`AuditLog` class wrapping every model** — rejected as
  premature: `Event` is the only row type duplicated across files; centralizing
  single-call-site writes (`Artifact`, `MicrotaskRecord`, `Sandbox`) would be pure
  ceremony with no DRY benefit.

## Consequences
- Commit-boundary ownership now correctly sits with whichever `session_scope()` lent
  the session — matches the Blackboard ownership model in `CLAUDE.md`.
- New nodes/agents get correct event-logging behavior "for free" by importing
  `EventLogger` instead of copy-pasting a local helper — the duplication that made #3
  easy to introduce can't recur the same way.
- `CLAUDE.md` known-bugs #3 marked fixed; coding-conventions section now points at
  `EventLogger.log(...)` as the required path for all `Event` construction.
