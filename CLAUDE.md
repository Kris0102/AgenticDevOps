
///# AgenticDevOps — Project Guide for Claude

## What this is
Multi-agent system that autonomously plans, implements, tests, and proposes code changes to an existing repo. Triggered by CLI (MVP) or GitHub webhook (future). Uses Docker-per-run sandboxing and Postgres for full audit trail.

## Key architecture decisions
- **Blackboard (run_state)**: single JSON state owned exclusively by Orchestrator. Agents never write state directly.
- **Grounded verification**: code is only accepted when deterministic checks (lint/tests/build) pass. No LLM-only review.
- **Tool Gateway**: all environment actions (file reads, shell commands, git) go through `ToolGateway` — deny-by-default, every call logged.
- **Star topology**: all agent-to-agent communication is mediated by Orchestrator via blackboard, not direct calls.
- **Docker-per-run**: each run gets its own container. Sandbox is the source of truth for code during a run.
- **Bootstrap always first**: `bootstrapper_node` runs unconditionally on every invocation (upserts Run, ensures sandbox running, clones on first run). No side effects in routers.
- **DB-backed sandbox registry**: sandbox container IDs stored in `sandboxes` table — survives process restarts, no in-memory dict.

## Project layout
```
src/
  main.py                        # CLI entrypoint
  orchestrator/
    orchestrator.py              # LangGraph graph + routing
    state.py                     # Pydantic BlackboardState + all models
    bootstrap_node.py            # BootStrapper class — always runs first
  agents/
    Planning/
      Planner/
        planner.py                 # Orchestration only: investigate → synthesize → validate (with retry)
        planner_tools.py           # Tool wrappers for planner
        quality.py                 # _quality_report / _normalize_output — microtask quality gate
        plan_output.py             # create_plan_file_as_output — debug plan dump to disk
        investigation_support.py   # _default_max_tool_steps, _investigation_recursion_limit, _last_ai_text, _log_investigation_steps
        prompts/                   # Prompt templates (investigator.py, synthesis.py)
      Reviewer/
        reviewer.py                # Plan Reviewer — two-phase (investigate + review), structured approve/reject
        reviewer_tools.py          # ReviewerTools: gateway-routed read/exec/semantic-search (read-only)
        review_output.py           # create_review_file_as_output — debug review dump to disk
        investigation_support.py   # shared investigation helpers (mirrors planner)
        prompts/reviewer.py        # REVIEWER_SYSTEM_PROMPT + build_investigation/review_prompt
    Implementing/
      Coder/
        coder.py                 # Coder — single ReAct loop, deterministic repo.diff verification
        coder_tools.py           # CoderTools: gateway-routed read/write/diff/exec + path normalization
        prompt/coder_prompt.py   # CODER_SYSTEM_PROMPT + build_coder_prompt (full grounding context)
      tester.py                  # Tester (stub)
  Exceptions/
    BadMicroTaskExceptions.py    # BadMicroTaskException — raised by quality.py, caught+retried in Planner
  tool_gateway/
    gateway.py                   # ToolGateway: allowlist enforcement (via permissions.get_permissions) + audit logging
    permissions.py               # RolePermissions subclasses — per-role tool + sandbox-cmd allowlists
    Roles.py                     # AgentRole enum
  sandbox/
    sandbox_tools.py             # DockerSandboxTools: create/exec/destroy + repo_diff (DB-backed). Pure sandbox exec — no remote git
    Tools.py                     # Tools enum — sandbox tool identifiers, dispatched via match/case in gateway
    Dockerfile                   # Base sandbox image (agentic-sandbox:0.1)
  gitops/
    gitops.py                    # GitOps — GitHub boundary (ADR 0012): clone_and_checkout, push_branch (agent/run-<run_id>), create_pr (stub). Token centralized here
  db/
    db.py                        # SQLAlchemy engine + session_scope
    event_log.py                 # EventLogger — centralized Event-row construction (session.add only, never commit)
    models.py                    # ORM models: Run, Event, Artifact, Sandbox, MicrotaskRecord, CodeChunk
    embedder.py                  # Embedder — lazy sentence-transformers (all-MiniLM-L6-v2, 384-dim)
    semantic_store.py            # SemanticStore — pgvector upsert/search/delete over code_chunks
    migrations/                  # Alembic migrations
  context_engine/                # Code semantic memory (ADR 0010)
    chunker.py                   # tree-sitter AST chunking (function/class) + whole-file fallback
    engine.py                    # ContextEngine: ingest_baseline / sync_workspace / search
  providers/                     # Pluggable LLM provider interface (TODO: implement)
  api/                           # FastAPI control plane + dashboard backend (ADR 0013) — NOT an LLM agent, bypasses ToolGateway
    app.py                       # Routes: POST /runs, /runs/{id}/resume; GET /runs/{id}{,/hitl,/events,/logs,/logs/stream}
    service/service.py           # Service — headless run engine: run/resume (one graph.invoke per gate), HITL review context, log+event feeds
    requests/requests.py         # RunRequest (objective/repo/constraints/policy/hitl/trigger)
    responses/responses.py       # RunResponse, HitlReviewContext (PlanView/DiffView), EventView (durable history), LogView (live)
  observability/
    log_bus.py                   # RunLogBus — in-memory per-run ring buffer; structlog tee → dashboard SSE (live logs, ephemeral). ADR 0013
    logging_config.py            # structlog setup + _bus_tee processor (feeds log_bus)
deploy/
  docker-compose.yml             # Postgres + pgAdmin + sandbox image builder
scripts/
  deleting_db_data_episodic_memory.sh  # Wipe all run data (keeps schema)
  snapshot_db.sh                       # Dump DB to scripts/snapshots/<label>.sql
  load_snapshot.sh                     # Restore DB from snapshot
docs/
  ARCHITECTURE_preview.md        # Full system spec (state machine, data flow)
  Design_preview.md              # Agent patterns and cognitive architecture
  MCP_AND_TOOLS.md               # Tool Gateway + MCP contracts
  Blackboard.md                  # Blackboard schema + event type registry
  POC_instructions.md            # Step-by-step PoC build guide
  instructions.md                # MVP implementation plan

  decisions/                     # ADRs — WHY decisions were made (read this for cross-session context, see below)
```

## Persistent memory across sessions: `docs/decisions/`
This file (`CLAUDE.md`) tells you **what's true now** — it gets rewritten and loses
history. `docs/decisions/` tells you **why** — append-only, dated ADRs (`NNNN-title.md`)
that are never deleted, only superseded. Read it when starting fresh on this project;
write to it (new `NNNN-title.md`, format in `docs/decisions/README.md`) whenever a
non-obvious architectural decision is made, an audit finding gets actioned/deferred, or
an alternative gets seriously considered and rejected — so the next session doesn't
re-litigate it cold. See `docs/decisions/0001-architecture-audit-2026-06-07.md` for the
first architecture audit and its remediation order.

## Graph topology (current)
```
START → planner → plan_reviewer → (planner | coder | hitl)        # planning loop
                  coder → tester → (reflector | hitl)             # execution loop
                  tester → reflector → coder
                  tester → deploy → (hitl) → deploy_push → END    # deploy (terminal)
                  hitl → (planner | coder | tester | reflector | deploy_push | END)

bootstrapper runs OUTSIDE the graph (RunLauncher calls it directly) — see ADR 0005.
```
- **Deploy (terminal)**: on `DONE` the tester routes to `deploy` (gate producer →
  emits `READY_TO_DEPLOY`, funnels through the shared `hitl` gate). Approve →
  `deploy_push` drives `gitops.GitOps` to stage/commit/push the sandbox to
  `agent/run-<run_id>` (bypasses ToolGateway — not an LLM agent), then END. Reject → `DONE` → END
  (run finished, un-deployed). See ADR 0012.
- **Planning loop**: `planner → plan_reviewer`. Reviewer rejects (`NEEDS_PLAN`) → back to
  planner to revise; approves (`NEEDS_CODE`) → coder.
- **Execution loop**: `coder → tester`. On test failure (`REFLECT`) tester goes to
  `reflector`, which diagnoses and is the SOLE path back to coder after a FAILURE
  (`NEEDS_CODE`) — no blind retries off an undiagnosed failure. On a PASS with more
  PENDING microtasks, tester emits `NEEDS_CODE` itself and routes straight back to
  coder for the next microtask (not a failure retry, so reflector isn't involved).
- **HITL gate** (shared `hitl` node, see below + ADR 0006): any phase in
  `policy.hitl_checkpoints` routes through `hitl` before advancing.

## When HITL is triggered
A phase emits `NEEDS_HITL_APPROVAL` (via `HitlGate.apply`, `orchestrator/hitl.py`) and the
graph suspends at the `hitl` node (`interrupt()`) ONLY when that phase is listed in
`policy.hitl_checkpoints` (set with `--hitl-checkpoints plan,code,test`; empty = no gates).
The three gate points and their resume routing:

| Gate   | fires after            | approve →        | reject (note required) → |
|--------|------------------------|------------------|--------------------------|
| `plan` | plan_reviewer approves | coder            | planner (note → `plan.review_feedback`) |
| `code` | coder produces a diff  | tester           | **reflector** → coder (note → a `Reflection`) |
| `test` | tester passes          | next microtask   | **reflector** → coder (note → a `Reflection`) |
| `deploy` | tester emits `DONE`  | `deploy_push` (push) | END, un-deployed (`DONE`; no loop-back, no note channel) |

Reject is stage-dependent and ALWAYS requires a note (no note = re-`interrupt()`, stays
paused). `code`/`test` reject does NOT go straight to the coder — it goes through the
reflector (carrying the human note as the diagnosis seed), preserving the
only-reflector-talks-to-coder invariant. Resume via
`python main.py --run_id <id> --approve | --reject --note "..."`.

## Run the pipeline
```bash
# 1. Start Postgres
docker compose -f deploy/docker-compose.yml up -d

# 2. Build sandbox image (only when Dockerfile changes)
docker compose -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder

# 3. Run migrations
cd /home/kristian/project/AgenticDevOps && venv/bin/alembic upgrade head

# 4a. Run via CLI (being deleted — see ADR 0013; prefer the API below)
cd src
../venv/bin/python main.py \
  --repo_url https://github.com/owner/repo \
  --base-sha <SHA> \          # omit for brand-new repos with no commits
  --task_description "Your task here"

# 4b. Run via Control API (dashboard backend — the going-forward surface)
cd src
../venv/bin/uvicorn api.app:app --port 8000     # serves REST + SSE; needs Postgres up
#   POST /runs                         trigger a run (body = RunRequest)
#   GET  /runs/{id}                    poll status / run_state / hitl_phase
#   GET  /runs/{id}/hitl               paused-gate review context (plan or green/red diff)
#   POST /runs/{id}/resume             {decision: approve|reject, note}
#   GET  /runs/{id}/logs/stream        live logs over SSE (structlog bus)
#   GET  /runs/{id}/events?after=<seq> durable history (events audit table)
```

## Control API ↔ graph (ADR 0013)
The API in `src/api/` is the dashboard's only backend — it drives the **same checkpointed
graph**, it is not a second engine. Key invariants:
- **One `graph.invoke()` per gate.** `POST /runs` drives to the first HITL pause or END;
  each `POST /resume` (`Command(resume=HitlResume…)`) advances exactly one gate. Runs in a
  `BackgroundTasks` worker → endpoints return `202` immediately; the dashboard polls.
- **Resume does NOT re-bootstrap** (the CLI did, because each `--approve` was a cold process).
  A HITL pause is a mid-graph suspend in the same live process — sandbox + checkpoint intact.
- **Approve/reject routing is unchanged** — `HitlGate.resolve()` sets the next status and
  routes the note into the existing channel (plan→`review_feedback`, code/test→`Reflection`).
- **Two log feeds, by purpose:** *live* = structlog tee'd into `observability/log_bus.py` →
  SSE (ephemeral, granular: `node.enter`, tool steps); *durable history* = `events` table
  polled by `seq`. The logger is the live source; the table is the record. Right-sized to the
  self-hosted single-user deployment — no resumable-SSE / LISTEN-NOTIFY machinery.

## DB management scripts
```bash
# Wipe all data for a fresh run (keeps schema/migrations)
./scripts/deleting_db_data_episodic_memory.sh

# Save DB snapshot before a run
./scripts/snapshot_db.sh <label>

# Restore from snapshot
./scripts/load_snapshot.sh scripts/snapshots/snapshot_<label>.sql
```
Scripts use `docker exec` into `deploy-postgres-1` — no local Postgres client needed.
Override container name: `POSTGRES_CONTAINER=<name>` in `.env`.

## Environment variables (`.env` in project root)
| Variable | Purpose |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` | Single source of truth for DB creds — docker-compose inits the DB from them; app (`db.py`) + Alembic build the connection URL from them (`Database.sqlalchemy_url()`). Also used by DB scripts |
| `DATABASE_URL` | Optional override — set to point app + Alembic at an **external** Postgres (RDS/Supabase). When set, wins over `POSTGRES_*`. Leave unset for the local compose DB |
| `CLUSTER_API_KEY` | API key for LLM cluster endpoint |
| `LLM_MODEL` | Model name for LLM cluster endpoint (default `google/gemma-4-31B-it`) |
| `USE_TOOLS` | `true` to enable planner's ReAct investigation tools |
| `PLANNER_MAX_TOOL_STEPS` | Max tool call rounds for planner investigation |
| `PLANNER_MAX_SYNTHESIS_ATTEMPTS` | Max synthesis retries when `_normalize_output` raises `BadMicroTaskException` (default 3) — Planner feeds the validation error back into the next prompt as context |
| `CODER_MAX_TOOL_STEPS` | Max tool call rounds for the coder's ReAct loop (default 16) |
| `CODER_MAX_EMPTY_DIFF_RETRIES` | Re-prompt attempts when the post-loop `repo.diff` comes back empty (default 2) |
| `CODER_TIMEOUT_SECONDS` | LLM request timeout for the Coder (default 120 — code gen is long) |
| `TEAR_DOWN_SANDBOX_AFTER_RUN` | Destroy container after run (set `false` during dev) |
| `GITHUB_TOKEN` | Required for private repos — injected into clone URL, never logged |
| `EMBEDDING_MODEL` | sentence-transformers model for the Context Engine (default `all-MiniLM-L6-v2`, 384-dim). Changing it requires matching `EMBEDDING_DIM` + a new `code_chunks` migration |
| `LOG_LEVEL` | Global structlog threshold (default `INFO`) |
| `LOG_LEVEL_<CATEGORY>` | Per-category override, e.g. `LOG_LEVEL_TOOL=DEBUG`, `LOG_LEVEL_SEMANTIC=DEBUG` (categories in `observability/categories.py`) |
| `LOG_JSON` | `true` → one JSON object per line (prod); `false` → pretty console (default) |
| `LOG_COLORS` | `auto` (color when stdout is a TTY, default) \| `true` \| `false`. `NO_COLOR` env also disables |

## LLM provider
Currently using `ChatOpenAI` with a custom cluster endpoint (`http://131.159.60.153:3000/inference`) running `google/gemma-4-31B-it`. All agents should eventually use `providers/base.py` abstraction (not yet implemented).

## DB schema (all tables)
| Table | Purpose |
|---|---|
| `runs` | One row per run — repo_url, base_sha (nullable), status, timestamps |
| `sandboxes` | Docker container ID per run — survives restarts |
| `microtasks` | Planner output persisted after each plan — survives crashes |
| `artifacts` | Plan summaries, test logs, diffs — kind field identifies type |
| `events` | Full audit trail — run.created, repo.cloned, plan.drafted, tool.executed, etc. Has a monotonic `seq` (BIGSERIAL identity) — the dashboard history cursor (`GET /runs/{id}/events?after=<seq>`). See ADR 0013 |
| `code_chunks` | Context Engine semantic index — AST chunks + `Vector(384)` embedding, `namespace` baseline/workspace (ADR 0010). Needs the `vector` extension (`pgvector/pgvector` image) |
| `checkpoints*` | LangGraph PostgresSaver — blackboard snapshot per superstep (ADR 0005; replaced `run_state`) |

Models use **SQLAlchemy 2.0** style: `Mapped[T]` + `mapped_column()`. Never use 1.x `Column()` style — Pylance will report type errors.

## Current implementation status
| Component | Status |
|---|---|
| Blackboard / state models | Done |
| DB schema + Alembic | Done |
| DockerSandboxTools (DB-backed) | Done |
| ToolGateway | Done |
| Bootstrapper node | Done |
| Planner (ReAct + structured output) | Done |
| DB persistence (microtasks, events, artifacts, Run.status) | Done |
| Planner↔Reviewer + Coder→Tester→Reflector loops (wired) | Done |
| HITL gates (plan/code/test, native `interrupt()`) | Done — see ADR 0006 |
| Plan Reviewer | Done — two-phase (independent investigation via read-only tools, then structured approve/reject). Appends a `ReviewFeedback` to the plan, emits `PLAN_REVIEWED`, caps the planner↔reviewer loop at `Constraints.max_total_iterations` (→ FAILED + `RUN_FAILED`). See ADR 0011 |
| Coder | Done — hybrid grounding (runner pre-reads target/context files), single ReAct loop, deterministic diff verification, per-microtask attempt limit enforced |
| Tester | Minimal stub — marks current microtask COMPLETED, `unit_tests` check recorded as "skipped" (not actually run), routes back to coder (more PENDING) or DONE. Unblocks coder<->tester loop end-to-end; real test execution still TODO |
| Reflector | Stub (returns `{}`) — not yet exercised since the stub Tester never emits `REFLECT` |
| Context Engine (pgvector) | Done — code semantic memory: `baseline`+`workspace` namespaces, local MiniLM (384-dim) embeddings, tree-sitter chunking, `semantic.search` Planner tool. Baseline ingested in Bootstrap after clone; workspace synced by coder node after a diff; purged on sandbox teardown. See ADR 0010 |
| GitOps (webhooks + PR) | Stub — `gitops.GitOps` class (GitHub boundary, ARCHITECTURE §4.4) owns branch/commit/push; terminal `deploy`/`deploy_push` nodes drive it to push the sandbox to `agent/run-<run_id>` (bypasses ToolGateway — not an agent), HITL-gateable (`--hitl-checkpoints deploy`), emits `gitops.pushed`. `GitOps.create_pr` is a stub (no GitHub API yet); no webhooks/CI relay/merge. See ADR 0012 |
| Control API + dashboard backend | Done — FastAPI (`src/api/`, ADR 0013). Trigger runs + HITL approve/reject over HTTP (`POST /runs/{id}/resume` → `Command(resume=...)`); one `graph.invoke()` per gate (no re-bootstrap on resume — mid-graph pause, sandbox alive). HITL review context (`GET /runs/{id}/hitl`): plan + microtasks for a `plan` gate, raw green/red diff for a `code` gate. Notify by poll (`GET /runs/{id}` → `run_state`/`hitl_phase`). Live logs: structlog → in-memory `RunLogBus` → SSE (`/logs/stream`); durable history: `events` table poll (`/events?after=<seq>`). Runs in `BackgroundTasks` (move to a worker before prod). CLI (`main.py`) being deleted — don't mirror it |
| Provider abstraction | Not started |
| State snapshots (LangGraph PostgresSaver checkpointer) | Done — see ADR 0005 (replaced `run_state` table) |
| `reflections` DB table | Not started |

## Known bugs / remaining TODOs (priority order)
See `docs/decisions/0001-architecture-audit-2026-06-07.md` for full evidence + fixes — fix 1-2 below *before* wiring real Coder/Tester/Reflector, the blast radius multiplies otherwise.
1. **Stop conditions PARTIALLY enforced** (remaining half: must-fix, safety): `CoderRunner._check_stop_limit` refuses a microtask when `attempts >= (Microtask.max_attempts or Constraints.max_microtask_attempts)` → FAILED, killing the runaway coder↔reflector loop. The planner↔reviewer loop is now also capped — `ReviewerRunner.invoke` fails the run when `Iteration.planner_reviewer_iteration_current >= Constraints.max_total_iterations` (enforced in the node, not the router; counter incremented via the node-return dict). STILL MISSING: the same cap on the coder↔tester↔reflector loop (`Iteration.coder_tester_reflector_iteration_current` is defined but never incremented) and a single whole-run `global_iterations_used` counter (currently dead — `global_iteration` + `global_iterations_used` are redundant, collapse to one).
2. ~~**`run_state` never written**~~ — **SUPERSEDED by ADR 0005**: hand-rolled `run_state` snapshots replaced by the LangGraph PostgresSaver checkpointer (snapshots every superstep, keyed by `thread_id=run_id`). `run_state` table dropped.
3. ~~**`ToolGateway` commits mid-transaction**~~ — **FIXED**: `_log_event` now delegates to `EventLogger.log()` (`db/event_log.py`), which only ever calls `session.add()`. Commit ownership correctly sits with the lending `session_scope()`.
4. ~~**`planner → END` shortcut**~~ — **FIXED**: full loops wired (`planner → plan_reviewer → (planner | coder)`, `coder → tester → reflector → coder`), plus the shared `hitl` gate (ADR 0006). Phase agents are still stubs (#8).
5. **`check_requested_path` dead code** (should-fix, security): defined in `gateway.py` but never called — `fs.read_file`/`fs.write_file` skip the sandbox-path-traversal check entirely.
6. **Provider coupling in planner**: `ChatOpenAI` instantiated directly. Should use `providers/` abstraction.
7. **`reflections` table missing**: reflections live in the checkpointed blackboard (so they survive a crash now, via ADR 0005), but there's no queryable `reflections` table for cross-run semantic memory. The `code`/`test` HITL-reject path already seeds `Reflection`s (ADR 0006) — a dedicated table makes them analyzable.
8. ~~**Global sandbox command allowlist**~~ — **FIXED**: `tool_gateway/permissions.py` gives each role its own `RolePermissions` (`tools` + `allowed_commands`); `gateway.py` calls `get_permissions(role)`. `CoderPermissions.allowed_commands` no longer includes `pytest` — Coder is now gateway-blocked from running tests, not just prompt-forbidden. Reflector is still a stub returning `{}`, but the stub Tester (`tester_node.py`) now marks microtasks COMPLETED and the coder<->tester loop cycles — see graph topology above.

## Coding conventions
- All state mutations must go through the LangGraph node return dict — never mutate `state` in place.
- Every tool invocation must emit a `tool.executed` or `tool.denied` event via `ToolGateway`.
- Never construct `Event(...)` rows directly — use `EventLogger.log(session, run_id, actor, event_type, payload)` (`db/event_log.py`). It handles id generation + timestamp and only calls `session.add()` — keeps commit-boundary ownership with the caller's `session_scope()`.
- Agents return `Dict[str, Any]` matching BlackboardState fields — only include changed keys.
- Tool functions in `sandbox_tools.py` return `ExecResult` dataclasses — never raw strings.
- New agents go in `src/agents/<Phase>/<agent_name>.py` and must be wired into `orchestrator.py`.
- New tool types must be registered in `sandbox/Tools.py` (the `Tools` enum), and in `permissions.py` — add/extend the relevant `RolePermissions` subclass's `tools` / `allowed_commands`, plus `Roles.py` for new roles.
- Sandbox is always pre-created by `bootstrapper_node` — agent nodes must never call `sandbox_create()`. Get sandbox_id from `state.get("sandbox").sandbox_id`.
- `Run.status` must be updated in DB whenever LangGraph status changes — do it in the node, not in a router.
- Use OOP for new classes.
- Operational logging: never `print()`. Use `from observability import get_logger, Category; _log = get_logger(Category.X)` (module-level) then `_log.info("dotted.event.name", key=val, ...)`. This is ephemeral debug/ops output (console or JSON), SEPARATE from the durable `events` audit trail — keep emitting `EventLogger` events too. `run_id` is auto-attached (bound once in `RunLauncher`). Interactive CLI prompts (HITL `input()` banners) stay as `print()`. Never name a package `logging` (shadows stdlib) — the package is `observability/`.

## Adding a new agent
1. Create `src/agents/<Phase>/<name>.py` with a class that has `invoke(state: BlackboardState) -> Dict[str, Any]`
2. Add a node wrapper in `orchestrator.py` following the `planner_node` pattern
3. Register the node: `builder.add_node("<name>", <name>_node)`
4. Wire edges and add routing logic if needed
5. Define which tools the agent can access in `Roles.py`
6. After `invoke()` returns: update `Run.status` in DB, emit lifecycle event, persist any new data

## Event types (must use these for DB logging)
See `docs/Blackboard.md` §6.7 for full registry. Key ones:
- `run.created`, `run.status_changed`, `run.completed`, `run.failed`
- `sandbox.created`, `sandbox.command_finished`
- `repo.cloned`, `gitops.pushed`, `gitops.pr_created`
- `plan.drafted`, `plan.reviewed`, `plan.approved`
- `microtask.started`, `microtask.completed`, `microtask.failed`
- `tool.executed`, `tool.denied`
- `test.unit_completed`, `check.lint_completed`
- `reflection.created`
- `semantic.baseline_ingested`, `semantic.workspace_synced`
