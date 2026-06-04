## Goal

Support POC-1 by keeping **one Docker sandbox per `run_id`** for the whole planner invocation. The Planner ReAct agent must read the same filesystem it explored.

## Problem

Today `planner_node` destroys the sandbox in a `finally` block after planning (`src/orchestrator/orchestrator.py`). That breaks any multi-step POC and contradicts "sandbox is source of truth."

## Tasks

- [ ] Create sandbox in a dedicated step (or orchestrator init), not inside `planner_node` only
- [ ] Store `sandbox_id`, `workspace_path`, `status` on blackboard (`BlackboardState.sandbox`)
- [ ] Pass shared `ToolGateway` + `DockerSandboxTools` into `Planner` / `PlannerTools`
- [ ] Destroy sandbox only on run completion or explicit `FAILED` (not after planner returns)
- [ ] Emit `sandbox.created` / `sandbox.destroyed` events via Tool Gateway

## Acceptance criteria

- Running the planner twice in the same run uses the **same** container
- `docker ps` shows one container per `run_id` during the run
- No `sandbox_destroy` in `planner_node` `finally`

## References

- `docs/instructions.md` § Step 3
- `src/sandbox/sandbox_tools.py`
- `.cursor/skills/agentic-planner/SKILL.md`

## Labels

`poc`, `sandbox`, `orchestrator`
