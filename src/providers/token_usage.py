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

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


def sum_usage(messages: Iterable[BaseMessage]) -> TokenUsage:
    """Sum `.usage_metadata` across every AIMessage in `messages`. Messages without usage
    data (e.g. a provider that doesn't report it) are silently skipped — never raises."""
    usage = TokenUsage()
    for message in messages:
        if not isinstance(message, AIMessage) or not message.usage_metadata:
            continue
        meta = message.usage_metadata
        usage = usage + TokenUsage(
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            total_tokens=meta.get("total_tokens", 0),
        )
    return usage
