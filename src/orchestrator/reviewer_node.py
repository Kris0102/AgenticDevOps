from datetime import datetime, timezone
from typing import Any, Dict
import os

from langchain_core.tools import BaseTool

from db.db import db
from db.event_log import EventLogger
from db.models import Run
from agents.Planning.Reviewer.reviewer_tools import ReviewerTools
from context_engine.engine import ContextEngine
from observability import Category, get_logger
from orchestrator.state import BlackboardState, EventType, RunStatus
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
            try:
                tools = self._init_tools(db_session)
                result = Reviewer(run_id=self.run_id, tools=tools).invoke(self.state)
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
