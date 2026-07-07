from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.db import db
from db.event_log import EventLogger
from db.models import Artifact, MicrotaskRecord, Run
from Exceptions.RunExecutionError import RunExecutionError
from Exceptions.TokenBudgetExceededError import TokenBudgetExceededError
from db.settings_store import resolve_run_config
from orchestrator.state import (
    BlackboardState,
    Constraints,
    EventType,
    Iteration,
    Microtask,
    MicrotaskStatus,
    RunStatus,
)
from orchestrator.run_failure import RunFailureHandler
from orchestrator.token_budget import record_usage, token_budget_exceeded
from agents.Implementing.Coder.coder import Coder
from agents.Implementing.Coder.coder_tools import CoderTools, to_sandbox_path
from agents.Implementing.Coder.prompt.coder_prompt import _MISSING_FILE_SENTINEL
from context_engine.engine import ContextEngine
from observability import Category, get_logger
from sandbox.Tools import Tools
from sandbox.sandbox_tools import DockerSandboxTools
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway

_log = get_logger(Category.CODER)
_FINISHED = {MicrotaskStatus.COMPLETED, MicrotaskStatus.DONE}


class CoderRunner:
    """Owns one Coder invocation: microtask selection → stop-limit check →
    grounding pre-fetch → invoke → persist. Mirrors PlannerRunner."""

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.CODER.value

    def invoke(self) -> Dict[str, Any]:
        with db.session_scope() as db_session:
            iteration: Iteration = self.state.get("iteration")
            constraints: Constraints = self.state.get("constraints")

            # Global token-budget guard (ADR 0025) — checked before spending another
            # LLM call. Raises (not RunFailureHandler) so the run STALLS instead of FAILS —
            # recoverable via /continue once the budget is raised, or /fail to close it.
            if token_budget_exceeded(iteration, constraints):
                _log.warning("coder.token_budget_exceeded",
                             used=iteration.tokens_used_total, limit=constraints.max_total_tokens)
                raise TokenBudgetExceededError(
                    phase="code", used=iteration.tokens_used_total, limit=constraints.max_total_tokens,
                )

            try:
                microtask = self._select_microtask()
                if microtask is None:
                    return self._finish_run(db_session)
                self._check_stop_limit(microtask)

                gateway = self._init_gateway(db_session)
                coder_tools = CoderTools(gateway=gateway)
                file_contents = self._gather_file_context(gateway, microtask)

                config = self.state.get("config") or resolve_run_config()
                coder = Coder(run_id=self.run_id, coder_tools=coder_tools, config=config)
                result = coder.invoke(self.state, microtask, file_contents)
            except RunExecutionError as e:
                _log.error("coder.run_execution_error", error=str(e))
                return RunFailureHandler.handle(db_session, self.run_id, self.name, self.state, e)
            except Exception as e:
                _log.error("coder.unexpected_error", error=str(e))
                raise

            # internal hand-off from Coder — not a BlackboardState channel, must
            # never reach the graph's node-result dict
            diff_text = result.pop("_diff_text", "")
            result["iteration"] = record_usage(
                db_session, self.run_id, self.name, iteration, coder.token_usage, config.llm.model
            )
            self._persist_results(db_session, result, microtask, diff_text)
            return result

    def _select_microtask(self) -> Optional[Microtask]:
        """Reuse the in-flight microtask (retry path from reflector) or pick the
        first PENDING one by idx whose dependencies are all finished. Derived from
        state — no extra bookkeeping field."""
        microtasks: List[Microtask] = list(self.state.get("microtasks") or [])
        by_id = {mt.microtask_id: mt for mt in microtasks}

        current_id = self.state.get("current_microtask_id")
        if current_id and current_id in by_id and by_id[current_id].status not in _FINISHED:
            return by_id[current_id]

        for mt in sorted(microtasks, key=lambda m: m.idx):
            if mt.status != MicrotaskStatus.PENDING:
                continue
            deps_done = all(
                by_id[dep].status in _FINISHED for dep in mt.dependencies if dep in by_id # dependency is a mircostask id !
            )
            if deps_done:
                return mt
        return None

    def _check_stop_limit(self, microtask: Microtask) -> None:
        """Minimal stop-condition enforcement (audit #1, coder half): refuse a
        microtask that already burned its attempt budget — kills the runaway
        coder↔reflector loop. Router-level max_total_iterations is a separate TODO."""
        constraints = self.state.get("constraints")
        limit = microtask.max_attempts or (constraints.max_microtask_attempts if constraints else None)
        if limit is not None and microtask.attempts >= limit:
            raise RunExecutionError(
                phase="code",
                reason=(
                    f"Microtask '{microtask.microtask_id}' exhausted its attempt "
                    f"budget ({microtask.attempts}/{limit})."
                ),
            )

    def _init_gateway(self, db_session) -> ToolGateway:
        sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
        return ToolGateway(db_session=db_session, run_id=self.run_id, sandbox_tools=sandbox_tools)

    def _gather_file_context(self, gateway: ToolGateway, microtask: Microtask) -> Dict[str, str]:
        """Pre-read every target/context file through the gateway (each read is
        audit-logged) so the Coder's prompt carries real contents — grounding is
        guaranteed, not left to optional tool calls."""
        contents: Dict[str, str] = {}
        for path in list(microtask.target_files) + list(microtask.context_files):
            if path in contents:
                continue
            result = gateway.execute_tool(
                actor_role=Roles.CODER,
                tool_name=Tools.FS_READ_FILE,
                args={"path": to_sandbox_path(path)},
            )
            if "error" in result:
                contents[path] = _MISSING_FILE_SENTINEL
            else:
                contents[path] = result.get("content", "")
        return contents

    def _finish_run(self, db_session) -> Dict[str, Any]:
        """No pending microtask left. Provisional DONE until the real Tester owns
        the microtask loop (same caveat as HitlGate.GATES[TEST])."""
        _log.info("coder.run_done", reason="no pending microtasks", provisional=True)
        now = datetime.now(timezone.utc)
        run_record = db_session.get(Run, self.run_id)
        old_status = run_record.status if run_record else "unknown"
        if run_record:
            run_record.status = RunStatus.DONE.value
            run_record.updated_at = now
        EventLogger.log(db_session, self.run_id, self.name, EventType.RUN_STATUS_CHANGED, {
            "from": old_status,
            "to": RunStatus.DONE.value,
            "reason": "no pending microtasks left for the coder",
        })
        return {"status": RunStatus.DONE, "current_microtask_id": None}

    def _persist_results(
        self,
        db_session,
        result: Dict[str, Any],
        microtask: Microtask,
        diff_text: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        updated = next(
            (mt for mt in result.get("microtasks", []) if mt.microtask_id == microtask.microtask_id),
            microtask,
        )

        db_session.merge(MicrotaskRecord(
            microtask_id=updated.microtask_id,
            run_id=self.run_id,
            idx=updated.idx,
            title=updated.title,
            description=updated.description,
            approach=updated.approach,
            status=updated.status.value,
            attempts=updated.attempts,
            acceptance_criteria=[c.model_dump() for c in updated.acceptance_criteria],
            target_files=list(updated.target_files),
            context_files=list(updated.context_files),
            dependencies=list(updated.dependencies),
            files_touched=list(updated.files_touched),
            test_strategy=updated.test_strategy,
            created_at=now,
            updated_at=now,
        ))

        evidence = result.get("evidence")
        diff_artifact_ref = (
            evidence.latest_diff.diff_artifact_ref if evidence and evidence.latest_diff else None
        )
        if diff_artifact_ref:
            db_session.merge(Artifact(
                artifact_id=diff_artifact_ref,
                run_id=self.run_id,
                kind="diff",
                microtask_id=updated.microtask_id,
                content_text=diff_text,
                created_at=now,
            ))

        new_status = result.get("status", RunStatus.NEEDS_TEST)
        run_record = db_session.get(Run, self.run_id)
        if run_record:
            run_record.status = new_status.value
            run_record.updated_at = now

        EventLogger.log(db_session, self.run_id, self.name, EventType.MICROTASK_UPDATED, {
            "microtask_id": updated.microtask_id,
            "attempts": updated.attempts,
            "files_touched": list(updated.files_touched),
            "diff_artifact": diff_artifact_ref,
        })

        if new_status == RunStatus.NEEDS_TEST and updated.files_touched:
            synced = self._sync_workspace(list(updated.files_touched))
            if synced:
                EventLogger.log(db_session, self.run_id, self.name, EventType.SEMANTIC_WORKSPACE_SYNCED, {
                    "files": list(updated.files_touched),
                    "chunks": synced,
                })

    def _sync_workspace(self, files_touched: List[str]) -> int:
        """Re-index the just-edited files into the workspace namespace so semantic
        search reflects in-run changes. Non-fatal — never break the execution loop."""
        repo = self.state.get("repo")
        sandbox = self.state.get("sandbox")
        if not repo or not sandbox:
            return 0
        try:
            sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
            engine = ContextEngine(sandbox_tools=sandbox_tools)
            return engine.sync_workspace(
                sandbox_id=sandbox.sandbox_id,
                run_id=self.run_id,
                repo_url=repo.repo_url,
                workspace_path=sandbox.workspace_path,
                changed_files=files_touched,
            )
        except Exception as e:
            _log.warning("semantic.workspace_sync_failed", error=str(e))
            return 0
