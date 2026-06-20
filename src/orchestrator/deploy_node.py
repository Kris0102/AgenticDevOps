import os
from typing import Any, Dict, List

from db.db import db
from db.event_log import EventLogger
from gitops.gitops import GitOps
from observability import Category, get_logger
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
        token = os.getenv("GITHUB_TOKEN", "")

        with db.session_scope() as db_session:
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

            self._emit_push_event(db_session, branch, results, pr_url)

        _log.info("deployer.pushed", branch=branch, repo_url=repo.repo_url, pr_url=pr_url)
        return {}

    def _emit_push_event(self, db_session, branch: str, results: List[ExecResult], pr_url) -> None:
        EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_PUSHED, {
            "branch": branch,
            "steps": len(results),
            "pr_url": pr_url,
        })
