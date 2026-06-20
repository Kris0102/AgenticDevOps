from typing import List, Optional

from observability import Category, get_logger
from sandbox.sandbox_tools import DockerSandboxTools, ExecResult, SandboxError

_log = get_logger(Category.DEPLOYER)


class GitOps:
    """Bridge to the git remote (GitHub) — the GitHub integration boundary from
    ARCHITECTURE §4.4. Owns branch naming, commit, push, and (eventually) PR
    creation, delegating raw git execution to the sandbox.

    Scope today: stage/commit/push a run branch. `create_pr` is a stub until the
    GitHub App / token wiring lands; webhooks + CI relay + merge are later milestones.
    Kept separate from DockerSandboxTools (pure sandbox exec) so the GitHub-API
    surface has an obvious home and the token stays in one place. See ADR 0012.
    """

    BRANCH_PREFIX = "agent/run-"

    def __init__(self, sandbox_tools: DockerSandboxTools):
        self.sandbox = sandbox_tools

    @classmethod
    def branch_name(cls, run_id: str) -> str:
        return f"{cls.BRANCH_PREFIX}{run_id}"

    @staticmethod
    def _authenticated_url(repo_url: str, token: str) -> str:
        """Inject the token into an HTTPS URL so git never prompts for credentials.
        Centralized here so the token lives only in the GitOps boundary (spec §4.4)."""
        if token and repo_url.startswith("https://"):
            return repo_url.replace("https://", f"https://{token}@", 1)
        return repo_url

    def clone_and_checkout(
        self,
        *,
        sandbox_id: str,
        repo_url: str,
        base_sha: Optional[str] = None,
        token: str = "",
        repo_dir: str = "repo",
        timeout_s: int = 600,
    ) -> str:
        """Clone the repo into the sandbox and optionally checkout `base_sha`; returns
        the workspace path. `base_sha=None` (brand-new/empty repo) → clone only."""
        clone_url = self._authenticated_url(repo_url, token)
        workspace_path = f"{self.sandbox.workdir}/{repo_dir}"

        clone = self.sandbox.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "clone", clone_url, repo_dir],
            timeout_s=timeout_s,
            cwd=self.sandbox.workdir,
        )
        if clone.exit_code != 0:
            raise SandboxError(f"git clone failed: {clone.stderr or clone.stdout}")

        if not base_sha:
            _log.info("gitops.clone_no_checkout", reason="no base_sha")
            return workspace_path

        checkout = self.sandbox.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "checkout", base_sha],
            timeout_s=timeout_s,
            cwd=workspace_path,
        )
        if checkout.exit_code != 0:
            raise SandboxError(f"git checkout failed: {checkout.stderr or checkout.stdout}")
        _log.info("gitops.cloned", workspace_path=workspace_path, base_sha=base_sha)
        return workspace_path

    def push_branch(
        self,
        *,
        sandbox_id: str,
        run_id: str,
        remote_url: str,
        token: str = "",
        repo_dir: str = "repo",
        commit_message: Optional[str] = None,
        author_name: str = "AgenticDevOps",
        author_email: str = "agentic-devops@users.noreply.github.com",
    ) -> List[ExecResult]:
        """Stage → commit → push the sandbox working tree to the run branch
        (`agent/run-<run_id>`).

        The Coder edits the working tree on a detached HEAD (base_sha), so changes
        are uncommitted — this stages and commits them first. Idempotent: safe to
        re-run (remote re-pointed, "nothing to commit" tolerated).
        """
        branch = self.branch_name(run_id)
        repo_cwd = f"{self.sandbox.workdir}/{repo_dir}"
        results: List[ExecResult] = []

        def _run(cmd: List[str], *, timeout_s: int = 60, allow_fail: bool = False) -> ExecResult:
            res = self.sandbox.sandbox_exec(sandbox_id=sandbox_id, cmd=cmd, timeout_s=timeout_s, cwd=repo_cwd)
            results.append(res)
            if res.exit_code != 0 and not allow_fail:
                raise SandboxError(f"{' '.join(cmd)} failed: {res.stderr or res.stdout}")
            return res

        # Commit identity — a fresh container has none, so `git commit` would fail.
        _run(["git", "config", "user.email", author_email])
        _run(["git", "config", "user.name", author_name])

        # Stage everything, then commit (tolerate an empty working tree).
        _run(["git", "add", "-A"])
        message = commit_message or f"AgenticDevOps run {run_id}"
        commit = _run(["git", "commit", "-m", message], allow_fail=True)
        if commit.exit_code != 0:
            blob = f"{commit.stdout} {commit.stderr}".lower()
            if "nothing to commit" in blob:
                _log.info("gitops.nothing_to_commit", branch=branch)
                return results
            raise SandboxError(f"git commit failed: {commit.stderr or commit.stdout}")

        # Authenticated remote, re-run-safe: add, or re-point if it already exists.
        authed_url = self._authenticated_url(remote_url, token)
        if _run(["git", "remote", "add", "auth-origin", authed_url], timeout_s=30, allow_fail=True).exit_code != 0:
            _run(["git", "remote", "set-url", "auth-origin", authed_url], timeout_s=30)

        # Fully-qualify the destination ref: HEAD is detached (checked out at
        # base_sha), so git can't infer branch-vs-tag and refuses a bare name.
        _run(["git", "push", "auth-origin", f"HEAD:refs/heads/{branch}"])
        _log.info("gitops.pushed", branch=branch, repo_dir=repo_dir)
        return results

    def create_pr(
        self,
        *,
        remote_url: str,
        run_id: str,
        title: str,
        body: str,
        base: str = "main",
    ) -> Optional[str]:
        """STUB — open/update a PR for the run branch via the GitHub API.

        Not yet implemented (needs the GitHub App / token wiring from
        ARCHITECTURE §4.4). Returns the PR URL once implemented; None for now.
        """
        branch = self.branch_name(run_id)
        _log.warning(
            "gitops.create_pr_not_implemented",
            branch=branch,
            base=base,
            hint="open the PR manually, or implement the GitHub API call here",
        )
        return None
