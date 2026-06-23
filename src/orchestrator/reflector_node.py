from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.db import db
from db.event_log import EventLogger
from db.models import Artifact as DbArtifact, MicrotaskRecord, Run
from Exceptions.RunExecutionError import RunExecutionError
from agents.Implementing.Reflector.reflector import Reflector
from agents.Implementing.Reflector.reflector_tools import ReflectorTools
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import (
    BlackboardState,
    EventType,
    Microtask,
    MicrotaskStatus,
    Reflection,
    RunStatus,
)
from sandbox.sandbox_tools import DockerSandboxTools
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway

_log = get_logger(Category.REFLECTOR)
_FINISHED = {MicrotaskStatus.COMPLETED, MicrotaskStatus.DONE}


class ReflectorRunner:
    """Owns one Reflector invocation: select the in-flight microtask, read
    test-pass context written by the tester, invoke the Reflector agent, then
    persist and advance state.

    Approve path: marks the microtask COMPLETED (or DONE if it was the last),
    clears current_microtask_id, and emits NEEDS_CODE / DONE so the coder picks
    up the next task (or the pipeline deploys).

    Improve path: keeps the microtask in VERIFYING with current_microtask_id
    set, emits NEEDS_CODE so the coder retries using the new Reflection.
    """

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.REFLECTOR.value

    def invoke(self) -> Dict[str, Any]:
        # Derive test pass/fail from evidence.checks written by the tester node
        # (not from a side-channel key — side-channel keys don't survive the
        # TypedDict merge that LangGraph performs between nodes).
        test_passed: bool = self._tests_passed_from_evidence()
        microtask = self._select_microtask(self.state.get("current_microtask_id"))
        if microtask is None:
            _log.warning(
                "reflector.no_microtask",
                reason="no in-progress microtask found — failing run",
            )
            with db.session_scope() as db_session:
                err = RunExecutionError(
                    phase="reflect",
                    reason="Reflector could not find an in-progress microtask to assess.",
                )
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state, err
                )

        with db.session_scope() as db_session:
            try:
                gateway = self._init_gateway(db_session)
                reflector_tools = ReflectorTools(gateway=gateway)
                reflector = Reflector(run_id=self.run_id, reflector_tools=reflector_tools)
                raw = reflector.invoke(self.state, microtask, test_passed=test_passed)
            except RunExecutionError as error:
                _log.error("reflector.run_execution_error", error=str(error))
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state, error
                )
            except Exception as error:
                _log.error("reflector.unexpected_error", error=str(error))
                raise

            # Consume the internal flag before it touches LangGraph state.
            approved: bool = bool(raw.pop("_reflector_approved", False))
            # Compute final_validation here (after selecting the microtask) so the
            # approve path knows whether to emit DONE or NEEDS_CODE for the next task.
            final_validation: bool = not self._has_remaining_microtasks(microtask.microtask_id)
            result = self._build_state_result(raw, microtask, approved, final_validation)
            self._persist_results(db_session, microtask, result, approved, final_validation)
            return result

    # ------------------------------------------------------------------ #
    # Helpers                                                               #
    # ------------------------------------------------------------------ #

    def _tests_passed_from_evidence(self) -> bool:
        """Derive test pass/fail from evidence.checks set by the tester node.
        Skipped counts as passed — nothing to fix."""
        evidence = self.state.get("evidence")
        if not evidence:
            return False
        unit = evidence.checks.unit_tests if evidence.checks else None
        if unit is None:
            return False
        return unit.status in ("passed", "skipped")

    def _select_microtask(self, current_id: Optional[str]) -> Optional[Microtask]:
        """Pick the in-flight microtask: current_microtask_id first, then first
        IN_PROGRESS/VERIFYING by idx."""
        microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        by_id = {mt.microtask_id: mt for mt in microtasks}

        if current_id and current_id in by_id and by_id[current_id].status not in _FINISHED:
            return by_id[current_id]

        for mt in sorted(microtasks, key=lambda m: m.idx):
            if mt.status in {MicrotaskStatus.IN_PROGRESS, MicrotaskStatus.EXECUTING, MicrotaskStatus.VERIFYING}:
                return mt

        return None

    def _has_remaining_microtasks(self, current_id: str) -> bool:
        microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        return any(
            mt.microtask_id != current_id and mt.status not in _FINISHED
            for mt in microtasks
        )

    # ------------------------------------------------------------------ #
    # Gateway                                                               #
    # ------------------------------------------------------------------ #

    def _init_gateway(self, db_session) -> ToolGateway:
        sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
        return ToolGateway(db_session=db_session, run_id=self.run_id, sandbox_tools=sandbox_tools)

    # ------------------------------------------------------------------ #
    # State result assembly                                                 #
    # ------------------------------------------------------------------ #

    def _build_state_result(
        self,
        raw: Dict[str, Any],
        microtask: Microtask,
        approved: bool,
        final_validation: bool,
    ) -> Dict[str, Any]:
        """Build the dict that will be merged into BlackboardState."""
        if not approved:
            # Improve path: keep microtask in VERIFYING so coder retries.
            return {
                **raw,
                "status": RunStatus.NEEDS_CODE,
                "current_microtask_id": microtask.microtask_id,
            }

        # Approve path: mark microtask COMPLETED/DONE and advance pipeline.
        remaining = self._has_remaining_microtasks(microtask.microtask_id)
        next_status = RunStatus.NEEDS_CODE if remaining else RunStatus.DONE
        completed_flag = MicrotaskStatus.DONE if not remaining else MicrotaskStatus.COMPLETED

        updated_microtasks = [
            mt.model_copy(update={"status": completed_flag})
            if mt.microtask_id == microtask.microtask_id else mt
            for mt in list(self.state.get("microtasks") or [])
        ]

        return {
            **raw,
            "status": next_status,
            "microtasks": updated_microtasks,
            "current_microtask_id": None,
            # Consumed by HitlGate.apply(Phase.CODE) in the orchestrator node so
            # CODE-gate rejection can reset the microtask back to IN_PROGRESS.
            "_tested_microtask_id": microtask.microtask_id,
        }

    # ------------------------------------------------------------------ #
    # Persistence                                                           #
    # ------------------------------------------------------------------ #

    def _persist_results(
        self,
        db_session,
        microtask: Microtask,
        result: Dict[str, Any],
        approved: bool,
        final_validation: bool,
    ) -> None:
        now = datetime.now(timezone.utc)
        reflections: List[Reflection] = list(result.get("reflections") or [])
        latest = reflections[-1] if reflections else None
        next_status: RunStatus = result.get("status", RunStatus.NEEDS_CODE)

        # Store the reflection as a DB artifact.
        if latest:
            artifact_ref = (
                f"art_{self.run_id}_reflect_{microtask.microtask_id}"
                f"_{now.strftime('%Y%m%d%H%M%S%f')}"
            )
            db_session.merge(DbArtifact(
                artifact_id=artifact_ref,
                run_id=self.run_id,
                kind="reflection",
                content_text=(
                    f"Microtask:         {microtask.microtask_id}\n"
                    f"Decision:          {'approve' if approved else 'improve'}\n"
                    f"Failure Type:      {latest.failure_type}\n"
                    f"Root Cause:        {latest.root_cause}\n"
                    f"Fix Instructions:  {latest.fix_instructions}\n"
                    f"Semantic Memory:   {latest.should_store_in_semantic_memory}\n"
                ),
                created_at=now,
            ))
            EventLogger.log(
                db_session,
                self.run_id,
                self.name,
                EventType.REFLECTION_CREATED,
                {
                    "microtask_id": microtask.microtask_id,
                    "decision": "approve" if approved else "improve",
                    "failure_type": latest.failure_type,
                    "artifact_ref": artifact_ref,
                    "should_store_in_semantic_memory": latest.should_store_in_semantic_memory,
                },
            )

        # On approve: update MicrotaskRecord to COMPLETED/DONE.
        if approved:
            completed_flag = (
                MicrotaskStatus.DONE if next_status == RunStatus.DONE else MicrotaskStatus.COMPLETED
            )
            record = db_session.get(MicrotaskRecord, microtask.microtask_id)
            if record:
                record.status = completed_flag.value
                record.updated_at = now

        # Update Run.status.
        run_record = db_session.get(Run, self.run_id)
        old_status = (
            run_record.status if run_record else self.state.get("status", RunStatus.REFLECT).value
        )
        if run_record:
            run_record.status = next_status.value
            run_record.updated_at = now

        action = "approved" if approved else "improve-requested"
        EventLogger.log(
            db_session,
            self.run_id,
            self.name,
            EventType.RUN_STATUS_CHANGED,
            {
                "from": old_status,
                "to": next_status.value,
                "reason": f"reflector {action} microtask {microtask.microtask_id}",
            },
        )

        if next_status == RunStatus.DONE:
            EventLogger.log(
                db_session,
                self.run_id,
                self.name,
                EventType.RUN_COMPLETED,
                {"summary": f"reflector approved final microtask {microtask.microtask_id}"},
            )
