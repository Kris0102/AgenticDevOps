"""Global token-budget accounting + enforcement (ADR 0025), shared by all 5 agent nodes.

`token_budget_exceeded` mirrors the `max_planner_reviewer_iterations` guard in `reviewer_node.py`: each
Runner checks this at the START of `invoke()`, before spending another LLM call, so a run
stops cleanly the moment the accumulated `Iteration.tokens_used_total` reaches
`Constraints.max_total_tokens` — never mid-call. Unlike that iteration cap, exceeding the
token budget does NOT fail the run: each Runner raises `TokenBudgetExceededError`
(`Exceptions/TokenBudgetExceededError.py`), which escapes `graph.invoke()` and is caught by
`Service._drive`, leaving the run non-terminal (STALLED, checkpoint intact). The human can
raise the budget via `POST /runs/{id}/continue`'s `max_total_tokens` body (ADR
0025, `Service.raise_token_budget`) before it re-runs the same node, or `POST
/runs/{id}/fail` to close the case.

`record_usage` is the single place that turns a per-invocation `TokenUsage` into: the updated
`Iteration` (running total), a denormalized mirror on the `runs.tokens_used_total` DB column
(same precedent as `Run.llm_provider`/`Run.llm_model` — cheap to query without deserializing
the LangGraph checkpoint blob), an ephemeral `llm.usage` log line, and a durable
`EventType.LLM_USAGE_RECORDED` event — called once per node, right after that node's agent
returns.
"""
from __future__ import annotations

from typing import Optional

from db.event_log import EventLogger
from db.models import Run
from observability import Category, get_logger
from orchestrator.state import Constraints, EventType, Iteration
from providers import TokenUsage
from providers.pricing import cost_for

_log = get_logger(Category.ORCHESTRATOR)


def token_budget_exceeded(iteration: Iteration, constraints: Constraints) -> bool:
    return (
        constraints.max_total_tokens is not None
        and iteration.tokens_used_total >= constraints.max_total_tokens
    )


def record_usage(
    db_session,
    run_id: str,
    actor: str,
    iteration: Iteration,
    usage: TokenUsage,
    model: str,
    microtask_id: str | None = None,
) -> Iteration:
    """Accumulate `usage` onto `iteration.tokens_used_total`, mirror the new total onto the
    `runs` row, log it, and emit the durable event. Returns the updated `Iteration` —
    callers put it on their result dict.

    `microtask_id` is set by Coder/Tester/Reflector (all three run scoped to one microtask)
    so the dashboard can attribute per-microtask token spend; the Planner drafts the whole plan
    before any microtask exists, so it's omitted there (None)."""
    updated = iteration.model_copy(update={
        "tokens_used_total": iteration.tokens_used_total + usage.total_tokens,
    })
    run_record = db_session.get(Run, run_id)
    if run_record:
        run_record.tokens_used_total = updated.tokens_used_total
    _log.info(
        "llm.usage", agent=actor, model=model,
        input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
        total_tokens=usage.total_tokens, run_total=updated.tokens_used_total,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        microtask_id=microtask_id,
    )
    # Price this usage HERE via cost_for (providers/pricing.py — the single source of truth
    # for the cost formula) and store the dollar figure on the event. That keeps the durable
    # event self-contained: the API per-run cost view and the Grafana cross-run cost panels
    # both read this ONE number, computed once, instead of re-deriving prices (the dashboard
    # can't call Python). None for an unpriced cluster model — it contributes tokens, no cost.
    cost = cost_for(
        model, usage.input_tokens, usage.output_tokens,
        usage.cache_read_tokens, usage.cache_creation_tokens,
    )
    EventLogger.log(db_session, run_id, actor, EventType.LLM_USAGE_RECORDED, {
        "agent": actor, "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        # Prompt-caching breakdown (GH #62) — subsets of input_tokens (informational,
        # never added onto totals). 0 for providers that don't report caching.
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cost_usd": round(cost.total, 6) if cost is not None else None,
        "microtask_id": microtask_id,
    })
    return updated


def record_usage_safe(
    db_session,
    run_id: str,
    actor: str,
    iteration: Iteration,
    usage: Optional[TokenUsage],
    model: str,
    microtask_id: str | None = None,
) -> Iteration:
    """`record_usage`, guarded to be a NO-OP when nothing was spent.

    Call this on an agent node's CRASH path (its `except` block, before re-raising or handing to
    RunFailureHandler): the LLM calls the agent already made hit the provider and were BILLED, so
    losing them just because the node then failed understates the run cost, the token budget, and
    the dashboard (which showed 0 while the Claude console billed). Every phase uses this so
    usage is tracked whether or not the agent crashed — not only on the happy path.

    On the success path use `record_usage` directly (you need its returned Iteration on the result
    dict). Empty/None usage (the agent never reached an LLM call) → returns `iteration` unchanged,
    with no event and no DB write."""
    if usage is None or usage.total_tokens == 0:
        return iteration
    return record_usage(db_session, run_id, actor, iteration, usage, model, microtask_id)
