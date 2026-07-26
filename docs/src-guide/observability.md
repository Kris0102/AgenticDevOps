# src/observability/ — Structured Logging & Live Streaming

**Role in the MAS:** the *ephemeral* telemetry layer — what the operator watches
while a run is live. Deliberately separate from the durable `events` audit table:
logs show you *now*, events are *the record*. Both exist on purpose (ADR 0013).

## Files

| File | What it does |
|---|---|
| `logging_config.py` | structlog setup: console-pretty or JSON-lines output, global + per-category levels (`LOG_LEVEL`, `LOG_LEVEL_TOOL=DEBUG`, …), and the `_bus_tee` processor that copies every log line into the log bus |
| `log_bus.py` | `RunLogBus` — in-memory per-run ring buffer. The dashboard's SSE endpoint (`GET /runs/{id}/logs/stream`) reads from here for live, granular output (`node.enter`, tool steps) |
| `categories.py` | The `Category` enum (TOOL, SEMANTIC, ORCHESTRATOR, …) — every logger is category-scoped, so noisy subsystems can be turned up/down independently |

## Conventions (enforced project-wide)

- Never `print()` — always
  `_log = get_logger(Category.X)` then `_log.info("dotted.event.name", key=val)`.
- `run_id` is bound once at run launch and auto-attached to every line after.
- Logging here does **not** replace `EventLogger` events — operational logs and the
  audit trail are separate channels with separate lifetimes.
- The package is named `observability/`, not `logging/` (stdlib shadowing).

## Design rationale

Right-sized for a self-hosted, single-user deployment: an in-memory ring buffer +
SSE gives live tail-following with zero extra infrastructure; if the process
restarts, the ephemeral feed is gone but nothing of record is lost — the durable
history lives in Postgres and the dashboard reconciles from `events?after=<seq>`.

## Interview sound bites

- "Two telemetry channels by design: structlog for watching a run live, the events
  table for auditing it afterwards — different lifetimes, different guarantees."
- "Per-category log levels mean you can put just the tool layer in DEBUG while the
  rest stays quiet."
