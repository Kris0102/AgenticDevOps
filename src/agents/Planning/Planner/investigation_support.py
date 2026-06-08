import os
from typing import Any, Sequence

from langchain_core.messages import AIMessage


def _default_max_tool_steps() -> int:
    raw = os.getenv("PLANNER_MAX_TOOL_STEPS", "15")
    try:
        return max(1, int(raw))
    except ValueError:
        return 15


def _investigation_recursion_limit(max_tool_steps: int) -> int:
    # Each tool round is typically model + tools nodes (≈2 supersteps).
    return max_tool_steps * 2 + 5


def _last_ai_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.content:
            content = message.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                joined = "\n".join(p for p in parts if p)
                if joined:
                    return joined
    return ""
