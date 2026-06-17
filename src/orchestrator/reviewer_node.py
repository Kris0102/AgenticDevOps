from datetime import datetime, timezone
from typing import Any, Dict

from db.db import db
from db.event_log import EventLogger
from db.models import Run
from observability import Category, get_logger
from orchestrator.state import BlackboardState, EventType, RunStatus
from agents.Planning.Reviewer.reviewer import Reviewer
from tool_gateway.Roles import Roles

_log = get_logger(Category.REVIEWER)


class ReviewerRunner:
    """Owns one Reviewer invocation: invoke the reviewer and persist the resulting run status."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.REVIEWER.value

    def invoke(self) -> Dict[str, Any]:
        with db.session_scope() as db_session:
            try:
                result = Reviewer(run_id=self.run_id).invoke(self.state)
            except Exception as error:
                _log.error("reviewer.unexpected_error", error=str(error))
                raise

            self._persist_results(db_session, result)
            return result

    def _persist_results(self, db_session, result: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        new_status = result.get("status", RunStatus.NEEDS_PLAN) # default to NEEDS_PLAN if no explicit status is returned

        run_record = db_session.get(Run, self.run_id)
        if run_record:
            old_status = run_record.status
            run_record.status = new_status.value
            run_record.updated_at = now
            EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_STATUS_CHANGED, {
                "from": old_status,
                "to": new_status.value,
                "reason": "plan reviewer completed",
            })
