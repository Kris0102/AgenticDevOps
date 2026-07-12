from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.db import db
from db.event_log import EventLogger
from db.lessons import LessonStore
from db.models import Artifact as DbArtifact, MicrotaskRecord, Run
from Exceptions.RunExecutionError import RunExecutionError
from Exceptions.RetriableError import RetriableError, is_transient_llm_error
from Exceptions.TokenBudgetExceededError import TokenBudgetExceededError
from agents.Implementing.Tester.pipeline import TestPipeline
from agents.Implementing.Tester.results import CheckResult, CheckStatus
from agents.Implementing.Tester.tester_tools import TesterTools
from db.settings_store import resolve_run_config
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.token_budget import record_usage, record_usage_safe, token_budget_exceeded
from orchestrator.state import (
    BlackboardState,
    CheckOutput,
    Constraints,
    Evidence,
    EventType,
    Iteration,
    Microtask,
    MicrotaskStatus,
    Reflection,
    RunConfig,
    RunStatus,
)
from sandbox.sandbox_tools import DockerSandboxTools
from context_engine.engine import ContextEngine
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway

_log = get_logger(Category.TESTER)
_FINISHED = {MicrotaskStatus.COMPLETED, MicrotaskStatus.DONE}


class TesterRunner:
    """Owns one Tester invocation: select the current microtask, run the phase pipeline,
    derive the deterministic PASS/FAIL verdict, persist it, and return the next run status.

    Contract (ADR 0016): the Tester owns its verdict. On PASS it marks the microtask
    COMPLETED/DONE and advances (NEEDS_CODE/DONE) — the CODE HITL gate fires here. On FAIL
    it emits REFLECT, routing to the failure-only Reflector for diagnosis. The verdict is
    read deterministically from the `unit_tests` check the pipeline emits."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.TESTER.value

    def invoke(self) -> Dict[str, Any]:
        microtask = self._select_microtask()
        if microtask is None:
            return self._finish_run()

        final_validation = not self._has_remaining_microtasks(microtask.microtask_id)

        with db.session_scope() as db_session:
            iteration: Iteration = self.state.get("iteration")
            constraints: Constraints = self.state.get("constraints")

            # Global token-budget guard (ADR 0025) — checked before spending another
            # LLM call. Raises (not RunFailureHandler) so the run STALLS instead of FAILS —
            # recoverable via /continue once the budget is raised, or /fail to close it.
            if token_budget_exceeded(iteration, constraints):
                _log.warning("tester.token_budget_exceeded",
                             used=iteration.tokens_used_total, limit=constraints.max_total_tokens)
                raise TokenBudgetExceededError(
                    phase="test", used=iteration.tokens_used_total, limit=constraints.max_total_tokens or -1,
                )

            pipeline: Optional[TestPipeline] = None
            config: Optional[RunConfig] = None
            try:
                gateway = self._init_gateway(db_session)
                tester_tools = TesterTools(gateway=gateway)
                config= self.state.get("config") or resolve_run_config()
                pipeline = TestPipeline(run_id=self.run_id, tester_tools=tester_tools, config=config)
                result = pipeline.run(self.state, microtask, final_validation=final_validation)
            except RunExecutionError as error:
                _log.error("tester.run_execution_error", error=str(error))
                self._record_crash_usage(db_session, iteration, pipeline, config, microtask)
                return RunFailureHandler.handle(db_session, self.run_id, self.name, self.state, error)
            except RetriableError:
                self._record_crash_usage(db_session, iteration, pipeline, config, microtask)
                raise   # transient (LLM timeout) → let _drive mark STALLED, not FAILED
            except Exception as error:
                self._record_crash_usage(db_session, iteration, pipeline, config, microtask)
                # Backstop: a transient LLM failure (429 "overloaded" / timeout / 5xx) that
                # escaped the tester's phases must STALL, not FAIL — the tester has no internal
                # RetriableError handling, so classify it here (ADR 0023).
                if is_transient_llm_error(error):
                    _log.error("tester.cluster_unreachable", error=str(error))
                    raise RetriableError(f"tester LLM call failed: {error}", phase="tester")
                # A generic/unexpected crash is NOT proven unrecoverable — let it escape to
                # `_drive`, which marks the run STALLED (recoverable, checkpoint intact, ADR
                # 0021), NOT terminal FAILED. `handle()` (→ FAILED) is reserved for the
                # deterministic RunExecutionError path above. Consistent with planner/coder.
                _log.error("tester.unexpected_error", error=str(error))
                raise

            # internal hand-off from the pipeline — not a BlackboardState channel, must
            # never reach the graph's node-result dict
            token_usage = result.pop("_token_usage", None)
            updated_iteration = record_usage(
                db_session, self.run_id, self.name, iteration, token_usage, config.llm.tester.model,
                microtask_id=microtask.microtask_id,
            ) if token_usage else iteration

            passed = self._verdict_passed(result)
            new_microtasks, new_evidence = self._persist_results(
                db_session, microtask, result, passed, final_validation)
            state_result = self._build_state_result(
                microtask, result, new_microtasks, new_evidence, passed, final_validation)
            state_result["iteration"] = updated_iteration
            return state_result

    def _record_crash_usage(self, db_session, iteration: Iteration, pipeline: Optional[TestPipeline],
                            config: Optional[RunConfig], microtask: Optional[Microtask]) -> None:
        """Record tokens the Tester pipeline spent this node even though a phase crashed. The
        pipeline exposes `token_usage` as a live attribute for exactly this (its result dict,
        which normally carries usage, is never built on a crash). record_usage_safe no-ops on
        empty usage."""
        if pipeline is None or config is None:
            return
        record_usage_safe(db_session, self.run_id, self.name, iteration, pipeline.token_usage,
                          config.llm.tester.model,
                          microtask.microtask_id if microtask else None)

    @staticmethod
    def _verdict_passed(result: Dict[str, Any]) -> bool:
        """Deterministic verdict from the canonical unit_tests check (skipped = passed,
        nothing to fix). Missing check = treat as failure (route to reflector)."""
        for check in result.get("check_results") or []:
            if check.label == "unit_tests":
                return check.status in (CheckStatus.PASSED, CheckStatus.SKIPPED)
        return False

    @staticmethod
    def _next_status(passed: bool, final_validation: bool) -> RunStatus:
        if not passed:
            return RunStatus.REFLECT
        return RunStatus.DONE if final_validation else RunStatus.NEEDS_CODE

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
        repo = self.state.get("repo")
        # Search-only ContextEngine so the test-writer can locate the code under test via the
        # semantic index instead of blind file reads (GH #71 follow-up).
        return ToolGateway(
            db_session=db_session, run_id=self.run_id, sandbox_tools=sandbox_tools,
            context_engine=ContextEngine(), repo_url=repo.repo_url if repo else "",
        )

    def _persist_results(
        self,
        db_session,
        microtask: Microtask,
        result: Dict[str, Any],
        passed: bool,
        final_validation: bool,
    ) -> tuple[List[Microtask], Evidence]:
        now = datetime.now(timezone.utc)
        next_status = self._next_status(passed, final_validation)
        checks = list(result.get("check_results") or [])

        evidence = self.state.get("evidence") or Evidence()
        check_updates: Dict[str, CheckOutput] = {}
        for check in checks:
            artifact_ref = self._store_check_artifact(db_session, microtask.microtask_id, check, result, now)  # Stores an artifact per check meaning for lint, typecheck, build, unit_tests, integration_tests
            check_updates[check.label] = CheckOutput(   # label has values : unit_tests, integration_tests, build, typecheck, or lint   !one of those!
                status=check.status.value,
                exit_code=check.exit_code,
                artifact_ref=artifact_ref,
            )

        new_evidence = evidence.model_copy(update={
            "checks": evidence.checks.model_copy(update=check_updates),   # Here the art ref is saved to state.py 
        })

        # On PASS the Tester owns completion: mark the microtask DONE (final) / COMPLETED.
        # TODO the reflector should alsways be triggert regardless of pass or not!
        
        updated_microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        completed_status = MicrotaskStatus.DONE if final_validation else MicrotaskStatus.COMPLETED
        if passed:
            updated_microtasks = [
                mt.model_copy(update={"status": completed_status})
                if mt.microtask_id == microtask.microtask_id else mt
                for mt in updated_microtasks
            ]
            # Outcome-validated lesson write (ADR 0018): the microtask is green, so the
            # reflector's code diagnosis that drove the fix is confirmed reusable.
            self._record_code_lesson(db_session, microtask)

        record = db_session.get(MicrotaskRecord, microtask.microtask_id)
        if record:
            if passed:
                record.status = completed_status.value
            record.updated_at = now

        run_record = db_session.get(Run, self.run_id)
        old_status = run_record.status if run_record else self.state.get("status", RunStatus.NEEDS_TEST).value
        if run_record:
            run_record.status = next_status.value
            run_record.updated_at = now

        # PASS → microtask.completed; FAIL → microtask.failed (routes to the reflector).
        event_type = EventType.MICROTASK_COMPLETED if passed else EventType.MICROTASK_FAILED
        EventLogger.log(db_session, self.run_id, self.name, event_type, {
            "microtask_id": microtask.microtask_id,
            "final_validation": final_validation,
            "summary": result.get("summary", ""),
            "checks": [
                {
                    "label": check.label,
                    "status": check.status.value,
                    "exit_code": check.exit_code,
                    "artifact_ref": check.artifact_ref,
                }
                for check in checks
            ],
        })
        EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_STATUS_CHANGED, {
            "from": old_status,
            "to": next_status.value,
            "reason": result.get("summary", "tester verdict"),
        })
        if next_status == RunStatus.DONE:
            EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_COMPLETED, {
                "summary": result.get("summary", "tester passed final validation"),
            })

        return updated_microtasks, new_evidence

    def _store_check_artifact(
        self,
        db_session,
        microtask_id: str,
        check: "CheckResult",
        result: Dict[str, Any],
        now: datetime,
    ) -> str:
        artifact_ref = f"art_{self.run_id}_test_{microtask_id}_{check.label}_{now.strftime('%Y%m%d%H%M%S%f')}"
        db_session.merge(DbArtifact(
            artifact_id=artifact_ref,
            run_id=self.run_id,
            kind=f"test-{check.label}",
            microtask_id=microtask_id,
            content_text=(
                f"Command: {' '.join(check.command)}\n"
                f"Status: {check.status}\n"
                f"Exit code: {check.exit_code}\n"
                f"Duration ms: {check.duration_ms}\n"
                f"Summary: {result.get('summary', '')}\n\n"
                f"{check.artifact_text}"  # STDOUT/STDERR/Gateway error
            ),
            created_at=now,
        ))
        check.artifact_ref = artifact_ref
        return artifact_ref

    def _record_code_lesson(self, db_session, microtask: Microtask) -> None:
        """Persist the reflector's code diagnosis for this (now-passing) microtask as a
        durable per-repo lesson. The latest reflection on this microtask is the diagnosis the
        green run confirmed; LessonStore gates it to code-target, reusable, non-empty. A
        microtask that passed first try has no reflection → nothing stored."""
        reflections: List[Reflection] = list(self.state.get("reflections") or [])
        latest = next(
            (r for r in reversed(reflections) if r.microtask_id == microtask.microtask_id),
            None,
        )
        repo = self.state.get("repo")
        LessonStore.record_code_lessons(
            db_session, self.run_id, repo.repo_url if repo else "", latest, self.name)

    def _build_state_result(
        self,
        microtask: Microtask,
        result: Dict[str, Any],
        updated_microtasks: List[Microtask],
        new_evidence: Evidence,
        passed: bool,
        final_validation: bool,
    ) -> Dict[str, Any]:
        # Fields propagated on every exit path so they're always up-to-date in state.
        shared: Dict[str, Any] = {
            "last_test_output_path": result.get("last_test_output_path"),
            "test_rejection_note": result.get("test_rejection_note"),  # None clears it
            "evidence": new_evidence,
            # Run-level existing-suite baseline = pristine failing-test count (GH #28/#72),
            # established on the first Tester run then fixed; carried on every exit so it
            # survives to the next microtask.
            "suite_baseline_failures": result.get("suite_baseline_failures"),
        }

        if not passed:
            # FAIL → reflector diagnoses; keep the microtask in flight for the coder retry.
            return {
                "status": RunStatus.REFLECT,
                "current_microtask_id": microtask.microtask_id,
                **shared,
            }

        # PASS → advance. The CODE gate fires here (orchestrator wraps this result).
        return {
            "status": self._next_status(passed, final_validation),  # NEEDS_CODE or DONE
            "microtasks": updated_microtasks,
            "current_microtask_id": None,
            # Consumed by HitlGate.apply(Phase.CODE) so a gate rejection can reset this microtask.
            "_tested_microtask_id": microtask.microtask_id,
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