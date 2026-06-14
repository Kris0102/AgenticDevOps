import json
from typing import Any, List, Sequence

from langchain_core.messages import AIMessage, ToolMessage

_TRANSCRIPT_TRUNCATE = 2000


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


def format_react_transcript(messages: Sequence[Any]) -> str:
    """Render one coding attempt's ReAct messages as thought/tool-call/tool-result
    lines (mirrors Planner's _log_investigation_steps), printing each line live and
    returning the joined text for the per-run code_file_log.txt."""
    lines: List[str] = []
    step = 0
    for message in messages:
        if isinstance(message, AIMessage):
            thought = _ai_text(message)
            if thought:
                step += 1
                lines.append(f"[step {step}] Thought: {thought}")
            for call in getattr(message, "tool_calls", None) or []:
                step += 1
                args = json.dumps(call.get("args", {}), default=str)
                lines.append(f"[step {step}] Tool call: {call.get('name')}({args})")
        elif isinstance(message, ToolMessage):
            step += 1
            result = message.content
            if isinstance(result, str) and len(result) > _TRANSCRIPT_TRUNCATE:
                result = result[:_TRANSCRIPT_TRUNCATE] + f"... [truncated, {len(result)} chars total]"
            lines.append(f"[step {step}] Tool result ({message.name}): {result}")

    for line in lines:
        print(f"[Coder] {line}")
    return "\n".join(lines) if lines else "(no messages — model produced no thoughts/tool calls)"
