from datetime import datetime, timezone
from typing import Any, Dict, List

from db.db import db
from db.event_log import EventLogger
from db.models import MicrotaskRecord, Run
from orchestrator.state import (
    BlackboardState,
    CheckOutput,
    EventType,
    Microtask,
    MicrotaskStatus,
    RunStatus,
)
from tool_gateway.Roles import Roles

_FINISHED = {MicrotaskStatus.COMPLETED, MicrotaskStatus.DONE}


class TesterRunner:
    """Minimal stub Tester — unblocks the coder<->tester loop end-to-end.

    No real test execution yet (CLAUDE.md Tester TODO): marks the microtask the
    Coder just produced a diff for as COMPLETED, records a 'skipped' unit_tests
    check (honest about not being grounded), and routes back to coder for the
    next PENDING microtask or DONE if none remain.
    """

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.TESTER.value

    def invoke(self) -> Dict[str, Any]:
        microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        current_id = self.state.get("current_microtask_id")

        updated_microtasks = [
            mt.model_copy(update={"status": MicrotaskStatus.COMPLETED})
            if mt.microtask_id == current_id else mt
            for mt in microtasks
        ]
        remaining = any(mt.status not in _FINISHED for mt in updated_microtasks)
        next_status = RunStatus.NEEDS_CODE if remaining else RunStatus.DONE

        evidence = self.state.get("evidence")
        new_evidence = evidence.model_copy(update={
            "checks": evidence.checks.model_copy(update={
                "unit_tests": CheckOutput(status="skipped", exit_code=0, artifact_ref=""),
            }),
        })

        with db.session_scope() as db_session:
            now = datetime.now(timezone.utc)
            record = db_session.get(MicrotaskRecord, current_id)
            if record:
                record.status = MicrotaskStatus.COMPLETED.value
                record.updated_at = now

            run_record = db_session.get(Run, self.run_id)
            if run_record:
                run_record.status = next_status.value
                run_record.updated_at = now

            EventLogger.log(db_session, self.run_id, self.name, EventType.MICROTASK_COMPLETED, {
                "microtask_id": current_id,
                "note": "stub tester — unit_tests check skipped, not actually run",
            })

        return {
            "status": next_status,
            "microtasks": updated_microtasks,
            "current_microtask_id": None,  # here we will set the next microtask to work on, but for now the router just checks if any remain and routes accordingly
            "evidence": new_evidence,
        }
