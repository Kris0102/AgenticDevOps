from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

from db.event_log import EventLogger
from db.models import Run
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import BlackboardState, EventType, RunStatus


class RunFailureHandler:
    """Centralizes FAILED bookkeeping — status update + audit events.

    Mirrors EventLogger: caller owns the session_scope/commit. State
    snapshotting is handled automatically by the LangGraph checkpointer
    (the returned dict is the node's result — checkpointed per superstep).
    """

    @staticmethod
    def handle(
        session: Session,
        run_id: str,
        actor: str,
        state: BlackboardState | None,
        error: RunExecutionError,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": RunStatus.FAILED, "error_logs": [error.reason]}

        run_record = session.get(Run, run_id)
        if run_record:
            run_record.status = RunStatus.FAILED.value
            run_record.updated_at = datetime.now(timezone.utc)

        if not state:
              EventLogger.log(session, run_id, actor, EventType.RUN_FAILED, {
            "reason": error.reason,
            "failed_phase": error.phase,
            "State cant be recovered.The run is marked as failed.": "Unrecovable state",
               })
              return result
        # `status` may be an enum (same-invoke) or a deserialized string (resumed run) —
        # never call `.value` blindly here, or this failure handler itself crashes and the
        # run is left in a non-terminal status ("never ends").
        prev = state.get("status")
        from_value = prev.value if hasattr(prev, "value") else (str(prev) if prev is not None else "unknown")
        EventLogger.log(session, run_id, actor, EventType.RUN_STATUS_CHANGED, {
            "from": from_value,
            "to": RunStatus.FAILED.value,
            "reason": error.reason,
        })
        EventLogger.log(session, run_id, actor, EventType.RUN_FAILED, {
            "reason": error.reason,
            "failed_phase": error.phase,
        })

        return result
