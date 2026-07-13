"""Token accounting shared by every agent (ADR 0025). LangChain's `AIMessage.usage_metadata`
is a standardized dict (`input_tokens`/`output_tokens`/`total_tokens`) exposed by both
ChatAnthropic and ChatOpenAI responses — `sum_usage` accumulates it across every AIMessage in
a ReAct trajectory (one per tool-call round) or a single structured-output call.
"""
from __future__ import annotations

from typing import Iterable

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    # Prompt-caching breakdown (GH #62) — SUBSETS of input_tokens, never added on top
    # (LangChain's input_tokens already includes them; these are informational).
    # cache_read costs ~0.1x input price, cache_creation ~1.25x. Both 0 for providers
    # that don't report caching (the cluster today).
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
        )


def sum_usage(messages: Iterable[BaseMessage]) -> TokenUsage:
    """Sum `.usage_metadata` across every AIMessage in `messages`. Messages without usage
    data (e.g. a provider that doesn't report it) are silently skipped — never raises.

    The per-invocation total returned here is what each node records into the durable
    `llm.usage` event (orchestrator/token_budget) — the ONE source of truth for run cost
    (providers/pricing). We deliberately do NOT accumulate per-model tallies onto the
    RunConfig here: nodes never return `config` in their result dict, so any such mutation
    is dropped at the next checkpoint (and is empty on a crash) — a dead, misleading
    second source. Cost is derived from the events instead."""
    usage = TokenUsage()
    for message in messages:
        if not isinstance(message, AIMessage) or not message.usage_metadata:
            continue
        meta = message.usage_metadata
        input_details = meta.get("input_token_details") or {}
        usage = usage + TokenUsage(
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            total_tokens=meta.get("total_tokens", 0),
            cache_read_tokens=input_details.get("cache_read", 0) or 0,
            cache_creation_tokens=input_details.get("cache_creation", 0) or 0,
        )
    return usage
