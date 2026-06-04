## Goal

After ReAct investigation, Planner emits **validated** `Plan` + `List[Microtask]` (Pydantic / `PlannerOutput`).

## Tasks

- [ ] Keep two-phase flow: investigate → synthesize (already sketched in `planner.py`)
- [ ] Synthesis prompt requires JSON matching `PlannerOutput` schema
- [ ] Validate with `PlannerOutput.model_validate` before returning to orchestrator
- [ ] Every microtask must have ≥1 **testable** `acceptance_criteria`
- [ ] Set `status=NEEDS_REVIEW` (Plan Reviewer comes in POC-2; for POC-1 OK to stop here)
- [ ] Assign `microtask_id`, `idx`, default `status=PENDING`

## Acceptance criteria

- Invalid LLM JSON → clear error, no partial blackboard merge
- Output includes `plan.summary` and 2+ microtasks for a non-trivial task
- Printed/logged microtasks show acceptance criteria

## Depends on

ReAct investigation loop

## References

- `src/orchestrator/state.py` — `Plan`, `Microtask`
- `docs/Blackboard.md` §F

## Labels

`poc`, `planner`
