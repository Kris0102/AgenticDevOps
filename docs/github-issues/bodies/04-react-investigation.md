## Goal

**POC centerpiece:** Planner runs a **ReAct agent** (reason + act) that uses tools to explore the cloned repo and produces a **text investigation summary** for the synthesis step.

## Current state

`Planner` already has `create_agent` + `investigator_agent` when tools are provided (`src/agents/Planning/planner.py`). Needs hardening for POC.

## Tasks

- [ ] Cap ReAct iterations (e.g. `max_iterations=15` or env `PLANNER_MAX_TOOL_STEPS`)
- [ ] Update system prompt: use structured tools only; workspace root; no pip/apt/network installs
- [ ] Pass objective from blackboard (`objective.title`, `description`, `definition_of_done`)
- [ ] On tool failure: surface gateway error to model (don't swallow)
- [ ] On connection errors: clear message (VPN/cluster endpoint)
- [ ] Log investigation transcript summary to stdout or artifact (debug POC)

## Acceptance criteria

- Given a small public fixture repo + task, investigator runs ≥2 tool calls (e.g. list dir + read file)
- Investigation summary references **concrete file paths** found in sandbox
- Run completes without unbounded tool loop

## Depends on

Bootstrap clone + structured planner tools

## References

- `docs/Design_preview.md` — ReAct within microtask/planning
- `.cursor/skills/agentic-planner/SKILL.md`

## Labels

`poc`, `planner`, `react`
