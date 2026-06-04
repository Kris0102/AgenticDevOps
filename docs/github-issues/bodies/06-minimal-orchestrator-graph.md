## Goal

Wire a **minimal LangGraph** for POC-1 only: `bootstrap` → `planner` → `END`. No Coder/Tester yet.

## Problem

Current graph: `planner → END`, plan reviewer disconnected, bootstrap missing, sandbox torn down in planner.

## Tasks

- [ ] Add `bootstrap_node` → sets `NEEDS_PLAN`
- [ ] `planner_node` uses shared sandbox (no destroy in `finally`)
- [ ] `START` routes `NEEDS_BOOTSTRAP` → bootstrap, `NEEDS_PLAN` → planner
- [ ] Remove or comment full execution loop until POC-2
- [ ] Fix `initial_router` — do not create duplicate `Run` rows on every entry (idempotent upsert)

## Acceptance criteria

```bash
python -m main --run_id test-001 --repo_url <url> --base-sha <sha> --task_description "..."
```

ends with blackboard containing `plan`, `microtasks`, `status=NEEDS_REVIEW`

## Depends on

Issues #1–#5 in POC-1 set

## Labels

`poc`, `orchestrator`, `langgraph`
