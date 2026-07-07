class TokenBudgetExceededError(Exception):
    """Raised when `Constraints.max_total_tokens` has been reached (ADR 0025) —
    checked at the top of every agent node's `invoke()`, before spending another LLM call.

    Deliberately NOT a `RunExecutionError`: it must not be caught by the node's own
    `except RunExecutionError` handler and routed to `RunFailureHandler` (terminal FAILED).
    Instead it escapes `graph.invoke()` up to `Service._drive`, which catches it
    specifically and leaves the run non-terminal (STALLED, checkpoint intact) — the human
    can raise the budget via `/continue`'s body (ADR 0025), or close the case via
    `/fail`. See ADR 0025 (why token-budget stalls are recoverable, unlike other
    deterministic caps).
    """

    def __init__(self, phase: str, used: int, limit: int):
        self.phase = phase
        self.used = used
        self.limit = limit
        self.reason = f"Token budget exceeded: {used}/{limit} tokens used"
        super().__init__(f"[token_budget:{phase}] {self.reason}")
