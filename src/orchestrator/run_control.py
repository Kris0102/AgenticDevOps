import threading

# Cooperative stop-request registry (ADR 0033 amendment). A hard sandbox kill alone
# cannot reliably interrupt a live run: tool-call exceptions are caught deep inside
# ToolGateway/tool wrappers and fed back to the LLM as an ordinary observation instead
# of propagating, and much of a ReAct agent's investigation may never touch the sandbox
# at all (e.g. semantic_search over pgvector) — so a killed container can be a complete
# no-op for whatever the node is doing right now. This registry gives every graph node
# wrapper a cheap, reliable checkpoint instead: "has a human asked this run_id to stop?"
# Checked once at the top of each node, so a stop request bounds the run to at most one
# more in-flight node's worth of work (bounded by that node's own tool-step/timeout
# budget) rather than an unbounded number of further plan/review/code cycles.
_stop_requested: set[str] = set()
_lock = threading.Lock()


def request_stop(run_id: str) -> None:
    with _lock:
        _stop_requested.add(run_id)


def is_stop_requested(run_id: str) -> bool:
    with _lock:
        return run_id in _stop_requested


def clear_stop(run_id: str) -> None:
    with _lock:
        _stop_requested.discard(run_id)
