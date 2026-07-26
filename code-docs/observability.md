# src/observability/ — Code Explanation

Three files + `__init__`, ~338 lines. The ephemeral telemetry layer: structured
logging + the live dashboard feed. Deliberately separate from the durable `events`
audit table.

---

## categories.py (26 lines)

`Category(str, enum.Enum)` — 13 logical channels: TOOL, SEMANTIC, PLANNER,
REVIEWER, CODER, TESTER, REFLECTOR, DEPLOYER, HITL, SANDBOX, BOOTSTRAP,
ORCHESTRATOR, DB. The value string lands in every log line's `category` field —
it's what you grep, what the dashboard filters, and what `LOG_LEVEL_<NAME>`
targets. `__str__` returns the value so it prints clean.

## log_bus.py (61 lines) — `RunLogBus`

The live-feed buffer. Module docstring states the design honestly: *ephemeral and
process-local BY DESIGN* — durable record is the events table; this is only "what's
on screen". Right-sized to self-hosted single-user, one process.

- `_buffers: Dict[run_id, deque(maxlen=2000)]` — bounded ring per run; a long run
  drops its oldest live lines (cosmetic loss — the tail is what matters live).
- Entries are `(seq, event_dict)` where seq comes from a **per-run monotonic
  counter** (`_counters`), NOT the deque index — the comment on `since()` explains
  why: "cursor lives in the per-run seq space so a maxlen overflow can't desync
  it." (If the cursor were an index, dropping old entries would shift it.)
- `append` / `since(run_id, cursor)` — the classic poll-with-cursor pair; `since`
  returns fresh entries + advanced cursor.
- `mark_done` / `is_done` / `reset` — lets the SSE stream close after draining
  when a run hits a terminal state (instead of heartbeating forever), and reopen
  on resume.
- One `threading.Lock` around everything — tee writes come from the run's worker
  thread, SSE reads from the async event loop.
- `log_bus = RunLogBus()` — process-wide singleton, imported by both the logging
  tee and the API service.

## logging_config.py (223 lines)

### The processor pipeline (`configure_logging`, lines 185–198) — order matters
```python
processors=[
    merge_contextvars,            # 1. inject bound run_id / microtask_id
    add_log_level,                # 2. level name into the dict
    _CategoryLevelFilter(...),    # 3. per-category threshold — may DropEvent
    TimeStamper(iso),             # 4.
    StackInfoRenderer, format_exc_info,
    _bus_tee,                     # 5. tee to dashboard AFTER filtering/enrichment
    renderer,                     # 6. JSON or colored console
]
```

### `_bus_tee` (27–35)
The bridge to the dashboard: if the event dict carries a `run_id` (i.e. contextvars
were bound — see below), copy it into `log_bus`. Positioned AFTER the level filter
(dropped lines never reach the dashboard) and AFTER merge_contextvars (so run_id is
present). Returns event_dict unchanged — pure tee.

### `_CategoryLevelFilter` (138–151)
Per-category verbosity: each line's level is compared against the threshold for
*its* category (`LOG_LEVEL_TOOL=DEBUG` etc., built by `_per_category_levels`);
below threshold → `raise structlog.DropEvent`. Plus a cheap global pre-filter:
`make_filtering_bound_logger(min_level)` uses the *minimum* of all thresholds, so
obviously-dead lines don't even enter the pipeline.

### `ColoredConsoleRenderer` (70–99)
The pretty-console path: `<dim ts> LEVEL [category] event key=val…`. Each category
has a stable ANSI hue (`_CATEGORY_COLOR`: tool=cyan, coder=green, tester=yellow…)
so you can eyeball which subsystem produced a line; only the LEVEL token is
level-colored. Color enablement: `NO_COLOR` env wins, then `LOG_COLORS`
true/false/auto (auto = `stdout.isatty()`).

### `configure_logging` details
- **Idempotent** via module `_configured` flag; `get_logger` auto-configures on
  first use so an unconfigured module still logs sanely.
- Loads `.env` FIRST (lines 160–168) — subtle real bug fixed here: configuration
  can fire at *import time* (first module-level `get_logger()`), which is before
  any module's own `load_dotenv()` — without this, `LOG_LEVEL` from `.env` was
  invisible and everything silently ran at INFO.
- `LOG_JSON=true` swaps the renderer for `JSONRenderer` (one object per line).

### Context binding (211–222)
`bind_run(run_id)` / `bind_microtask(id)` / `clear_context()` — thin wrappers over
structlog contextvars. `bind_run` is called once at run launch; every subsequent
log line on that thread auto-carries `run_id` — which is exactly what `_bus_tee`
keys on. No manual run_id threading through call sites.

---

## Relationships

```
every module:  _log = get_logger(Category.X)   (module-level, no setup)
Service/RunLauncher: bind_run(run_id)          (once per run thread)
        │
        ▼
structlog pipeline ──► console/JSON stdout      (operator view)
        └─ _bus_tee ─► log_bus ─► GET /runs/{id}/logs/stream (SSE, dashboard live view)

DURABLE channel (separate!): EventLogger → events table → GET /runs/{id}/events?after=seq
```

Exam sentence: "Two channels by design — structlog/bus is *now*, the events table
is *the record*. The bus is a lock-guarded ring buffer with a monotonic per-run
cursor; losing old live lines is fine because nothing durable lives there."
