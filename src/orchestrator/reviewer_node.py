from datetime import datetime, timezone
from typing import Any, Dict
import os

from langchain_core.tools import BaseTool

from db.db import db
from db.event_log import EventLogger
from db.lessons import LessonStore
from db.models import Artifact, Run
from Exceptions.RunExecutionError import RunExecutionError
from Exceptions.RetriableError import RetriableError
from Exceptions.TokenBudgetExceededError import TokenBudgetExceededError
from db.settings_store import resolve_run_config
from orchestrator.run_failure import RunFailureHandler
from orchestrator.token_budget import record_usage, token_budget_exceeded
from agents.Planning.Reviewer.reviewer_tools import ReviewerTools
from context_engine.engine import ContextEngine
from observability import Category, get_logger
from orchestrator.state import BlackboardState, EventType, RunStatus, Iteration, Constraints
from agents.Planning.Reviewer.reviewer import Reviewer
from sandbox.sandbox_tools import DockerSandboxTools
from tool_gateway.gateway import ToolGateway
from tool_gateway.Roles import Roles

_log = get_logger(Category.REVIEWER)


class ReviewerRunner:
    """Owns one Reviewer invocation: invoke the reviewer and persist the resulting run status."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.REVIEWER.value
        self.sandbox_tools: DockerSandboxTools | None = None

    def _init_tools(self, db_session) -> list[BaseTool]:
        try:
            self.sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
            repo = self.state.get("repo")
            context_engine = ContextEngine()
            gateway = ToolGateway(
                db_session=db_session,
                run_id=self.run_id,
                sandbox_tools=self.sandbox_tools,
                context_engine=context_engine,
                repo_url=repo.repo_url if repo else "",
            )
            reviewer_tools = ReviewerTools(gateway=gateway)
            use_tools = os.getenv("USE_TOOLS", "true").lower() == "true"
            return reviewer_tools.build_tools() if use_tools else []
        except Exception as error:
            _log.warning("reviewer.tools_init_failed", recovery="fallback to no tools", error=str(error))
            self.sandbox_tools = None
            return []

    def invoke(self) -> Dict[str, Any]:
        with db.session_scope() as db_session:
            iteration: Iteration = self.state.get("iteration")
            constraints: Constraints = self.state.get("constraints")

            # Global token-budget guard (ADR 0025) — checked before spending another
            # LLM call. Raises (not RunFailureHandler) so the run STALLS instead of FAILS —
            # recoverable via /continue once the budget is raised, or /fail to close it.
            if token_budget_exceeded(iteration, constraints):
                _log.warning("reviewer.token_budget_exceeded",
                             used=iteration.tokens_used_total, limit=constraints.max_total_tokens)
                raise TokenBudgetExceededError(
                    phase="plan_review", used=iteration.tokens_used_total, limit=constraints.max_total_tokens or -1,
                )

            # Runaway planner↔reviewer guard. Cap the planning loop at
            # max_planner_reviewer_iterations; on exhaustion fail the run (and persist it)
            # rather than re-invoking the LLM forever.
            if iteration.planner_reviewer_iteration_current >= constraints.max_planner_reviewer_iterations:
                _log.warning(
                    "reviewer.max_iterations_exceeded",
                    current=iteration.planner_reviewer_iteration_current,
                    limit=constraints.max_planner_reviewer_iterations,
                    recovery="setting status to FAILED",
                )
                result = {"status": RunStatus.FAILED}
                self._persist_results(db_session, result)
                LessonStore.retract_run(db_session, self.run_id)   # failed run leaves no lessons
                EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_FAILED, {
                    "reason": "planner-reviewer iteration cap exceeded",
                    "iterations": iteration.planner_reviewer_iteration_current,
                    "limit": constraints.max_planner_reviewer_iterations,
                })
                return result

            # Increment via the node-return dict — never mutate state in place
            # (LangGraph checkpoints state from returned deltas).
            next_iteration = iteration.model_copy(update={
                "planner_reviewer_iteration_current": iteration.planner_reviewer_iteration_current + 1,
            })

            try:
                tools = self._init_tools(db_session)
                config = self.state.get("config") or resolve_run_config()
                reviewer_agent = Reviewer(run_id=self.run_id, tools=tools, config=config)
                result = reviewer_agent.invoke(self.state)
            except RetriableError:
                raise   # transient (LLM timeout) → let _drive mark STALLED, not FAILED
            except Exception as error:
                # Convert to a clean FAILED (phase-attributed) instead of unwinding into
                # graph.invoke. The _drive guard is the net; this gives the better label.
                _log.error("reviewer.unexpected_error", error=str(error))
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state,
                    RunExecutionError(phase="plan_review", reason=f"Reviewer crashed: {error}"),
                )

            next_iteration = record_usage(
                db_session, self.run_id, self.name, next_iteration,
                reviewer_agent.token_usage, config.llm.reviewer.model,
            )
            result["iteration"] = next_iteration
            self._persist_results(db_session, result)
            self._emit_review_event(db_session, result)

            # Outcome-validated lesson write (ADR 0018): persist the addressed rejections
            # ONLY when the reviewer APPROVES (status NEEDS_CODE) — that's the confirmation
            # the prior rejections were valid and got fixed. Saving on rejection would store
            # unvalidated (possibly wrong) lessons. Best-effort, never breaks the run.
            if result.get("status") == RunStatus.NEEDS_CODE:
                repo = self.state.get("repo")
                LessonStore.record_plan_lessons(
                    db_session, self.run_id, repo.repo_url if repo else "",
                    result.get("plan"), self.name,
                )

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

        # Persist the review verdict as a queryable artifact row, mirroring the
        # planner's plan artifact. The blackboard already carries a state.Artifact
        # in artifacts_index, but that's checkpoint-only — this makes the decision +
        # notes queryable in the `artifacts` table. Skipped on the cap-FAILED path
        # (no review ran, so no feedback).
        plan = result.get("plan")
        feedback = plan.review_feedback[-1] if plan and plan.review_feedback else None
        if feedback:
            iteration = result.get("iteration")
            n = iteration.planner_reviewer_iteration_current if iteration else 0
            db_session.merge(Artifact(
                artifact_id=f"art_{self.run_id}_review_{n}",
                run_id=self.run_id,
                kind="review",
                content_text=f"[{feedback.decision.value}] {feedback.notes}",
                created_at=now,
            ))

    def _emit_review_event(self, db_session, result: Dict[str, Any]) -> None:
        """Durable domain event for the review verdict — mirrors the planner's
        PLAN_DRAFTED. Decision + notes come off the latest review_feedback the
        Reviewer appended to the plan; without it the audit log has no record a
        review happened, only a status flip."""
        plan = result.get("plan")
        feedback = plan.review_feedback[-1] if plan and plan.review_feedback else None
        status = result.get("status")
        EventLogger.log(db_session, self.run_id, self.name, EventType.PLAN_REVIEWED, {
            "decision": feedback.decision.value if feedback else None,
            "notes": feedback.notes[:300] if feedback else "",
            "next_status": status.value if status else None,
            "iteration": result.get("iteration", {}).planner_reviewer_iteration_current if result.get("iteration") else None,
        })
