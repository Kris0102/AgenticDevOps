# src/api/ — Control Plane & Dashboard Backend

**Role in the MAS:** the human's window into the system (ADR 0013). A FastAPI app
that triggers runs, pauses/resumes them at HITL gates, streams logs, and manages
repos/lessons/settings. Crucially it is **not a second engine** — it drives the same
checkpointed LangGraph graph the CLI did. It is also *not an agent*: it bypasses the
ToolGateway because it acts on behalf of the human, not an LLM.

## Files

| File | What it does |
|---|---|
| `app.py` | Routes: `POST /runs`, `/runs/{id}/resume|stop|continue|fail`, `GET /runs/{id}{,/hitl,/events,/logs/stream}`, repo registry CRUD, lesson store CRUD, settings |
| `service/service.py` | `Service` — the headless run engine: drives `graph.invoke()`, builds HITL review context, manages the active-run registry, finalizes runs (incl. sandbox teardown) |
| `requests/requests.py` | `RunRequest` — objective, repo (registry id or raw), constraints, HITL checkpoints, deploy mode, per-run LLM override |
| `responses/responses.py` | `RunResponse`, `HitlReviewContext` (plan view / green-red diff view), `EventView`, `LogView` |
| `settings_schema.py` | Typed schema for the UI settings page (incl. display conventions like thousands-scaling for token fields) |

## Core invariants (the part worth explaining)

- **One `graph.invoke()` per gate.** `POST /runs` drives to the first HITL pause or
  END; each `POST /resume` (`Command(resume=...)`) advances exactly one gate. Work
  runs in background tasks; endpoints return `202` and the dashboard polls.
- **Resume does not re-bootstrap.** A HITL pause is a mid-graph suspend in a live
  process — sandbox running, checkpoint intact. (The old CLI re-bootstrapped because
  every `--approve` was a cold process.)
- **Preflight before commit.** `POST /runs` health-checks the LLM provider and
  validates the repo/branch/deploy config first — a 4xx means *no run was created*,
  never a doomed half-run.
- **Two log feeds, by purpose:** *live* = structlog tee → in-memory `RunLogBus` →
  SSE (ephemeral, granular); *durable* = the `events` table polled by monotonic
  `seq`. The logger shows you now; the table is the record.
- **Failure taxonomy:** crashes and token-budget exhaustion mark a run **STALLED**
  (continuable — `POST /continue` can even add token budget), never silently
  FAILED. `stop` is a cooperative hard-stop; `DELETE` only works on terminal runs.
- **Concurrent runs (ADR 0032):** checkpointer on a connection pool, lock-guarded
  active-run registry with atomic test-and-set — parallel *across* runs, strictly
  single-threaded *within* one.

## Interview sound bites

- "The API and the agents share one graph and one checkpoint store — the dashboard
  is a remote control, not a reimplementation."
- "Human gates are just LangGraph interrupts surfaced over HTTP: `GET /hitl` shows
  the plan or the diff, `POST /resume` carries approve/reject plus the note."
- "Right-sized for self-hosted single-user: SSE + a seq-cursor poll instead of
  Kafka-shaped machinery."
