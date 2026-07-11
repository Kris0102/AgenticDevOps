# 0032 — Concurrent runs (connection pool + same-run drive guard)

Status: accepted
Date: 2026-07-11

## Context
The control API drives each run in a FastAPI `BackgroundTasks` worker (a thread from anyio's
threadpool). Nothing stopped several runs executing at once — except the shared infrastructure
underneath them wasn't concurrency-safe:

- The LangGraph checkpointer (`orchestrator/checkpointer.py`) wrapped **one** raw psycopg
  `Connection`, held for the process lifetime, and every `graph.invoke` / `get_state` /
  `update_state` went through it. A psycopg connection is **not** thread-safe: two operations on
  it at once raise "another command already in progress" and corrupt the wire protocol. This bit
  even a SINGLE run — the dashboard polls `GET /runs/{id}` (→ `graph.get_state`) while that run's
  worker writes its next superstep (→ checkpoint write) on the same connection.
- `Service._active` (the "who's being driven now" set that separates RUNNING from STALLED) was a
  plain `set` mutated from worker threads and read from the API event loop.
- Nothing prevented **two drivers on the same run**: concurrent `POST /continue` (or `/resume`
  racing a live worker) both scheduled a `graph.invoke` on the same `thread_id` — two writers on
  one checkpoint chain.

Requirement: a single-user localhost app that can nonetheless run multiple runs simultaneously,
with no races.

## Decision

**1. Checkpointer over a psycopg `ConnectionPool`, not a shared connection.**
`CheckpointerFactory.build()` opens a `ConnectionPool` (min 2, max 20) with
`kwargs={autocommit, row_factory=dict_row, prepare_threshold=0}` and hands it to `PostgresSaver`
(which natively accepts a pool). Each checkpoint op borrows its own connection; Postgres MVCC
isolates concurrent reads/writes per row. `prepare_threshold=0` avoids "prepared statement already
exists" clashes across pooled connections. This fixes both cross-run concurrency AND the
single-run poll-races-write bug.

**2. `_active` is lock-guarded, and `mark_active` is an atomic test-and-set.**
`mark_active(run_id) -> bool` claims the run under a `threading.Lock`, returning False if a worker
already holds it. The `/resume` and `/continue` routes refuse with **409** when the claim fails —
so a run is driven by at most one worker at a time (we parallelize across runs, never within one
`thread_id`). `/continue` claims BEFORE `raise_token_budget` patches the checkpoint (and releases
via `mark_inactive` if that validation 422s), closing the mutate-then-double-drive window. The
existing racy `run_state == RUNNING → 409` check stays as a cheap fast-path; the atomic claim is
the authoritative guard.

**Already concurrency-safe, verified, left unchanged:** SQLAlchemy's engine uses the default
thread-safe `QueuePool` (one `Session` per `session_scope`); `RunLogBus` is per-run-keyed under a
`threading.Lock`; structlog's `run_id` binding is a per-task contextvar (BackgroundTasks copy the
context into their thread, so bindings don't leak between runs); each run owns its own Docker
sandbox container (DB-keyed) and its events get server-side identity `seq`s.

## Alternatives considered

- **A second raw connection per run / a connection-per-thread cache — rejected.** A pool is the
  standard, bounded, self-recycling answer; hand-rolling connection lifecycle per run invites
  leaks and an unbounded connection count. `psycopg_pool` is already a transitive dep.
- **A global lock serialising all `graph.invoke`s — rejected.** That defeats the point (no real
  concurrency) and would make a long Coder invoke block every other run's polling.
- **A real task queue / worker process (Celery/RQ) — deferred.** Correct for multi-tenant/prod,
  overkill for a single-user localhost app. The pool + claim guard make the current
  `BackgroundTasks` model correct for concurrent runs here; the "move to a real worker before
  prod" note stands (CLAUDE.md) for when scale demands it.

## Consequences / open
- Verified under load: 200 concurrent `get_state` and 300 mixed read/write ops across 10
  `thread_id`s / 20 threads completed with zero protocol errors (the old single connection would
  raise under this).
- Pool max is 20 — comfortably above any realistic concurrent-run count for one user; revisit if
  that assumption changes.
- Concurrent DIFFERENT ops on the SAME run (e.g. `set_agent_model`'s `update_state` while the
  worker invokes) remains the pre-existing, documented single-field race (ADR 0031), out of scope
  here — this ADR is about running multiple DISTINCT runs safely.
- The zombie `run_state: running`-with-no-worker mislabel (problems-log 2026-07-11) is unrelated
  and still open.
