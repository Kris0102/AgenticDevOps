from datetime import datetime, timezone
from typing import Any, Dict
import os

from langchain_core.tools import BaseTool

from db.db import db
from db.settings_store import resolve_run_config
from db.event_log import EventLogger
from db.models import Artifact, MicrotaskRecord, Run
from Exceptions.RunExecutionError import RunExecutionError
from Exceptions.TokenBudgetExceededError import TokenBudgetExceededError
from orchestrator.state import BlackboardState, Constraints, EventType, Iteration, RunStatus
from orchestrator.run_failure import RunFailureHandler
from orchestrator.token_budget import record_usage, token_budget_exceeded
from agents.Planning.Planner.planner import Planner
from agents.Planning.Planner.planner_tools import PlannerTools
from context_engine.engine import ContextEngine
from observability import Category, get_logger
from sandbox.sandbox_tools import DockerSandboxTools
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway

_log = get_logger(Category.PLANNER)


class PlannerRunner:
    """Owns one Planner invocation: tool setup → invoke → persist, mirrors BootStrapper."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.PLANNER.value
        self.sandbox_tools: DockerSandboxTools | None = None

    def invoke(self) -> Dict[str, Any]:
        with db.session_scope() as db_session:
            iteration: Iteration = self.state.get("iteration")
            constraints: Constraints = self.state.get("constraints")

            # Global token-budget guard (ADR 0025) — checked before spending another
            # LLM call. Raises (not RunFailureHandler) so the run STALLS instead of FAILS —
            # recoverable via /continue once the budget is raised, or /fail to close it.
            if token_budget_exceeded(iteration, constraints):
                _log.warning("planner.token_budget_exceeded",
                             used=iteration.tokens_used_total, limit=constraints.max_total_tokens)
                raise TokenBudgetExceededError(
                    phase="planner", used=iteration.tokens_used_total, limit=constraints.max_total_tokens or -1,
                )

            tools = self._init_tools(db_session)
            try:
                result = self._run_planner(tools)
            except RunExecutionError as e:
                _log.error("planner.run_execution_error", error=str(e))
                return RunFailureHandler.handle(db_session, self.run_id, self.name, self.state, e)
            except Exception as e:
                _log.error("planner.unexpected_error", error=str(e))
                raise
            finally:
                self._teardown_sandbox()

            result["iteration"] = record_usage(
                db_session, self.run_id, self.name, iteration, self.token_usage, self._config.llm.model
            )
            self._persist_results(db_session, result)
            return result

    def _init_tools(self, db_session) -> list[BaseTool]:
        try:
            self.sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
            repo = self.state.get("repo")
            # Search-only ContextEngine (no sandbox_tools needed — reads the index, not the repo).
            context_engine = ContextEngine()
            gateway = ToolGateway(
                db_session=db_session,
                run_id=self.run_id,
                sandbox_tools=self.sandbox_tools,
                context_engine=context_engine,
                repo_url=repo.repo_url if repo else "",
            )
            planner_tools = PlannerTools(gateway=gateway)
            use_tools = os.getenv("USE_TOOLS", "true").lower() == "true"
            return planner_tools.build_tools() if use_tools else []
        except Exception as e:
            _log.warning("planner.tools_init_failed", recovery="fallback to no tools", error=str(e))
            self.sandbox_tools = None
            return []

    def _run_planner(self, tools: list[BaseTool]) -> Dict[str, Any]:
        config = self.state.get("config") or resolve_run_config()
        self._config = config
        planner = Planner(run_id=self.run_id, tools=tools, config=config)
        result = planner.invoke(self.state)
        self.token_usage = planner.token_usage
        return result

    def _teardown_sandbox(self) -> None:
        if self.sandbox_tools and self.run_id and os.getenv("TEAR_DOWN_SANDBOX_AFTER_RUN", "false").lower() == "true":
            try:
                self.sandbox_tools.sandbox_destroy(sandbox_id=f"sbox_{self.run_id}")
            except Exception as teardown_e:
                _log.warning("planner.sandbox_teardown_failed", error=str(teardown_e))

    def _persist_results(self, db_session, result: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        microtasks = result.get("microtasks", [])
        plan = result.get("plan")
        new_status = result.get("status", RunStatus.NEEDS_REVIEW)

        # A fresh plan REPLACES the board: drop rows from a superseded draft (reviewer
        # rejected → planner re-planned with new microtask ids) so the dashboard doesn't
        # show both generations interleaved. Ids that survived the re-plan are merged below.
        if microtasks:
            new_ids = [mt.microtask_id for mt in microtasks]
            (db_session.query(MicrotaskRecord)
                .filter(MicrotaskRecord.run_id == self.run_id,
                        MicrotaskRecord.microtask_id.notin_(new_ids))
                .delete(synchronize_session=False))

        # Write / upsert each microtask
        for mt in microtasks:
            db_session.merge(MicrotaskRecord(
                microtask_id=mt.microtask_id,
                run_id=self.run_id,
                idx=mt.idx,
                title=mt.title,
                description=mt.description,
                approach=mt.approach,
                status=mt.status.value,
                attempts=mt.attempts,
                acceptance_criteria=[c.model_dump() for c in mt.acceptance_criteria],
                target_files=list(mt.target_files),
                context_files=list(mt.context_files),
                dependencies=list(mt.dependencies),
                files_touched=list(mt.files_touched),
                test_strategy=mt.test_strategy,
                created_at=now,
                updated_at=now,
            ))

        # Update Run.status in DB
        run_record = db_session.get(Run, self.run_id)
        if run_record:
            run_record.status = new_status.value
            run_record.updated_at = now

        # Store plan summary as artifact — versioned by the planner↔reviewer iteration so a
        # re-drafted plan doesn't overwrite the rejected one (both stay browsable).
        iteration = self.state.get("iteration")
        version = (iteration.planner_reviewer_iteration_current if iteration else 0) + 1
        if plan:
            db_session.merge(Artifact(
                artifact_id=f"art_{self.run_id}_plan_v{version}",
                run_id=self.run_id,
                kind="plan",
                content_text=plan.summary,
                created_at=now,
            ))

        # Emit plan.drafted event
        EventLogger.log(db_session, self.run_id, self.name, EventType.PLAN_DRAFTED, {
            "microtasks_count": len(microtasks),
            "plan_version": version,
            "plan_summary": plan.summary[:300] if plan else "",
        })
