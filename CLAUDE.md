
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
      planner.py                 # ReAct investigate → structured Plan + Microtasks
      planner_tools.py           # Tool wrappers for planner
      reviewer.py                # Plan Reviewer (stub)
    Implementing/
      coder.py                   # Coder (stub)
      tester.py                  # Tester (stub)
  tool_gateway/
    gateway.py                   # ToolGateway: allowlist enforcement + audit logging
    Roles.py                     # AgentRole enum
    AllowListCommands.py         # Allowlisted shell commands per role
  sandbox/
    sandbox_tools.py             # DockerSandboxTools: create/exec/clone/destroy (DB-backed)
    Dockerfile                   # Base sandbox image (agentic-sandbox:0.1)
  db/
    db.py                        # SQLAlchemy engine + session_scope
    models.py                    # ORM models: Run, RunState, Event, Artifact, Sandbox, MicrotaskRecord
    migrations/                  # Alembic migrations
  providers/                     # Pluggable LLM provider interface (TODO: implement)
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
```

## Graph topology (current)
```
START → bootstrapper → phase_router → planner → END
                                    ↘ coder → tester → (reflector | END)
                                    ↘ tester

plan_reviewer node exists but planner → END (dev shortcut, not yet wired to reviewer)
```

## Run the pipeline
```bash
# 1. Start Postgres
docker compose -f deploy/docker-compose.yml up -d

# 2. Build sandbox image (only when Dockerfile changes)
docker compose -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder

# 3. Run migrations
cd /home/kristian/project/AgenticDevOps && venv/bin/alembic upgrade head

# 4. Run
cd src
../venv/bin/python main.py \
  --repo_url https://github.com/owner/repo \
  --base-sha <SHA> \          # omit for brand-new repos with no commits
  --task_description "Your task here"
```

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
| `DATABASE_URL` | Postgres connection string |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Used by DB scripts |
| `CLUSTER_API_KEY` | API key for LLM cluster endpoint |
| `USE_TOOLS` | `true` to enable planner's ReAct investigation tools |
| `PLANNER_MAX_TOOL_STEPS` | Max tool call rounds for planner investigation |
| `TEAR_DOWN_SANDBOX_AFTER_RUN` | Destroy container after run (set `false` during dev) |
| `GITHUB_TOKEN` | Required for private repos — injected into clone URL, never logged |

## LLM provider
Currently using `ChatOpenAI` with a custom cluster endpoint (`http://131.159.60.153:3000/inference`) running `google/gemma-4-31B-it`. All agents should eventually use `providers/base.py` abstraction (not yet implemented).

## DB schema (all tables)
| Table | Purpose |
|---|---|
| `runs` | One row per run — repo_url, base_sha (nullable), status, timestamps |
| `sandboxes` | Docker container ID per run — survives restarts |
| `microtasks` | Planner output persisted after each plan — survives crashes |
| `artifacts` | Plan summaries, test logs, diffs — kind field identifies type |
| `events` | Full audit trail — run.created, repo.cloned, plan.drafted, tool.executed, etc. |
| `run_state` | Blackboard JSON snapshots (table exists, not yet written) |

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
| Plan Reviewer | Stub |
| Coder | Stub |
| Tester | Stub |
| Reflector | Not started |
| Context Engine (pgvector) | Not started |
| GitOps (webhooks + PR) | Not started |
| Provider abstraction | Not started |
| `run_state` snapshots (table exists, writes missing) | Not started |
| `reflections` DB table | Not started |

## Known bugs / remaining TODOs (priority order)
1. **`planner → END` shortcut**: real wire is `planner → plan_reviewer → coder → tester → (reflect | next_microtask)`.
2. **Provider coupling in planner**: `ChatOpenAI` instantiated directly. Should use `providers/` abstraction.
3. **`plan_verification_router`**: uses `.get()` on review_feedback items — fails if they're Pydantic models. Use attribute access or keep as dicts.
4. **`run_state` never written**: table exists but blackboard snapshot never persisted after each node.
5. **`reflections` table missing**: reflections only in LangGraph state (memory), lost on crash.
6. **Coder / Tester / Reflector**: all stubs returning `{}`.

## Coding conventions
- All state mutations must go through the LangGraph node return dict — never mutate `state` in place.
- Every tool invocation must emit a `tool.executed` or `tool.denied` event via `ToolGateway`.
- Agents return `Dict[str, Any]` matching BlackboardState fields — only include changed keys.
- Tool functions in `sandbox_tools.py` return `ExecResult` dataclasses — never raw strings.
- New agents go in `src/agents/<Phase>/<agent_name>.py` and must be wired into `orchestrator.py`.
- New tool types must be registered in `tool_gateway/AllowListCommands.py` and `Roles.py`.
- Sandbox is always pre-created by `bootstrapper_node` — agent nodes must never call `sandbox_create()`. Get sandbox_id from `state.get("sandbox").sandbox_id`.
- `Run.status` must be updated in DB whenever LangGraph status changes — do it in the node, not in a router.

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
- `repo.cloned`, `gitops.pr_created`
- `plan.drafted`, `plan.reviewed`, `plan.approved`
- `microtask.started`, `microtask.completed`, `microtask.failed`
- `tool.executed`, `tool.denied`
- `test.unit_completed`, `check.lint_completed`
- `reflection.created`
