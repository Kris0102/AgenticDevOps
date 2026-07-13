"""Token → USD cost pricing (GH #62 cost view).

The dollar cost of a run is a PURE DERIVATION over the durable `llm.usage`
(`LLM_USAGE_RECORDED`) events — each carries the agent (phase), the model, and the
input/output/cache token split, and is written on EVERY path including a crash
(`orchestrator/token_budget.record_usage_safe`). So cost is accurate live, after a
clean finish, AND after a crash/stall — unlike anything computed in the terminal
deploy node, which a failed run never reaches.

This module is the single source of truth for per-model rates and the cost formula;
the API service aggregates the events by (phase, model) and calls `cost_for` here, and
the dashboard renders the result. Rates are LIST PRICES per 1M tokens (USD). A model not
in `PRICE_TABLE` (e.g. a self-hosted cluster model) prices to None — the run's total is
then a lower bound flagged `has_unpriced`, never a wrong number.
"""
from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel


class ModelPrice(BaseModel):
    """List price for a model, USD per 1,000,000 tokens. Cache rates are the standard
    Anthropic multipliers off the input price (read ~0.1x, 5-min write ~1.25x), applied
    in `cost_for` rather than stored, so the table stays a single input/output pair."""
    input: float
    output: float


# Anthropic list prices (USD / 1M tokens), from the current models catalog. Bare model
# ids only (the anthropic provider sends no prefix). NOTE: Sonnet 5 has an intro rate of
# $2/$10 through 2026-08-31; we price at the standard $3/$15 so the estimate never
# UNDER-reports once the intro lapses (a cost view that surprises high is a bug; one that
# surprises low after a price rise is worse). Cluster / OpenAI-compatible models are
# intentionally absent → priced as unknown.
PRICE_TABLE: Dict[str, ModelPrice] = {
    "claude-fable-5":   ModelPrice(input=10.0, output=50.0),
    "claude-mythos-5":  ModelPrice(input=10.0, output=50.0),
    "claude-opus-4-8":  ModelPrice(input=5.0,  output=25.0),
    "claude-opus-4-7":  ModelPrice(input=5.0,  output=25.0),
    "claude-opus-4-6":  ModelPrice(input=5.0,  output=25.0),
    "claude-opus-4-5":  ModelPrice(input=5.0,  output=25.0),
    "claude-sonnet-5":  ModelPrice(input=3.0,  output=15.0),
    "claude-sonnet-4-6": ModelPrice(input=3.0, output=15.0),
    "claude-sonnet-4-5": ModelPrice(input=3.0, output=15.0),
    "claude-haiku-4-5": ModelPrice(input=1.0,  output=5.0),
}

# Cache-rate multipliers off the input price (standard Anthropic, 5-min TTL).
_CACHE_READ_MULT = 0.1
_CACHE_WRITE_MULT = 1.25


class Cost(BaseModel):
    """USD cost breakdown for one bucket of token usage. `cache_read`/`cache_write` are
    the caching-specific slices of the input spend, priced at their own multipliers."""
    input: float = 0.0          # uncached input tokens at full price
    output: float = 0.0
    cache_read: float = 0.0     # cache hits (~0.1x input)
    cache_write: float = 0.0    # cache writes (~1.25x input)
    total: float = 0.0


def cost_for(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Optional[Cost]:
    """USD cost for one model's token usage, or None if the model has no list price.

    `cache_read_tokens`/`cache_creation_tokens` are SUBSETS of `input_tokens` (LangChain's
    input count already includes them — see providers/token_usage.py), so the full-price
    input is the remainder after both are removed."""
    price = PRICE_TABLE.get(model)
    if price is None:
        return None

    uncached_input = max(0, input_tokens - cache_read_tokens - cache_creation_tokens)
    input_cost = uncached_input / 1_000_000 * price.input
    read_cost = cache_read_tokens / 1_000_000 * price.input * _CACHE_READ_MULT
    write_cost = cache_creation_tokens / 1_000_000 * price.input * _CACHE_WRITE_MULT
    output_cost = output_tokens / 1_000_000 * price.output
    return Cost(
        input=input_cost,
        output=output_cost,
        cache_read=read_cost,
        cache_write=write_cost,
        total=input_cost + read_cost + write_cost + output_cost,
    )
