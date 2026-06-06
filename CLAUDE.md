
///# AgenticDevOps — Project Guide for Claude

## What this is
Multi-agent system that autonomously plans, implements, tests, and proposes code changes to an existing repo. Triggered by CLI (MVP) or GitHub webhook (future). Uses Docker-per-run sandboxing and Postgres for full audit trail.

## Key architecture decisions
- **Blackboard (run_state)**: single JSON state owned exclusively by Orchestrator. Agents never write state directly.
- **Grounded verification**: code is only accepted when deterministic checks (lint/tests/build) pass. No LLM-only review.
- **Tool Gateway**: all environment actions (file reads, shell commands, git) go through `ToolGateway` — deny-by-default, every call logged.
- **Star topology**: all agent-to-agent communication is mediated by Orchestrator via blackboard, not direct calls.
- **Docker-per-run**: each run gets its own container. Sandbox is the source of truth for code during a run.

## Project layout
```
src/
  main.py                        # CLI entrypoint
  orchestrator/
    orchestrator.py              # LangGraph graph + routing
    state.py                     # Pydantic BlackboardState + all models
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
    sandbox_tools.py             # DockerSandboxTools: create/exec/clone/destroy
    Dockerfile                   # Base sandbox image (agentic-sandbox:0.1)
  db/
    db.py                        # SQLAlchemy engine + session_scope
    models.py                    # ORM models: Run, RunState, Event, Artifact
    migrations/                  # Alembic migrations
  providers/                     # Pluggable LLM provider interface (TODO: implement)
deploy/
  docker-compose.yml             # Postgres + pgAdmin + sandbox image builder
docs/
  ARCHITECTURE_preview.md        # Full system spec (state machine, data flow)
  Design_preview.md              # Agent patterns and cognitive architecture
  MCP_AND_TOOLS.md               # Tool Gateway + MCP contracts
  Blackboard.md                  # Blackboard schema + event type registry
  POC_instructions.md            # Step-by-step PoC build guide
  instructions.md                # MVP implementation plan
```

## Run the pipeline
```bash
# 1. Start Postgres
docker compose -f deploy/docker-compose.yml up -d

# 2. Build sandbox image (only when Dockerfile changes)
docker compose -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder

# 3. Run migrations
cd src && alembic upgrade head  # (from project root, alembic.ini is there)

# 4. Run
cd src
python main.py \
  --repo_url https://github.com/owner/repo \
  --base-sha <SHA> \
  --task_description "Your task here"
```

## Environment variables (`.env` in project root)
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `CLUSTER_API_KEY` | API key for LLM cluster endpoint |
| `USE_TOOLS` | `true` to enable planner's ReAct investigation tools |
| `PLANNER_MAX_TOOL_STEPS` | Max tool call rounds for planner investigation |
| `TEAR_DOWN_SANDBOX_AFTER_RUN` | Destroy container after run (set `false` during dev) |

## LLM provider
Currently using `ChatOpenAI` with a custom cluster endpoint (`http://131.159.60.153:3000/inference`) running `google/gemma-4-31B-it`. All agents should eventually use `providers/base.py` abstraction (not yet implemented).

## Current implementation status
| Component | Status |
|---|---|
| Blackboard / state models | Done |
| DB schema + Alembic | Done |
| DockerSandboxTools | Done |
| ToolGateway | Done |
| Planner (ReAct + structured output) | Done |
| Plan Reviewer | Stub |
| Coder | Stub |
| Tester | Stub |
| Reflector | Not started |
| Context Engine (pgvector) | Not started |
| GitOps (webhooks + PR) | Not started |
| Provider abstraction | Not started |

## Known architecture bugs to fix (priority order)
1. **No `bootstrap_node`**: `initial_router` creates DB entry — side effect in router is wrong. Need dedicated bootstrap node that creates run, sandbox, clones repo.
2. **Sandbox not shared across nodes**: Sandbox is created in `planner_node` and may be destroyed before `coder_node` runs. `sandbox_id` must be persisted in BlackboardState and the container reused.
3. **Graph wired to END after planner**: `planner → END` is a dev shortcut. Real wire: `planner → plan_reviewer → coder → tester → (reflect | next_microtask)`.
4. **Provider coupling in planner**: `ChatOpenAI` instantiated directly. Should use `providers/` abstraction.
5. **`plan_verification_router`**: Uses `.get()` on review_feedback items — fails if they're Pydantic models. Use attribute access.

## Coding conventions
- All state mutations must go through the LangGraph node return dict — never mutate `state` in place.
- Every tool invocation must emit a `tool.executed` or `tool.denied` event via `ToolGateway`.
- Agents return `Dict[str, Any]` matching BlackboardState fields — only include changed keys.
- Tool functions in `sandbox_tools.py` return `ExecResult` dataclasses — never raw strings.
- New agents go in `src/agents/<Phase>/<agent_name>.py` and must be wired into `orchestrator.py`.
- New tool types must be registered in `tool_gateway/AllowListCommands.py` and `Roles.py`.

## Adding a new agent
1. Create `src/agents/<Phase>/<name>.py` with a class that has `invoke(state: BlackboardState) -> Dict[str, Any]`
2. Add a node wrapper in `orchestrator.py` following the `planner_node` pattern
3. Register the node: `builder.add_node("<name>", <name>_node)`
4. Wire edges and add routing logic if needed
5. Define which tools the agent can access in `Roles.py`

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
