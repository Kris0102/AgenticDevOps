# 0007 — Investigation phase stays free-text; synthesis is the sole structured contract

Status: accepted
Date: 2026-06-11

## Context

`Planner.invoke()` runs in two phases: a ReAct investigator agent (`create_agent`,
free exploration via tools) followed by a structured synthesis call
(`structured_llm.invoke(...)` → `PlannerOutput`, enforced via
`with_structured_output(..., method="json_mode")`).

`create_agent` supports a `response_format=<PydanticModel>` parameter, which forces
the agent's final output into `state["structured_response"]`, validated against that
model. The question came up: should the investigator be given a `response_format`
(e.g. `InvestigationResult` with fields like `key_files`, `dependencies`) instead of
returning free-text via `_last_ai_text`?

Separately, this session added `_log_investigation_steps` (in
`investigation_support.py`) to print each ReAct step (model thoughts, tool calls,
tool results) for debuggability — unrelated to the structured-output question but
informed the discussion (the raw step log is already inspectable without a schema).

## Decision

Investigation output stays free-text (`investigation_summary: str`, via
`_last_ai_text`). Do **not** add `response_format` to `investigator_agent`.

Synthesis (`PlannerOutput`/`Plan`/`Microtask` via structured output) remains the only
point where a schema is enforced, with `_normalize_output` / `BadMicroTaskException`
as its quality gate.

## Alternatives considered

- **`response_format=InvestigationResult` on investigator**: rejected.
  - Investigation is exploratory — forcing a schema (`key_files: list[str]`,
    `summary: str`, etc.) at this stage either duplicates fields synthesis already
    derives from `investigation_summary` + raw repo access, or constrains the model
    to a shape that doesn't fit what it actually found.
  - `response_format` triggers an additional LLM call at the end of the ReAct loop —
    extra latency and a second `ValidationError`/`APITimeoutError` surface needing its
    own retry handling, mirroring the synthesis retry loop for no clear benefit.
  - Two structured-output enforcement points (investigation + synthesis) = two
    failure surfaces or quality gates to maintain in lockstep.

## Consequences

- Investigation remains a "research notes" free-text blob; synthesis is the single
  place a contract is validated and retried.
- `_log_investigation_steps` gives full step-by-step visibility into the ReAct loop
  (thoughts/tool calls/tool results) without needing a schema — useful for judging
  whether the investigator is grounded in real repo content vs. hallucinating.
- If a future need arises for machine-readable investigation output (e.g. feeding
  `context_files` candidates programmatically), revisit this — but the bar is "what
  does synthesis still need that free text + repo access can't provide."
