import os
import json
from typing import Any, Sequence

from langchain_core.messages import AIMessage, ToolMessage

from observability import Category, get_logger

_log = get_logger(Category.REVIEWER)


def _default_max_tool_steps() -> int:
    raw = os.getenv("REVIEWER_MAX_TOOL_STEPS", "15")
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
                _log.debug("reviewer.investigation.thought", step=step, thought=thought)
            for call in getattr(message, "tool_calls", None) or []:
                step += 1
                args = json.dumps(call.get("args", {}), default=str)
                _log.debug("reviewer.investigation.tool_call", step=step,
                           tool=call.get("name"), args=args)
        elif isinstance(message, ToolMessage):
            step += 1
            result = message.content
            if isinstance(result, str) and len(result) > 1000:
                result = result[:1000] + "... [truncated]"
            _log.debug("reviewer.investigation.tool_result", step=step,
                       tool=message.name, result=result)


def _last_ai_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _ai_text(message)
            if text:
                return text
    return ""
