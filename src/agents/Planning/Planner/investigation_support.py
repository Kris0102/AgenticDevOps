import os
import json
from typing import Any, Sequence

from langchain_core.messages import AIMessage, ToolMessage


def _default_max_tool_steps() -> int:
    raw = os.getenv("PLANNER_MAX_TOOL_STEPS", "15")
    try:
        return max(1, int(raw))
    except ValueError:
        return 15


def _investigation_recursion_limit(max_tool_steps: int) -> int:
    # Each tool round is typically model + tools nodes (≈2 supersteps).
    return max_tool_steps * 2 + 5


def _ai_text(message: AIMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _log_investigation_steps(messages: Sequence[Any]) -> None:
    """Print each ReAct step (model thought, tool calls, tool results) in order."""
    step = 0
    for message in messages:
        if isinstance(message, AIMessage):
            thought = _ai_text(message)
            if thought:
                step += 1
                print(f"[Planner][Investigation step {step}] Thought: {thought}")
            for call in getattr(message, "tool_calls", None) or []:
                step += 1
                args = json.dumps(call.get("args", {}), default=str)
                print(f"[Planner][Investigation step {step}] Tool call: {call.get('name')}({args})")
        elif isinstance(message, ToolMessage):
            step += 1
            result = message.content
            if isinstance(result, str) and len(result) > 1000:
                result = result[:1000] + "... [truncated]"
            print(f"[Planner][Investigation step {step}] Tool result ({message.name}): {result}")


def _last_ai_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _ai_text(message)
            if text:
                return text
    return ""
