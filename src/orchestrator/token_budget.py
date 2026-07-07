"""Global token-budget accounting + enforcement (ADR 0025), shared by all 5 agent nodes.

`token_budget_exceeded` mirrors the `max_total_iterations` guard in `reviewer_node.py`: each
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

from db.event_log import EventLogger
from db.models import Run
from observability import Category, get_logger
from orchestrator.state import Constraints, EventType, Iteration
from providers import TokenUsage

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
        microtask_id=microtask_id,
    )
    EventLogger.log(db_session, run_id, actor, EventType.LLM_USAGE_RECORDED, {
        "agent": actor, "model": model,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "microtask_id": microtask_id,
    })
    return updated
