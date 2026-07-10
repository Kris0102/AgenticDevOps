"""Shared `semantic_search` tool wrapper for every agent that can query the code index
(planner, reviewer, coder, tester, reflector).

Runs `SEMANTIC_SEARCH` through the gateway and formats each hit with a short inline code
SNIPPET — so one call gives the model *locate + a peek* instead of a search-then-read round
trip (the store already returns `chunk_text`; the old wrappers threw it away and emitted only
`filepath :: symbol`). The snippet is bounded so a top-k search can't blow up the ReAct
context; the model still reads the file for full context when it needs more.
"""
from __future__ import annotations

from tool_gateway.gateway import ToolGateway
from tool_gateway.Roles import Roles
from sandbox.Tools import Tools

_SNIPPET_LINES = 8
_SNIPPET_CHARS = 600

SEMANTIC_SEARCH_DESCRIPTION = (
    "Semantically search the indexed repository for code relevant to a natural-language "
    "query (e.g. 'where are HTTP routes defined', 'auth middleware'). Returns the most "
    "relevant functions/classes with their file path AND a short code snippet. Prefer this "
    "over grepping or reading whole files to LOCATE where something lives; read the file for "
    "the full context once you've found it."
)


def _snippet(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    head = "\n".join(lines[:_SNIPPET_LINES])
    if len(head) > _SNIPPET_CHARS:
        return head[:_SNIPPET_CHARS] + " …"
    if len(lines) > _SNIPPET_LINES:
        head += "\n  …"
    return head


def run_semantic_search(gateway: ToolGateway, role: Roles, query: str, top_k: int = 8) -> str:
    """Execute + format a semantic search for `role`. Returns an LLM-facing string."""
    if gateway is None:
        raise RuntimeError("Sandbox tools are not available. Docker initialization failed.")
    result = gateway.execute_tool(
        actor_role=role,
        tool_name=Tools.SEMANTIC_SEARCH,
        args={"query": query, "top_k": top_k},
    )
    if "error" in result:
        return result["error"]
    hits = result.get("hits", [])
    if not hits:
        return "No relevant code found in the semantic index for that query."
    blocks = [f"Top {len(hits)} semantic matches (snippet — read the file for full context):"]
    for h in hits:
        symbol = h.get("symbol") or h.get("chunk_type")
        snip = _snippet(h.get("chunk_text", ""))
        blocks.append(f"\n### {h['filepath']} :: {symbol}  (score {h['score']})\n{snip}")
    return "\n".join(blocks)
