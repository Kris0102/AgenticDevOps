from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.db import db
from db.event_log import EventLogger
from db.models import Artifact as DbArtifact, MicrotaskRecord, Run
from Exceptions.RunExecutionError import RunExecutionError
from agents.Implementing.Tester.tester import Tester
from agents.Implementing.Tester.tester_tools import TesterTools
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import (
    BlackboardState,
    CheckOutput,
    Evidence,
    EventType,
    Microtask,
    MicrotaskStatus,
    RunStatus,
)
from sandbox.sandbox_tools import DockerSandboxTools
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway

_log = get_logger(Category.TESTER)
_FINISHED = {MicrotaskStatus.COMPLETED, MicrotaskStatus.DONE}


class TesterRunner:
    """Owns one Tester invocation: select the current microtask, run checks,
    persist the results, and return the next run status for the orchestrator."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.TESTER.value

    def invoke(self) -> Dict[str, Any]:
        microtask = self._select_microtask()
        if microtask is None:
            return self._finish_run()

        remaining = self._has_remaining_microtasks(microtask.microtask_id)
        final_validation = not remaining

        with db.session_scope() as db_session:
            try:
                gateway = self._init_gateway(db_session)
                tester_tools = TesterTools(gateway=gateway)
                tester = Tester(run_id=self.run_id, tester_tools=tester_tools)
                result = tester.invoke(self.state, microtask, final_validation=final_validation)
            except RunExecutionError as error:
                _log.error("tester.run_execution_error", error=str(error))
                return RunFailureHandler.handle(db_session, self.run_id, self.name, self.state, error)
            except Exception as error:
                _log.error("tester.unexpected_error", error=str(error))
                raise

            new_microtasks, new_evidence = self._persist_results(db_session, microtask, result)
            return self._build_state_result(microtask, result, new_microtasks, new_evidence)

    def _select_microtask(self) -> Optional[Microtask]:
        microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        by_id = {mt.microtask_id: mt for mt in microtasks}

        current_id = self.state.get("current_microtask_id")
        if current_id and current_id in by_id and by_id[current_id].status not in _FINISHED:
            return by_id[current_id]

        for mt in sorted(microtasks, key=lambda item: item.idx):
            if mt.status in {MicrotaskStatus.IN_PROGRESS, MicrotaskStatus.EXECUTING, MicrotaskStatus.VERIFYING, MicrotaskStatus.PENDING}:
                return mt

        return None

    def _has_remaining_microtasks(self, current_id: str) -> bool:
        microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        return any(mt.microtask_id != current_id and mt.status not in _FINISHED for mt in microtasks)

    def _init_gateway(self, db_session) -> ToolGateway:
        sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
        return ToolGateway(db_session=db_session, run_id=self.run_id, sandbox_tools=sandbox_tools)

    def _persist_results(
        self,
        db_session,
        microtask: Microtask,
        result: Dict[str, Any],
    ) -> tuple[List[Microtask], Evidence]:
        now = datetime.now(timezone.utc)
        # Tester always routes to REFLECT — the Reflector decides approve/improve.
        # Reflector marks microtasks COMPLETED on approve; tester never does.
        next_status = RunStatus.REFLECT
        final_validation = bool(result.get("final_validation"))
        checks = list(result.get("check_results") or [])

        evidence = self.state.get("evidence") or Evidence()
        check_updates: Dict[str, CheckOutput] = {}
        for check in checks:
            artifact_ref = self._store_check_artifact(db_session, microtask.microtask_id, check, result, now)
            check_updates[check["label"]] = CheckOutput(
                status=check["status"],
                exit_code=int(check["exit_code"]),
                artifact_ref=artifact_ref,
            )

        new_evidence = evidence.model_copy(update={
            "checks": evidence.checks.model_copy(update=check_updates),
        })

        record = db_session.get(MicrotaskRecord, microtask.microtask_id)
        if record:
            record.updated_at = now

        run_record = db_session.get(Run, self.run_id)
        old_status = run_record.status if run_record else self.state.get("status", RunStatus.NEEDS_TEST).value
        if run_record:
            run_record.status = next_status.value
            run_record.updated_at = now

        # MICROTASK_UPDATED: tests ran, routing to reflector for verdict.
        check_status = checks[0]["status"] if checks else "unknown"
        EventLogger.log(db_session, self.run_id, self.name, EventType.MICROTASK_UPDATED, {
            "microtask_id": microtask.microtask_id,
            "final_validation": final_validation,
            "summary": result.get("summary", ""),
            "check_status": check_status,
            "checks": [
                {
                    "label": check["label"],
                    "status": check["status"],
                    "exit_code": check["exit_code"],
                    "artifact_ref": check.get("artifact_ref", ""),
                }
                for check in checks
            ],
        })
        EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_STATUS_CHANGED, {
            "from": old_status,
            "to": next_status.value,
            "reason": result.get("summary", "tester completed, routing to reflector"),
        })

        updated_microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        return updated_microtasks, new_evidence

    def _store_check_artifact(
        self,
        db_session,
        microtask_id: str,
        check: Dict[str, Any],
        result: Dict[str, Any],
        now: datetime,
    ) -> str:
        artifact_ref = f"art_{self.run_id}_test_{microtask_id}_{check['label']}_{now.strftime('%Y%m%d%H%M%S%f')}"
        db_session.merge(DbArtifact(
            artifact_id=artifact_ref,
            run_id=self.run_id,
            kind=f"test-{check['label']}",
            content_text=(
                f"Command: {' '.join(check['command'])}\n"
                f"Status: {check['status']}\n"
                f"Exit code: {check['exit_code']}\n"
                f"Duration ms: {check['duration_ms']}\n"
                f"Summary: {result.get('summary', '')}\n\n"
                f"{check.get('artifact_text', '')}"
            ),
            created_at=now,
        ))
        check["artifact_ref"] = artifact_ref
        return artifact_ref

    def _build_state_result(
        self,
        microtask: Microtask,
        result: Dict[str, Any],
        updated_microtasks: List[Microtask],
        new_evidence: Evidence,
    ) -> Dict[str, Any]:
        # Fields propagated on every exit path so they're always up-to-date in state.
        shared: Dict[str, Any] = {
            "last_test_output_path": result.get("last_test_output_path"),
            "test_rejection_note": result.get("test_rejection_note"),  # None clears it
        }
        # Always route to REFLECT — the Reflector reads evidence.checks and decides
        # whether to approve (advance) or improve (coder retries with new Reflection).
        return {
            "status": RunStatus.REFLECT,
            "current_microtask_id": microtask.microtask_id,
            "evidence": new_evidence,
            **shared,
        }

    def _finish_run(self) -> Dict[str, Any]:
        _log.info("tester.run_done", reason="no pending microtasks")
        with db.session_scope() as db_session:
            now = datetime.now(timezone.utc)
            run_record = db_session.get(Run, self.run_id)
            old_status = run_record.status if run_record else self.state.get("status", RunStatus.DONE).value
            if run_record:
                run_record.status = RunStatus.DONE.value
                run_record.updated_at = now
            EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_STATUS_CHANGED, {
                "from": old_status,
                "to": RunStatus.DONE.value,
                "reason": "no pending microtasks left for the tester",
            })
            EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_COMPLETED, {
                "summary": "no pending microtasks left for the tester",
            })
        return {"status": RunStatus.DONE, "current_microtask_id": None}