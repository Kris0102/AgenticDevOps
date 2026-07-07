# 0025 — Per-call token cap, token usage tracking, and a global per-run token budget

Status: accepted
Date: 2026-07-07

## Context

The user needs to bound LLM cost/runaway generation in two independent ways: (1) cap how many
tokens a single LLM call may generate, and (2) cap the total tokens a whole run may consume
across every agent, with the run failing cleanly once the budget is spent. Alongside both caps,
the user wants visibility into actual token consumption — for now surfaced in the existing
per-microtask debug output files and logs, not yet in the dashboard.

Before this change there was **no token accounting anywhere** in the codebase (zero uses of
LangChain's `usage_metadata`), and no per-call generation cap was passed to either provider
factory branch. Two adjacent, pre-existing traces informed the design:
- `Constraints.cost_budget_usd` / `time_budget_minutes` (`orchestrator/state.py`) have existed
  since the CLI-only days but are never read anywhere — dead input fields, dollar/time based,
  not token based.
- ADR 0020 explicitly dropped `max_completion_tokens` from the provider factory ("incidental
  gemma tuning; irrelevant at temperature 0") with the note "re-add as a knob if a model needs
  them" — a direct precedent for Part A below.
- The existing `max_total_iterations` cap (`reviewer_node.py`) is the established pattern for
  "a run-scoped counter enforced in the node, failing the run cleanly on exhaustion" — the
  template for Part C.

## Decision

Three independent, additive pieces, all backend-only (no dashboard changes — see Consequences):

**A — Per-call output-token cap.** `LLMConfig.max_output_tokens: Optional[int] = None`
(`gt=0` when set) flows through the single provider factory, `providers/llm.py::build_chat_llm`,
as `max_tokens=llm.max_output_tokens` to both `ChatAnthropic` and `ChatOpenAI`. `None` behaves
identically to omitting the parameter (verified: Anthropic's `set_default_max_tokens` validator
only fires on `None`, applying its own model-profile default; OpenAI simply omits the field).
Exposed via `selected.max_output_tokens` in the settings schema (`nullable: true`) and an
`LLM_MAX_OUTPUT_TOKENS` env seed, following the existing `selected.max_retries` pattern exactly.

**B — Token usage capture.** A shared `providers/token_usage.py::TokenUsage` /
`sum_usage(messages)` sums `AIMessage.usage_metadata` across every message in a ReAct
trajectory or a single structured call. Every agent (Planner, Reviewer, Coder, Reflector,
Tester) now tracks `self.token_usage` (or `TestContext.token_usage`) across all of its LLM
calls per invocation. The three agents that use `.with_structured_output(...)` (Planner
synthesis, Reviewer review, Reflector assess) were switched to `include_raw=True` so the
underlying `AIMessage` — and its `usage_metadata` — stays reachable; the parsing-error branch
is explicitly re-raised to preserve prior error-handling behavior. Usage is:
1. written into each agent's existing per-iteration debug `.txt` file (already-established
   writers: `plan_output.py`, `review_output.py`, `coder_output.py`, `reflector_output.py`,
   `tester_output.py`);
2. logged via structlog (`llm.usage`, ephemeral, visible in the live SSE log feed);
3. emitted as a new durable `EventType.LLM_USAGE_RECORDED` ("llm.usage") event per node
   invocation;
4. accumulated into a new `Iteration.tokens_used_total: int = 0` field (same `model_copy`
   running-total pattern as `global_iterations_used`) — the read side Part C's enforcement
   needs;
5. mirrored onto a new `runs.tokens_used_total` DB column (same precedent as the existing
   `Run.llm_provider`/`Run.llm_model` denormalized mirrors) so the total is queryable without
   deserializing the LangGraph checkpoint.

All three logging/event/accumulation/mirror steps for a given invocation are done by one
shared function, `orchestrator/token_budget.py::record_usage(db_session, run_id, actor,
iteration, usage, model) -> Iteration`, called once per node right after that node's agent
returns — avoiding five copies of the same bookkeeping.

**C — Global per-run token budget, enforced.** `Constraints.max_total_tokens: Optional[int] =
None` (own field — NOT a reuse of `cost_budget_usd`, which is dollar-denominated and a separate
concern) plus a matching `RunDefaults.max_total_tokens` global default. Enforcement:
`orchestrator/token_budget.py::token_budget_exceeded(iteration, constraints) -> bool`, checked
at the very start of **every** agent node's `invoke()` (Planner, Reviewer, Coder, Tester,
Reflector) — before that node spends another LLM call, mirroring the existing
`max_total_iterations` guard in `reviewer_node.py`. On exceeding the budget the node calls the
existing `RunFailureHandler.handle(...)` with a `RunExecutionError` — the run goes cleanly
**FAILED** (status update, `LessonStore.retract_run`, `RUN_STATUS_CHANGED` + `RUN_FAILED`
events), exactly like every other deterministic stop-condition in this codebase. No partial/
soft degrade: once the accumulated total reaches the cap, the very next node to run refuses to
make its LLM call at all.

## Alternatives considered

- **Reuse `Constraints.cost_budget_usd` for the token budget.** Rejected — it's
  dollar-denominated (would require a cost model per provider/model to convert), already dead
  code with unclear semantics, and conflating two different budget types would make future
  cost-budget enforcement (if ever built) harder to reason about.
- **Enforce the budget only in the Reviewer** (mirroring the existing `max_total_iterations`
  guard, which lives in `reviewer_node.py` only). Rejected — the planner-reviewer loop is only
  one of the five places tokens are spent; a Coder/Tester/Reflector-heavy run could blow the
  budget without ever revisiting the Reviewer. The guard is duplicated (via a shared predicate)
  into all five nodes instead.
- **Ship a dashboard tile / any dashboard-facing changes as part of this cut.** Rejected per
  explicit user direction — this is backend-only; the dashboard is a separate, later PR. The
  DB column + durable events already lay the groundwork so that PR needs no new persistence.
- **A dedicated `token_usage` table with one row per LLM call.** Rejected as premature — the
  existing `llm.usage` events already give per-call granularity for future drill-down; a
  running total column on `runs` covers the "current spend" need cheaply. A dedicated table can
  be added later if per-call historical analysis across runs is actually needed.
- **Capture usage without switching to `include_raw=True`** (e.g. regex-scraping token counts
  from raw text, or skipping usage capture for the three structured-output calls). Rejected —
  `include_raw=True` is the standard, supported LangChain mechanism for this and required no
  behavior change beyond re-raising `parsing_error` explicitly.

## Consequences

Cost/runaway control now exists at both granularities the user asked for, using patterns that
already exist elsewhere in the codebase (settings hub knob, `Iteration` running counter,
`RunFailureHandler`, denormalized `runs` columns) — no new architectural concepts. The
`include_raw=True` switch touches three existing call sites (Planner synthesis, Reviewer
review, Reflector assess) and is a small, contained risk: a parsing failure now flows through
`raw["parsing_error"]` instead of a raised exception from `with_structured_output` itself,
re-raised explicitly to keep the existing `except ValidationError` / `except Exception` paths
behaving the same. The dashboard cannot yet show any of this — that's an explicitly deferred,
separate piece of work; until it ships, the only way to see token usage is the debug output
files, the live log feed, the `events` table, or a direct `SELECT tokens_used_total FROM runs`.
