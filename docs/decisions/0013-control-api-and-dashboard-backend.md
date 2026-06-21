# 0013 — Control API + dashboard backend (HITL over HTTP, live logs via SSE)

Status: accepted
Date: 2026-06-21

## Context

The dashboard (a separate frontend project, ADR-pending) needs to: trigger runs, watch
them, **see what a paused HITL gate is about** (the plan at a `plan` gate, the green/red
diff at a `code` gate), approve/reject with a note, and watch logs in real time. The CLI
(`src/main.py`) is being **deleted** — the HTTP API becomes the only surface, so it must
carry everything the CLI's interactive `input()` loop did, plus the review context the
terminal never showed.

Key constraint that drove every choice: **deployment model is self-hosted, single-user,
localhost — maybe a packaged exe** (see memory `project-deployment-model`). Not a hosted
multi-tenant SaaS. So: one process, one user, no flaky-internet reconnects, no horizontal
scale, restart tolerance only where it's cheap. An earlier draft over-engineered the log
transport for SaaS scale (resumable `Last-Event-ID` SSE, LISTEN/NOTIFY-vs-Redis analysis);
that was cut.

## Decision

A FastAPI control plane in the MAS repo at `src/api/` (`app.py` routes, `service/Service`
engine, `requests/`, `responses/`). It is **not** an LLM agent and does not go through the
ToolGateway — it drives the existing checkpointed graph.

**Run lifecycle = one `graph.invoke()` per gate.** A single invoke drives the graph to the
next `interrupt()` or END (LangGraph batches the supersteps). So:
- `POST /runs` → mint run_id, `BootStrapper` (clone + sandbox), invoke → pauses at gate 1.
  Runs in a `BackgroundTasks` worker; returns `202` immediately.
- `POST /runs/{id}/resume` → deliver the verdict with `Command(resume=HitlResume…)`, invoke
  → advances exactly one gate. **No bootstrap on resume** (unlike the CLI): a HITL pause is
  a mid-graph suspend in the same live process — sandbox still up, checkpoint intact. The
  CLI re-bootstrapped only because each `--approve` was a cold separate process (ADR 0005).
- Approve/reject routing is unchanged — `HitlGate.resolve()` sets the next status; the note
  feeds the existing channels (plan→`review_feedback`, code/test→a `Reflection`, ADR 0006).

**Notification = poll, not push.** `GET /runs/{id}` returns `run_state` (derived from the
blackboard status + pending interrupt, no parallel field) plus `hitl_phase` / `hitl_approval_id`
when paused — one poll tells the dashboard a gate is waiting and which kind. `HitlGate.apply`
already writes a durable `hitl.requested` event too.

**HITL review context** assembled on demand (`GET /runs/{id}/hitl`): plan + microtasks for a
`plan` gate (from checkpoint state), raw unified diff for a `code` gate (read from
`artifacts.content_text` via `evidence.latest_diff.diff_artifact_ref`). The large diff text
never rides the `interrupt()` payload — kept out of the per-superstep checkpoint.

**Two log streams, two mechanisms, matched to what each is for:**
- **Live logs** = the existing structlog logger, tee'd by a `_bus_tee` processor into an
  in-memory per-run ring buffer (`observability/log_bus.py`), drained over SSE
  (`GET /runs/{id}/logs/stream`, `GET /runs/{id}/logs` poll fallback). Ephemeral, granular
  (`node.enter`, tool steps), zero schema. The tee sits after the level filter (respects
  `LOG_LEVEL`) and after `merge_contextvars` (so `run_id` is present).
- **Durable history** = the `events` audit table, polled (`GET /runs/{id}/events?after=<seq>`).
  A new `seq BIGSERIAL` identity column (migration `a1b2c3d4e5f6`) is the monotonic cursor +
  ordering guarantee; `ts` alone is collision-prone. `run.completed` is now emitted on a clean
  DONE (it never was) so terminal state is observable without reading the checkpoint from the
  stream thread.

**Incidental fix:** `Phase` moved from `hitl.py` into `state.py` to break a `state ↔ hitl`
import cycle that only manifested when `state` was imported first (the API's import order).
`hitl.py` re-exports it, so every `from orchestrator.hitl import Phase` still works.

## Alternatives considered

- **Push (SSE+LISTEN/NOTIFY / WebSocket) for live logs** — rejected for this deployment.
  LISTEN/NOTIFY pins a DB connection per stream, has no replay, breaks across instances, 8KB
  payload cap; WebSocket is bidirectional overkill for a one-way feed. On localhost a ~0.5–1s
  in-memory poll is imperceptible.
- **Resumable `Last-Event-ID` SSE over the events table for live logs** — built, then cut.
  Solves internet drops + horizontal scale + exactly-once reconnect — none of which exist for
  a single local user. The events table stays as *history* (poll), not the live feed.
- **"Just stream the logger, skip the DB"** — insufficient alone: structlog is ephemeral
  (restart = scrollback gone, no past-run browse) and can't carry the structured plan/diff a
  gate needs. Hence both: logger for live, events table for the durable record.
- **Re-bootstrap on resume (CLI parity)** — rejected; redundant in a long-lived process and
  it dragged clone/state-merge logic into a path that only needs to wake an `interrupt()`.
- **Graph execution in a real worker (Celery/arq/Temporal)** — deferred. `BackgroundTasks`
  is fine for single-user MVP but won't survive a deploy/restart mid-run. Flagged in
  `Service` docstring as the load-bearing production debt.

## Consequences

- The HTTP API fully replaces the CLI for runs + HITL; the dashboard is a pure client over
  REST + SSE. Frontend stays thin (no MAS imports).
- Live logs reuse infra that already existed (structlog) — ~80 lines, no schema. History gets
  a durable, ordered, resumable-by-poll feed via `events.seq`.
- Sharp edges, all flagged in code: (1) `graph`/`checkpointer` share one psycopg connection —
  true parallel runs need a pool; (2) `BackgroundTasks` execution doesn't survive process
  restart — move to a worker before multi-run/production; (3) the live bus is per-process and
  bounded (oldest live lines drop on a long run — cosmetic, the events table is the record).
- `run.completed` now exists on the happy path — anything keying off run terminality (the log
  stream close, future metrics) can rely on it, not just `run.failed`.
