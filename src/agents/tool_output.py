"""Shared cap on LLM-facing tool output for every ReAct agent (planner, reviewer, coder,
tester).

A ReAct loop resends its whole message history to the model on every step, so one uncapped
tool result (a big ``cat``, an ``npm test`` trace, a lockfile read) compounds quadratically
across the loop — a single runaway read once cost a tester call ~245k tokens (GH #68). Every
LLM-facing tool wrapper routes its output through :func:`truncate_for_llm` to bound that.

Only the LLM-facing wrappers cap. Deterministic phases that parse tool output
programmatically (the Tester's checks, the Coder's diff verification) read the raw gateway
result and must never go through here — the full output still lives in the durable event log.
"""
from __future__ import annotations

# Command stdout/stderr, diffs, semantic dumps.
MAX_TOOL_OUTPUT_CHARS = 6000
# File reads — a touch larger; the model often needs a whole small module.
MAX_FILE_READ_CHARS = 8000


def truncate_for_llm(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    """Keep head+tail (a command's signal lives at the start/end) with a marker in between.
    Returns ``text`` unchanged when already within ``limit``."""
    if not text or len(text) <= limit:
        return text or ""
    head = limit * 2 // 3
    tail = limit - head
    dropped = len(text) - head - tail
    return (
        f"{text[:head]}\n"
        f"… [{dropped} chars truncated — narrow your query or read a specific line range] …\n"
        f"{text[-tail:]}"
    )
