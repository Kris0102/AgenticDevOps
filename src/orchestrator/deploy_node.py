import os
from typing import Any, Dict, List, Optional, Tuple

from db.db import db
from db.event_log import EventLogger
from db.repo_store import RepoNotFoundError, RepoStore
from Exceptions.RunExecutionError import RunExecutionError
from gitops.commit_message import build_commit_message
from gitops.github_api import PrCreationError
from gitops.gitops import GitOps, LocalExportError
from gitops.pr_body import build_pr_body
from observability import Category, get_logger
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import BlackboardState, DeployMode, EventType, Repo, Sandbox
from sandbox.factory import sandbox_for
from sandbox.sandbox_tools import ExecResult, SandboxError
from tool_gateway.Roles import Roles

_log = get_logger(Category.DEPLOYER)


class DeployRunner:
    """GitOps deployer, dispatched on the run's resolved DeployMode (ADR 0036):
    dry_run → nothing leaves the sandbox (audit event only); push/pr → the sandbox
    working tree is pushed to the run's push branch (per-run override › the repo
    registry's › `agent/run-<run_id>`), plus a GitHub PR for 'pr' (ADR 0027);
    local_only → the commit is git-bundled into the user's local clone as that same
    branch, no remote touched.

    Not an LLM agent, so it deliberately BYPASSES the ToolGateway — the gateway's
    job is per-agent tool allowlisting + audit, and there is no allowlist to enforce
    for a fixed deploy step. It drives the GitOps bridge directly, then emits a
    GITOPS_PUSHED event so the push still lands in the durable audit trail.
    """

    def __init__(self, state: BlackboardState):
        self.state = state
        self.run_id = state.get("run_id")
        self.name = Roles.DEPLOYER.value
        self.gitops = GitOps(sandbox_for(self.state))

    def invoke(self) -> Dict[str, Any]:
        sandbox: Sandbox = self.state.get("sandbox")
        repo: Repo = self.state.get("repo")
        if sandbox is None or repo is None:
            _log.warning("deployer.missing_state", has_sandbox=sandbox is not None, has_repo=repo is not None)
            return {}

        mode: DeployMode = self.state.get("deploy_mode") or DeployMode.PUSH
        if mode == DeployMode.DRY_RUN:
            return self._finish_dry_run(sandbox)

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
            # the gate overwrote the seed) › the built default.
            commit_message = (self.state.get("deploy_commit_message") or "").strip() \
                or self._default_commit_message(fileset) # the self._default_commit_message only fires if deploy is ungated, Otherwise the
                #_deploy_message_preview in service.py created the default commit message for the user to view, (Exacrtly the same string,
                # Both use Gitops.build_commit_message)

            if mode == DeployMode.LOCAL_ONLY:
                return self._export_local(db_session, sandbox, branch, commit_message, fileset)

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
                db_session, repo, branch, token, commit_message, fileset, mode,
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
    
    def _finish_dry_run(self, sandbox: Sandbox) -> Dict[str, Any]:
        """DRY_RUN approve: ship nothing, but leave a durable audit event carrying the
        would-have-shipped fileset — the run's diff artifacts stay the full record."""
        fileset = self.gitops.changed_files(sandbox_id=sandbox.sandbox_id)
        with db.session_scope() as db_session:
            EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_DEPLOY_SKIPPED, {
                "mode": DeployMode.DRY_RUN.value,
                "files": fileset,
                "file_count": len(fileset),
            })
        _log.info("deployer.dry_run_finished", files=len(fileset))
        return {}

    def _export_local(self, db_session, sandbox: Sandbox, branch: str,
                      commit_message: str, fileset: List[str]) -> Dict[str, Any]:
        """LOCAL_ONLY approve: bundle the sandbox commit into the user's local clone as
        `branch`. Failure fails the run (the human chose this mode to receive a branch —
        pretending success while delivering nothing would be worse than failing)."""
        local_path = self.state.get("local_export_path")
        if not local_path:
            # Unreachable behind the RunRequest validator; guards direct/legacy callers.
            return RunFailureHandler.handle(
                db_session, self.run_id, self.name, self.state,
                RunExecutionError(phase="deploy",
                                  reason="deploy mode is local_only but no local_export_path is set"),
            )
        try:
            results = self.gitops.export_local_branch(
                sandbox_id=sandbox.sandbox_id,
                run_id=self.run_id,
                local_repo_path=local_path,
                commit_message=commit_message,
                branch=branch,
            )
        except (LocalExportError, SandboxError) as error:
            _log.error("deployer.local_export_failed", branch=branch, path=local_path, error=str(error))
            return RunFailureHandler.handle(
                db_session, self.run_id, self.name, self.state,
                RunExecutionError(phase="deploy", reason=f"local export failed: {error}"),
            )
        EventLogger.log(db_session, self.run_id, self.name, EventType.GITOPS_EXPORTED_LOCAL, {
            "branch": branch,
            "local_repo_path": local_path,
            "steps": len(results),
            "commit_message": commit_message,
            "files": fileset,
            "file_count": len(fileset),
        })
        _log.info("deployer.exported_local", branch=branch, path=local_path, files=len(fileset))
        return {}

    def _default_commit_message(self, fileset: List[str]) -> str:
        """The message the push uses when nobody supplied one. Takes the fileset the caller
        already computed rather than re-shelling into the sandbox for it.

        `Service._hitl_context` builds the SAME string to prefill the deploy gate's box, so
        approving without editing pushes exactly what the gate showed (ADR 0035)."""
        try:
            return build_commit_message(
                run_id=self.run_id,
                objective=self.state.get("objective"),
                microtasks=list(self.state.get("microtasks") or []),
                files=fileset,
            )
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
        mode: DeployMode,
    ) -> Tuple[Optional[str], Optional[str]]:
        """(pr_url, pr_error) — open the auto-PR when this run's mode is 'pr'.
        A PR failure never fails the run: the push already landed, so it degrades to
        a logged error carried in the gitops.pushed payload (ADR 0027).

        Reads the run's OWN resolved mode (per-run RunRequest.deploy_mode › the global
        default, fixed at run creation), not Settings live: run-start already preflighted
        PR rights against exactly this value, so re-reading the global here would let a
        settings flip mid-run attempt a PR the run was never checked for (ADR 0035/0036)."""
        if mode != DeployMode.PR: # no PR should be created!
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
        """(push branch, forbidden branch). Precedence: the run's own `branch_to_push_to`
        (`RunRequest.push_branch`) › the registry's `push_branch_override` › the default
        `agent/run-<run_id>`. The repo's default branch is always forbidden.
        Unregistered/deleted repos fall back to the defaults."""
        forbidden = repo.default_branch
        registry_override: Optional[str] = None
        if repo.repo_id:
            try:
                entry = RepoStore.get(repo.repo_id)
                registry_override = entry.push_branch_override
                forbidden = entry.default_branch or forbidden
            except RepoNotFoundError:
                _log.warning("deployer.repo_entry_missing", repo_id=repo.repo_id,
                             recovery=f"pushing to the default run branch {GitOps.branch_name(self.run_id)}")
        branch = repo.branch_to_push_to or registry_override or GitOps.branch_name(self.run_id)

        # Both overrides are validated where they're set (run creation → 422, add_repo), so
        # this only catches a value that went stale afterwards — e.g. the repo's default
        # branch was renamed onto it. Degrade rather than fail: the run is finished and only
        # its push target is wrong, so land the work on the always-safe run branch instead of
        # throwing it away. Checked outside the repo_id block on purpose — a per-run override
        # on an unregistered repo needs the same guard.
        if forbidden and branch == forbidden:
            _log.warning("deployer.push_target_conflict", branch=branch, forbidden=forbidden,
                         recovery=f"pushing to the default run branch {GitOps.branch_name(self.run_id)}")
            branch = GitOps.branch_name(self.run_id)
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
