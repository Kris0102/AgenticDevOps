class RetriableError(Exception):
    """Raised for a TRANSIENT failure that is not the run's fault — an LLM API timeout,
    connection drop, rate-limit, or 5xx from the inference cluster. The run's state is
    intact; the same node just needs to run again.

    The `_drive` guard treats this differently from a real error: instead of marking the
    run FAILED, it leaves the run non-terminal so it reads STALLED (no worker driving it)
    and can be re-driven from its checkpoint via POST /runs/{id}/continue. Only whitelist
    genuinely transient, safe-to-retry causes here — anything deterministic must still FAIL,
    or a doomed run looks retriable forever. See ADR 0019 / continue feature.
    """

    def __init__(self, reason: str, phase: str = "llm"):
        self.phase = phase
        self.reason = reason
        super().__init__(f"[transient:{phase}] {reason}")
