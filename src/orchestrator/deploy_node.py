import os
from typing import Any, Dict, List, Optional, Tuple

from db.db import db
from db.event_log import EventLogger
from db.repo_store import RepoNotFoundError, RepoStore
from db.settings_store import SettingsStore
from Exceptions.RunExecutionError import RunExecutionError
from gitops.github_api import PrCreationError
from gitops.gitops import GitOps
from gitops.pr_body import build_pr_body
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import BlackboardState, EventType, Repo, Sandbox
from sandbox.sandbox_tools import DockerSandboxTools, ExecResult, SandboxError
from tool_gateway.Roles import Roles

_log = get_logger(Category.DEPLOYER)


class DeployRunner:
    """GitOps deployer: push the sandbox working tree to the run branch
    (`agent/run-<run_id>`) and, when `defaults.auto_create_pr` is on, open a PR
    (ADR 0027).

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

        branch, forbidden_branch = self._resolve_push_target(repo)
        token = RepoStore.resolve_token(repo.repo_id)

        with db.session_scope() as db_session:
            # Never land on the repo's default branch — even a misconfigured override
            # must not turn a deploy into a push to main. GitOps re-checks (backstop).
            if forbidden_branch and branch == forbidden_branch:
                _log.error("deployer.push_target_forbidden", branch=branch)
                return RunFailureHandler.handle(
                    db_session, self.run_id, self.name, self.state,
                    RunExecutionError(phase="deploy",
                                      reason=f"push target '{branch}' is the repo's default branch — refused"),
                )
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
            # Gate-entered message › per-run message (both on deploy_commit_message,
            # the gate overwrote the seed) › the default. ADR 0027.
            commit_message = (self.state.get("deploy_commit_message") or "").strip() \
                or self._create_detailed_commit_message()
            try:
                results = self.gitops.push_branch(
                    sandbox_id=sandbox.sandbox_id,
                    run_id=self.run_id,
                    remote_url=repo.repo_url,
                    token=token,
                    commit_message=commit_message,
                    branch=branch,
                    forbidden_branch=forbidden_branch,
                )
            except SandboxError as error:
                _log.error("deployer.push_failed", branch=branch, error=str(error))
                raise

            pr_url, pr_error = self._maybe_create_pr(
                db_session, repo, branch, token, commit_message, fileset,
            )

            self._emit_push_event(db_session, branch, results, pr_url, fileset,
                                  commit_message, pr_error)

        _log.info("deployer.pushed", branch=branch, repo_url=repo.repo_url, pr_url=pr_url,
                  files=len(fileset))
        # PR URL is the run's result on the repo — carry it on state via the node
        # return dict (never mutate in place).
        if pr_url:
            return {"repo": repo.model_copy(update={"pr_url": pr_url})}
        return {}
    
    def _create_detailed_commit_message(self) -> str:
        """Commit message from the run objective + microtask titles + changed files.
        Falls back to a minimal default on any error."""
        try:
            objective = self.state.get("objective")
            microtasks = self.state.get("microtasks") or []
            fileset = self.gitops.changed_files(sandbox_id=self.state.get("sandbox").sandbox_id)

            subject = (objective.title.strip() if objective and objective.title.strip()
                    else f"AgenticDevOps run {self.run_id}")[:72]

            body: List[str] = []
            if objective:
                body.append(f"Objective:\n{objective.description}")
            if microtasks:
                lines = "\n".join(f"- {mt.title}" for mt in sorted(microtasks, key=lambda m: m.idx))
                body.append(f"Microtasks:\n{lines}")
            if fileset:
                shown = fileset[:50]
                more = len(fileset) - len(shown)
                files = "\n".join(f"- {f}" for f in shown) + (f"\n- …and {more} more" if more > 0 else "")
                body.append(f"Files touched:\n{files}")
            body.append(f"Run: {self.run_id}")
            return f"{subject}\n\n" + "\n\n".join(body)
        except Exception as e:
            _log.error("deployer.commit_message_failed", message="reverting to default", error=str(e))
            return f"AgenticDevOps run {self.run_id}"

    def _maybe_create_pr(
        self,
        db_session,
        repo: Repo,
        branch: str,
        token: str,
        commit_message: str,
        fileset: List[str],
    ) -> Tuple[Optional[str], Optional[str]]:
        """(pr_url, pr_error) — open the auto-PR when the global setting is on.
        A PR failure never fails the run: the push already landed, so it degrades to
        a logged error carried in the gitops.pushed payload (ADR 0027)."""
        if not SettingsStore.get().defaults.auto_create_pr:
            return None, None
        if not fileset:
            # Nothing shipped → "No commits between base and head" is guaranteed.
            _log.info("deployer.pr_skipped", reason="empty fileset — nothing to propose")
            return None, "nothing shipped — PR skipped"

        base = repo.base_ref or repo.default_branch or "main"
        title = commit_message.splitlines()[0][:100]
        evidence = self.state.get("evidence")
        body = build_pr_body(
            run_id=self.run_id,
            objective=self.state.get("objective"),
            microtasks=self.state.get("microtasks") or [],
            files=fileset,
            diff_summary=evidence.latest_diff.summary if evidence and evidence.latest_diff else None,
            branch=branch,
            base=base,
        )
        try:
            pr_url = self.gitops.create_pr(
                remote_url=repo.repo_url, branch=branch, base=base,
                title=title, body=body, token=token,
            )
        except PrCreationError as error:
            _log.warning("deployer.pr_create_failed", branch=branch, base=base, error=str(error))
            return None, str(error)
        EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_PR_CREATED, {
            "branch": branch, "base": base, "pr_url": pr_url, "title": title,
        })
        return pr_url, None

    def _resolve_push_target(self, repo: Repo) -> tuple:
        """(push branch, forbidden branch). Registry `push_branch_override` wins over
        the default `agent/run-<run_id>`; the repo's default branch is always
        forbidden. Unregistered/deleted repos fall back to the defaults."""
        branch = GitOps.branch_name(self.run_id)
        forbidden = repo.default_branch
        if repo.repo_id:
            try:
                entry = RepoStore.get(repo.repo_id)
                branch = entry.push_branch_override or branch
                forbidden = entry.default_branch or forbidden
            except RepoNotFoundError:
                _log.warning("deployer.repo_entry_missing", repo_id=repo.repo_id,
                             recovery="pushing to the default run branch")
        return branch, forbidden

    def _emit_push_event(self, db_session, branch: str, results: List[ExecResult], pr_url,
                         fileset: List[str], commit_message: str,
                         pr_error: Optional[str]) -> None:
        EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_PUSHED, {
            "branch": branch,
            "steps": len(results),
            "commit_message": commit_message,
            "pr_url": pr_url,
            "pr_error": pr_error,      # why the auto-PR didn't land (None when fine/off)
            "files": fileset,          # what actually shipped (post-.gitignore) — audit/deploy summary
            "file_count": len(fileset),
        })
