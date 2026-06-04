## Goal

Before the Planner ReAct loop runs, the sandbox workspace must contain the **real repo** at `base_sha`.

## Problem

`DockerSandboxTools.git_clone_and_checkout` exists but the orchestrator never calls it before planning. The Planner may explore an empty `/workspace`.

## Tasks

- [ ] Add `bootstrap_node` (or equivalent) to LangGraph
- [ ] Clone `repo.repo_url` and checkout `repo.base_sha` inside container (use existing `git_clone_and_checkout`)
- [ ] Transition `NEEDS_BOOTSTRAP` → `NEEDS_PLAN`
- [ ] Log `repo.cloned` event with `repo_url`, `base_sha`, `sandbox_id`
- [ ] Handle clone failure → `RunStatus.FAILED` with reason

## Acceptance criteria

- After bootstrap, `ls /workspace` (or repo dir) shows project files from the target repo
- Planner investigation summary mentions **real** paths (e.g. `src/`, `pyproject.toml`)

## Depends on

Run-scoped sandbox lifecycle (#1 in POC-1 issue set)

## References

- `src/sandbox/sandbox_tools.py` — `git_clone_and_checkout`
- `docs/instructions.md` — `bootstrap_node`

## Labels

`poc`, `sandbox`, `orchestrator`
