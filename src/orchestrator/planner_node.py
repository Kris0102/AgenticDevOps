from datetime import datetime, timezone
from typing import Any, Dict
import os

from langchain_core.tools import BaseTool

from db.db import db
from db.event_log import EventLogger
from db.models import Artifact, MicrotaskRecord, Run
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import BlackboardState, EventType, RunStatus
from orchestrator.run_failure import RunFailureHandler
from agents.Planning.Planner.planner import Planner
from agents.Planning.Planner.planner_tools import PlannerTools
from sandbox.sandbox_tools import DockerSandboxTools
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway


class PlannerRunner:
    """Owns one Planner invocation: tool setup → invoke → persist, mirrors BootStrapper."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.PLANNER.value
        self.sandbox_tools: DockerSandboxTools | None = None

    def invoke(self) -> Dict[str, Any]:
        with db.session_scope() as db_session:
            tools = self._init_tools(db_session)
            try:
                result = self._run_planner(tools)
            except RunExecutionError as e:
                print(f"[Orchestrator] Planner raised RunExecutionError: {e}")
                return RunFailureHandler.handle(db_session, self.run_id, self.name, self.state, e)
            except Exception as e:
                print(f"[Orchestrator] Error caught during Planner execution: {e}")
                raise
            finally:
                self._teardown_sandbox()

            self._persist_results(db_session, result)
            return result

    def _init_tools(self, db_session) -> list[BaseTool]:
        try:
            self.sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
            gateway = ToolGateway(db_session=db_session, run_id=self.run_id, sandbox_tools=self.sandbox_tools)
            planner_tools = PlannerTools(gateway=gateway)
            use_tools = os.getenv("USE_TOOLS", "true").lower() == "true"
            return planner_tools.build_tools() if use_tools else []
        except Exception as e:
            print(f"Warning: Could not initialize planner tools. Fallback to no tools. Error: {e}")
            self.sandbox_tools = None
            return []

    def _run_planner(self, tools: list[BaseTool]) -> Dict[str, Any]:
        planner = Planner(run_id=self.run_id, tools=tools)
        return planner.invoke(self.state)

    def _teardown_sandbox(self) -> None:
        if self.sandbox_tools and self.run_id and os.getenv("TEAR_DOWN_SANDBOX_AFTER_RUN", "false").lower() == "true":
            try:
                self.sandbox_tools.sandbox_destroy(sandbox_id=f"sbox_{self.run_id}")
            except Exception as teardown_e:
                print(f"[Orchestrator] Failed to destroy sandbox: {teardown_e}")

    def _persist_results(self, db_session, result: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        microtasks = result.get("microtasks", [])
        plan = result.get("plan")
        new_status = result.get("status", RunStatus.NEEDS_REVIEW)

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
                acceptance_criteria=list(mt.acceptance_criteria),
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

        # Store plan summary as artifact
        if plan:
            db_session.merge(Artifact(
                artifact_id=f"art_{self.run_id}_plan_v1",
                run_id=self.run_id,
                kind="plan",
                content_text=plan.summary,
                created_at=now,
            ))

        # Emit plan.drafted event
        EventLogger.log(db_session, self.run_id, self.name, EventType.PLAN_DRAFTED, {
            "microtasks_count": len(microtasks),
            "plan_version": 1,
            "plan_summary": plan.summary[:300] if plan else "",
        })
