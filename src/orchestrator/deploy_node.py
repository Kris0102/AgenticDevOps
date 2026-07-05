import os
from typing import Any, Dict, List

from db.db import db
from db.event_log import EventLogger
from db.settings_store import SettingsStore
from Exceptions.RunExecutionError import RunExecutionError
from gitops.gitops import GitOps
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import BlackboardState, EventType, Repo, Sandbox
from sandbox.sandbox_tools import DockerSandboxTools, ExecResult, SandboxError
from tool_gateway.Roles import Roles

_log = get_logger(Category.DEPLOYER)


class DeployRunner:
    """Stub GitOps deployer: push the sandbox working tree to the run branch
    (`agent/run-<run_id>`) and (eventually) open a PR.

    Not an LLM agent, so it deliberately BYPASSES the ToolGateway — the gateway's
    job is per-agent tool allowlisting + audit, and there is no allowlist to enforce
    for a fixed deploy step. It drives the GitOps bridge directly, then emits a
    GITOPS_PUSHED event so the push still lands in the durable audit trail.
    """

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.DEPLOYER.value
        self.gitops = GitOps(DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace"))

    def invoke(self) -> Dict[str, Any]:
        sandbox: Sandbox = self.state.get("sandbox")
        repo: Repo = self.state.get("repo")
        if sandbox is None or repo is None:
            _log.warning("deployer.missing_state", has_sandbox=sandbox is not None, has_repo=repo is not None)
            return {}

        branch = GitOps.branch_name(self.run_id)
        token = SettingsStore.github_token()

        with db.session_scope() as db_session:
            # Secret content-scan is a HARD gate BEFORE the push — even a human-approved
            # deploy must not ship a credential. A finding blocks the push and fails the run
            # (safe by default); a scan failure (gitleaks missing) also blocks. See ADR 0019.
            try:
                findings = self.gitops.scan_secrets(sandbox_id=sandbox.sandbox_id)
            except SandboxError as error:
                _log.error("deployer.secret_scan_failed", branch=branch, error=str(error))
                EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_SECRET_BLOCKED, {
                    "branch": branch, "reason": "secret scan could not run", "error": str(error),
                })
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state,
                    RunExecutionError(phase="deploy", reason=f"secret scan failed: {error}"),
                )
            if findings:
                _log.error("deployer.secret_blocked", branch=branch, count=len(findings))
                EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_SECRET_BLOCKED, {
                    "branch": branch, "count": len(findings), "findings": findings,  # redacted (rule/file/line)
                })
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state,
                    RunExecutionError(phase="deploy",
                                      reason=f"push blocked — {len(findings)} secret finding(s) in the working tree"),
                )

            fileset = self.gitops.changed_files(sandbox_id=sandbox.sandbox_id)
            try:
                results = self.gitops.push_branch(
                    sandbox_id=sandbox.sandbox_id,
                    run_id=self.run_id,
                    remote_url=repo.repo_url,
                    token=token,
                    commit_message=f"AgenticDevOps run {self.run_id}",
                )
            except SandboxError as error:
                _log.error("deployer.push_failed", branch=branch, error=str(error))
                raise

            # PR creation is still a stub (GitHub API not wired) — returns None.
            pr_url = self.gitops.create_pr(
                remote_url=repo.repo_url,
                run_id=self.run_id,
                title=f"AgenticDevOps run {self.run_id}",
                body="Automated changes from an AgenticDevOps run.",
            )

            self._emit_push_event(db_session, branch, results, pr_url, fileset)

        _log.info("deployer.pushed", branch=branch, repo_url=repo.repo_url, pr_url=pr_url,
                  files=len(fileset))
        return {}

    def _emit_push_event(self, db_session, branch: str, results: List[ExecResult], pr_url,
                         fileset: List[str]) -> None:
        EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_PUSHED, {
            "branch": branch,
            "steps": len(results),
            "pr_url": pr_url,
            "files": fileset,          # what actually shipped (post-.gitignore) — audit/deploy summary
            "file_count": len(fileset),
        })
