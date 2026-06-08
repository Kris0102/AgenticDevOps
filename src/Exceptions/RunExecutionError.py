class RunExecutionError(Exception):
    """Raised by an agent/node when it hits an unrecoverable error — the
    Orchestrator marks the run FAILED and persists a snapshot in response."""

    def __init__(self, phase: str, reason: str):
        self.phase = phase
        self.reason = reason
        super().__init__(f"[{phase}] {reason}")
