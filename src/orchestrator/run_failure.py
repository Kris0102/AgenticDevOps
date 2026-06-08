from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

from db.event_log import EventLogger
from db.models import Run
from db.run_state import RunStateStore
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import BlackboardState, EventType, RunStatus, merge_state


class RunFailureHandler:
    """Centralizes FAILED bookkeeping — status update, audit events, snapshot.

    Mirrors EventLogger/RunStateStore: caller owns the session_scope/commit.
    """

    @staticmethod
    def handle(
        session: Session,
        run_id: str,
        actor: str,
        state: BlackboardState,
        error: RunExecutionError,
    ) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": RunStatus.FAILED, "error_logs": [error.reason]}

        run_record = session.get(Run, run_id)
        if run_record:
            run_record.status = RunStatus.FAILED.value
            run_record.updated_at = datetime.now(timezone.utc)

        EventLogger.log(session, run_id, actor, EventType.RUN_STATUS_CHANGED, {
            "from": state["status"].value,
            "to": RunStatus.FAILED.value,
            "reason": error.reason,
        })
        EventLogger.log(session, run_id, actor, EventType.RUN_FAILED, {
            "reason": error.reason,
            "failed_phase": error.phase,
        })

        RunStateStore.save(session, run_id, merge_state(state, result))
        return result
