# 0025 — Token usage tracking and a global per-run token budget

Status: accepted
Date: 2026-07-07

## Context

The user needs to bound LLM cost/runaway generation across a whole run: cap the total tokens
a run may consume across every agent. Alongside the cap, the user wants visibility into
actual token consumption — for now surfaced in the existing per-microtask debug output files
and logs (dashboard surfacing is ADR 0026).

Before this change there was **no token accounting anywhere** in the codebase (zero uses of
LangChain's `usage_metadata`). Two adjacent, pre-existing traces informed the design:
- `Constraints.cost_budget_usd` / `time_budget_minutes` (`orchestrator/state.py`) have existed
  since the CLI-only days but are never read anywhere — dead input fields, dollar/time based,
  not token based.
- The existing `max_total_iterations` cap (`reviewer_node.py`) is the established pattern for
  "a run-scoped counter enforced in the node, checked at the top of `invoke()`" — the
  template for the enforcement piece below.

A budget cap also needed to decide what happens when it's exceeded. The obvious first
answer — fail the run outright, exactly like `max_total_iterations` and the microtask
attempt caps — doesn't fit a token budget the way it fits those other caps. A blown
iteration cap or an exhausted microtask-attempt budget usually means the run is genuinely
stuck (looping, not converging) — FAILED is the right terminal verdict there. A token budget
is different: it's a cost-control policy knob, not evidence the run is broken. The run may be
one microtask away from DONE when it trips the cap, and hard-failing would force the human to
throw away a checkpoint that's otherwise perfectly fine and start a brand-new run from
scratch just because the number was set too low. The codebase already has a mechanism for
exactly this shape of problem: a **STALLED** run — non-terminal status, checkpoint intact,
sandbox alive — recoverable via `POST /runs/{id}/continue` (re-drives the same node from the
checkpoint) or closed via `POST /runs/{id}/fail` (`Service.give_up`). It's used today for
transient LLM errors (`RetriableError`, ADR 0023) and for uncaught crashes/worker deaths
(`Service._drive`'s generic exception guard); both dashboard actions ("Continue run" / "Mark
as failed") already exist and are wired to it.

## Decision

Three additive pieces:

**A. Token usage capture.** A shared `providers/token_usage.py::TokenUsage` /
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
   running-total pattern as `global_iterations_used`) — the read side the budget enforcement
   below needs;
5. mirrored onto a new `runs.tokens_used_total` DB column (same precedent as the existing
   `Run.llm_provider`/`Run.llm_model` denormalized mirrors) so the total is queryable without
   deserializing the LangGraph checkpoint.

All three logging/event/accumulation/mirror steps for a given invocation are done by one
shared function, `orchestrator/token_budget.py::record_usage(db_session, run_id, actor,
iteration, usage, model) -> Iteration`, called once per node right after that node's agent
returns — avoiding five copies of the same bookkeeping.

**B. Global per-run token budget, enforced.** `Constraints.max_total_tokens: Optional[int] =
None` (own field — NOT a reuse of `cost_budget_usd`, which is dollar-denominated and a
separate concern) plus a matching `RunDefaults.max_total_tokens` global default. Enforcement:
`orchestrator/token_budget.py::token_budget_exceeded(iteration, constraints) -> bool`, checked
at the very start of **every** agent node's `invoke()` (Planner, Reviewer, Coder, Tester,
Reflector) — before that node spends another LLM call, mirroring the existing
`max_total_iterations` guard in `reviewer_node.py`. No partial/soft degrade: once the
accumulated total reaches the cap, the very next node to run refuses to make its LLM call at
all.

**C. Exceeding the budget stalls the run, not fails it.** Per the Context above, this is
deliberately NOT routed through `RunFailureHandler`. A new `Exceptions/TokenBudgetExceededError.py`
— deliberately **not** a `RunExecutionError`, so it is NOT caught by each node's own
`except RunExecutionError → RunFailureHandler` handler — is raised by each of the five
Runners instead. It escapes `graph.invoke()` uncaught (the same path `RetriableError`
already takes) up to `Service._drive`, which catches `TokenBudgetExceededError` explicitly
(checked before the generic `RetriableError`/`Exception` branches) and calls
`_mark_stalled(run_id, exc.reason, kind="token_budget")` — `Run.status` is left untouched
(still whatever in-flight status the checkpoint had), so `_derive_state`/`_list_state` report
**STALLED**. `_mark_stalled` takes a `kind` parameter (default `"transient"`, now also
`"token_budget"`) purely for the `RUN_STALLED` event payload, letting the dashboard
eventually distinguish "worker died" from "hit the token cap" (consumed starting ADR 0026).
Recovery is exactly the existing STALLED flow: `POST /runs/{id}/continue` re-invokes the same
node from the checkpoint (a no-op re-stall if the cap genuinely wasn't raised first — see
ADR 0026 for how the human actually raises it from the dashboard); `POST /runs/{id}/fail`
closes the case exactly like any other stalled run.

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
  explicit user direction — this is backend-only; the dashboard is a separate, later piece
  (ADR 0026). The DB column + durable events already lay the groundwork so that piece needs
  no new persistence.
- **A dedicated `token_usage` table with one row per LLM call.** Rejected as premature — the
  existing `llm.usage` events already give per-call granularity for future drill-down; a
  running total column on `runs` covers the "current spend" need cheaply. A dedicated table can
  be added later if per-call historical analysis across runs is actually needed.
- **Capture usage without switching to `include_raw=True`** (e.g. regex-scraping token counts
  from raw text, or skipping usage capture for the three structured-output calls). Rejected —
  `include_raw=True` is the standard, supported LangChain mechanism for this and required no
  behavior change beyond re-raising `parsing_error` explicitly.
- **Fail the run outright (terminal FAILED) on budget exhaustion**, exactly like
  `max_total_iterations` and the microtask attempt caps. Rejected — see Context: a token
  budget is a cost-control policy knob, not evidence the run is broken, and FAILING forces
  discarding an otherwise-fine checkpoint.
- **Let the human retry via a brand-new run** instead of stalling the existing one. Rejected
  — throws away a possibly near-complete checkpoint (microtasks already COMPLETED, sandbox
  state) for a problem that's purely "the cap was too conservative," not a defect in the run.
- **A dedicated `RunStatus` enum value for "token-capped".** Rejected — `STALLED` is a
  derived/coarse dashboard state (`RunState`), not a `RunStatus` on the blackboard; the run's
  actual `status` field keeps whatever in-flight value it had (e.g. `NEEDS_CODE`), which is
  exactly what makes `/continue` "just re-run the node" work. Introducing a new terminal-ish
  status would duplicate machinery `_derive_state` already provides for free.

## Consequences

Run-wide cost/runaway control now exists, using patterns that already exist elsewhere in the
codebase (settings hub knob, `Iteration` running counter, denormalized `runs` columns,
the STALLED/`_drive` machinery already used for transient errors) — no new architectural
concepts. The `include_raw=True` switch touches three existing call sites (Planner synthesis,
Reviewer review, Reflector assess) and is a small, contained risk: a parsing failure now flows
through `raw["parsing_error"]` instead of a raised exception from `with_structured_output`
itself, re-raised explicitly to keep the existing `except ValidationError` / `except
Exception` paths behaving the same.

A token-budget stop is recoverable, not a dead end — consistent with the project's general
stance that only genuine domain failures (runaway loops, exhausted attempt budgets) should be
terminal, while operational/policy stops (transient errors, cost caps) should leave the
checkpoint intact. `RunFailureHandler`/`RunExecutionError` are never invoked for this path in
any of the five nodes (their imports remain in all five node files, still used by their own
`except RunExecutionError` handlers for unrelated domain failures). The `/continue` no-op
when the cap still isn't raised is intentional and safe, but not maximally useful on its own
— ADR 0026 closes that gap by letting the human actually raise the cap from the dashboard
before continuing. Until ADR 0026 ships, the only way to see token usage or raise the budget
is the debug output files, the live log feed, the `events` table, or a direct `SELECT
tokens_used_total, max_total_tokens FROM runs`.
