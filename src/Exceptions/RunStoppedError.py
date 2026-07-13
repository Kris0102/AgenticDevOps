class RunStoppedError(RuntimeError):
    """Raised by a graph node wrapper when it finds this run_id flagged for a
    human-requested stop (Service.stop_run, ADR 0033) — aborts the node immediately,
    before it does any real work, instead of letting the graph advance further."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"run {run_id} was stopped by the user")
