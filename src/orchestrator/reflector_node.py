import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from db.db import db
from db.settings_store import resolve_run_config
from db.event_log import EventLogger
from db.models import Artifact as DbArtifact, Run
from Exceptions.RunExecutionError import RunExecutionError
from Exceptions.RetriableError import RetriableError, is_transient_llm_error
from Exceptions.TokenBudgetExceededError import TokenBudgetExceededError
from agents.Implementing.Reflector.reflector import Reflector
from agents.Implementing.Reflector.reflector_tools import ReflectorTools
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.token_budget import record_usage, token_budget_exceeded
from orchestrator.state import (
    BlackboardState,
    Constraints,
    EventType,
    Iteration,
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
    """Owns one Reflector invocation. Failure-only (ADR 0016): the Reflector is reached
    ONLY after a tester FAIL (REFLECT). It diagnoses the failure, seeds a Reflection with
    concrete fix instructions, keeps the microtask in flight (current_microtask_id set),
    and emits NEEDS_CODE so the coder retries. It never approves or marks a microtask
    complete — the Tester owns the PASS verdict."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.REFLECTOR.value

    def invoke(self) -> Dict[str, Any]:
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
            iteration: Iteration = self.state.get("iteration")
            constraints: Constraints = self.state.get("constraints")

            # Global token-budget guard (ADR 0025) — checked before spending another
            # LLM call. Raises (not RunFailureHandler) so the run STALLS instead of FAILS —
            # recoverable via /continue once the budget is raised, or /fail to close it.
            if token_budget_exceeded(iteration, constraints):
                _log.warning("reflector.token_budget_exceeded",
                             used=iteration.tokens_used_total, limit=constraints.max_total_tokens)
                raise TokenBudgetExceededError(
                    phase="reflect", used=iteration.tokens_used_total, limit=constraints.max_total_tokens,
                )

            try:
                gateway = self._init_gateway(db_session)
                reflector_tools = ReflectorTools(gateway=gateway)
                config = self.state.get("config") or resolve_run_config()
                reflector = Reflector(run_id=self.run_id, reflector_tools=reflector_tools, config=config)
                # Always a failure here — the Reflector only runs after a tester FAIL.
                # TODO Change thisn behaviour to always run!
                # Reads the artifact which was created by the tester!
                test_output = self._test_output(db_session)
                raw = reflector.invoke(self.state, microtask, test_passed=False, test_output=test_output)
            except RunExecutionError as error:
                _log.error("reflector.run_execution_error", error=str(error))
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state, error
                )
            except RetriableError:
                raise   # transient (LLM timeout) → let _drive mark STALLED, not FAILED
            except Exception as error:
                # Backstop: a transient LLM failure (429 / timeout / 5xx) that escaped the
                # reflector must STALL, not FAIL (ADR 0023).
                if is_transient_llm_error(error):
                    _log.error("reflector.cluster_unreachable", error=str(error))
                    raise RetriableError(f"reflector LLM call failed: {error}", phase="reflector")
                # Convert to a clean FAILED (phase-attributed) instead of unwinding into
                # graph.invoke. The _drive guard is the net; this gives the better label.
                _log.error("reflector.unexpected_error", error=str(error))
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state,
                    RunExecutionError(phase="reflect", reason=f"Reflector crashed: {error}"),
                )

            # Drop the approve signal (failure-only) and read the fix target.
            raw.pop("_reflector_approved", None)
            fix_target = raw.pop("_fix_target", "code")
            result = self._build_state_result(raw, microtask, fix_target)
            result["iteration"] = record_usage(
                db_session, self.run_id, self.name, iteration, reflector.token_usage, config.llm.model,
                microtask_id=microtask.microtask_id,
            )
            self._persist_results(db_session, microtask, result)
            # NOTE: lessons are NOT written here. A reflector diagnosis is unconfirmed — it
            # could be a misdiagnosis. The code-lesson is written later, in tester_node on the
            # microtask PASS that proves the fix worked (outcome-validated, ADR 0018).
            return result

    def _max_test_resynth(self) -> int:
        # Settings hub: from the run's resolved knobs; env fallback for pre-config runs.
        config = self.state.get("config")
        if config is not None:
            return max(0, config.knobs.reflector_max_test_resynth)
        try:
            return max(0, int(os.getenv("REFLECTOR_MAX_TEST_RESYNTH", "2")))
        except ValueError:
            return 2

    # ------------------------------------------------------------------ #
    # Helpers                                                               #
    # ------------------------------------------------------------------ #

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

    # ------------------------------------------------------------------ #
    # Gateway                                                               #
    # ------------------------------------------------------------------ #

    def _init_gateway(self, db_session) -> ToolGateway:
        sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
        return ToolGateway(db_session=db_session, run_id=self.run_id, sandbox_tools=sandbox_tools)

    # Every check kind whose detail the reflector may want (one Checks field each).
    _CHECK_LABELS = ("unit_tests", "integration_tests", "build", "typecheck", "lint")

    def _test_output(self, db_session) -> str:
        """Full tester output, read from the durable check artifacts (not a host file).

        The blackboard carries only refs (`evidence.checks.<kind>.artifact_ref`); the content
        lives in the `artifacts` table — restart-safe, no side channel. Reads every check
        that actually ran (a check is `None` when the repo didn't opt into it), so the
        reflector sees suite + build + typecheck + lint detail, not just the failure. This is
        ready for the always-on judge (#26); under failure-only the failing gating check is
        what matters. Empty if nothing is available. The output of each artifact is added regardless of whether it passed or failed, 
        so the reflector can see the full context of what was run."""
        evidence = self.state.get("evidence")
        checks = evidence.checks if evidence else None
        if checks is None:
            _log.warning("reflector.no_evidence", run_id=self.run_id,
                         reason="no evidence.checks on state — reflector runs without test output")
            return ""
        blocks: List[str] = []
        for label in self._CHECK_LABELS:
            check_output = getattr(checks, label, None)
            if not (check_output and check_output.artifact_ref):
                _log.info("reflector.no_check_artifact", run_id=self.run_id, label=label,
                          reason="no artifact_ref for this check — reflector runs without test output")
                continue  # check didn't run (no config / not applicable)
            row = db_session.get(DbArtifact, check_output.artifact_ref)
            if row and row.content_text:
                blocks.append(f"===== {label} ({check_output.status}) =====\n{row.content_text}")  # Add the content of the artifact regardless of pass/fail, so the reflector can see the full context of what was run.
        if not blocks:
            _log.warning("reflector.no_test_artifact", run_id=self.run_id,
                         reason="no readable check artifacts — reflector runs without test output")
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ #
    # State result assembly                                                 #
    # ------------------------------------------------------------------ #

    def _build_state_result(
        self,
        raw: Dict[str, Any],
        microtask: Microtask,
        fix_target: str,
    ) -> Dict[str, Any]:
        """Route the diagnosis. `fix_target == "test"` (and budget left) → regenerate the
        synthesized test (NEEDS_TEST + note for the tester); otherwise → coder retry
        (NEEDS_CODE). The microtask stays in flight either way (GH #33)."""
        count = int(self.state.get("test_resynth_count") or 0)
        cap = self._max_test_resynth()

        if fix_target == "test" and count < cap:
            reflections: List[Reflection] = list(raw.get("reflections") or [])
            note = (reflections[-1].fix_instructions if reflections else
                    "The previous synthesized test asserted a wrong expectation — regenerate it.")
            _log.info("reflector.fix_target_test", run_id=self.run_id,
                      microtask_id=microtask.microtask_id, attempt=count + 1, cap=cap)
            return {
                **raw,
                "status": RunStatus.NEEDS_TEST,        # → tester re-synthesizes the test
                "current_microtask_id": microtask.microtask_id,
                "test_rejection_note": note,           # SynthesisPhase reads this
                "test_resynth_count": count + 1,
            }

        if fix_target == "test":
            _log.warning("reflector.test_resynth_exhausted", run_id=self.run_id,
                         microtask_id=microtask.microtask_id, count=count, cap=cap,
                         reason="test-fix budget hit — falling back to the code path")
        return {
            **raw,
            "status": RunStatus.NEEDS_CODE,            # → coder retries with the Reflection
            "current_microtask_id": microtask.microtask_id,
        }

    # ------------------------------------------------------------------ #
    # Persistence                                                           #
    # ------------------------------------------------------------------ #

    def _persist_results(
        self,
        db_session,
        microtask: Microtask,
        result: Dict[str, Any],
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
                microtask_id=microtask.microtask_id,
                content_text=(
                    f"Microtask:         {microtask.microtask_id}\n"
                    f"Decision:          improve\n"
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
                    "decision": "improve",
                    "failure_type": latest.failure_type,
                    "artifact_ref": artifact_ref,
                    "should_store_in_semantic_memory": latest.should_store_in_semantic_memory,
                },
            )

        # Update Run.status (always loops back to the coder).
        run_record = db_session.get(Run, self.run_id)
        old_status = (
            run_record.status if run_record else self.state.get("status", RunStatus.REFLECT).value
        )
        if run_record:
            run_record.status = next_status.value
            run_record.updated_at = now

        EventLogger.log(
            db_session,
            self.run_id,
            self.name,
            EventType.RUN_STATUS_CHANGED,
            {
                "from": old_status,
                "to": next_status.value,
                "reason": f"reflector improve-requested microtask {microtask.microtask_id}",
            },
        )
