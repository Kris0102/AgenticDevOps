import os
from datetime import datetime, timezone
from typing import Any, Dict

from db.db import db
from db.event_log import EventLogger
from db.models import Run
from db.repo_store import RepoStore
from gitops.gitops import GitOps
from observability import Category, get_logger
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import BlackboardState, EventType, RunStatus, Sandbox
from sandbox.factory import sandbox_for, spec_from
from sandbox.sandbox_tools import SandboxError
from tool_gateway.Roles import Roles

_log = get_logger(Category.BOOTSTRAP)


class BootStrapper:

    def __init__(self, blackboard: BlackboardState):
        self.state = blackboard
        self.name = Roles.BOOTSTRAPPER.value
        # Container shape comes from the run's spec (defaults = the historic
        # agentic-sandbox:0.1 at /workspace with the repo in repo/).
        self.spec = spec_from(blackboard)
        self.sandbox_tools = sandbox_for(blackboard)
        self.gitops = GitOps(self.sandbox_tools)

    def invoke(self) -> Dict[str, Any]:
        repo = self.state.get("repo")
        run_id = self.state.get("run_id")
        sandbox_state: Sandbox = self.state.get("sandbox")
        sandbox_id = sandbox_state.sandbox_id
        status = self.state.get("status")

        if repo is None:
            raise ValueError("BlackboardState missing 'repo'")

        # 1. Upsert Run in DB — always, regardless of status
        self._upsert_run_record()  # Add run to db if not exists, else update timestamp (heartbeat for run recovery)
        # 2. Ensure sandbox container is running — always, regardless of status
        self._ensure_sandbox_running()

        # 3. Materialize the repo — retry whenever it hasn't actually happened yet (covers
        # both the first run and a resume after a clone failure). workspace_path is still
        # the pre-clone sentinel (the spec's workdir) until the repo is in place.
        if sandbox_state.workspace_path == self.spec.workdir:
            workspace_path, cloned = self._materialize_repo(sandbox_id)
            # A pre-provisioned image arrives with a pristine tree at the base commit.
            # Seeding .gitignore there would DIRTY a tracked file and leak into every diff
            # we extract (benchmark patches especially). Its purpose — keeping deps and
            # secrets out of a push (ADR 0019) — is also moot: deps are already installed
            # into the image, not vendored into the tree, and these runs don't push.
            if cloned:
                self._ensure_gitignore(sandbox_id, workspace_path)
            chunks_ingested = self._ingest_baseline(sandbox_id, workspace_path)

            result = {
                "status": RunStatus.NEEDS_PLAN,
                "sandbox": self._sandbox(sandbox_id, workspace_path),
            }
            with db.session_scope() as session:
                run_record = session.get(Run, run_id)
                if run_record:
                    run_record.status = RunStatus.NEEDS_PLAN.value
                    run_record.updated_at = datetime.now(timezone.utc)
                EventLogger.log(session, run_id, self.name, EventType.REPO_CLONED, {
                    "repo_url": repo.repo_url,
                    "base_sha": repo.base_sha,
                    "workspace_path": workspace_path,
                })
                EventLogger.log(session, run_id, self.name, EventType.SEMANTIC_BASELINE_INGESTED, {
                    "chunks": chunks_ingested,
                    "base_sha": repo.base_sha,
                })
                EventLogger.log(session, run_id, self.name, EventType.RUN_STATUS_CHANGED, {
                    "from": status.value,
                    "to": RunStatus.NEEDS_PLAN.value,
                })

            return result

        # Resume path — repo already cloned (workspace_path != WORKDIR), sandbox
        # healthy, keep current status + preserve existing workspace_path
        return {
            "sandbox": self._sandbox(sandbox_id, sandbox_state.workspace_path),
        }

    @staticmethod
    def _sandbox(sandbox_id: str, workspace_path: str) -> Sandbox:
        return Sandbox(sandbox_id=sandbox_id, workspace_path=workspace_path, status="running")

    def _ingest_baseline(self, sandbox_id: str, workspace_path: str) -> int:
        """Index the freshly cloned repo into semantic memory (namespace=baseline).
        Non-fatal: a missing/failed index degrades to the planner re-reading the
        sandbox directly — it must never break the run."""
        repo = self.state.get("repo")
        try:
            from context_engine.engine import ContextEngine

            engine = ContextEngine(sandbox_tools=self.sandbox_tools)
            return engine.ingest_baseline(
                sandbox_id=sandbox_id,
                repo_url=repo.repo_url,
                base_sha=repo.base_sha,
                workspace_path=workspace_path,
            )
        except Exception as e:
            _log.warning("semantic.baseline.failed", error=str(e))
            return 0

    def _upsert_run_record(self):
        repo = self.state.get("repo")
        run_id = self.state.get("run_id")
        status = self.state.get("status")
        objective = self.state.get("objective")
        task = f"{objective.title}: {objective.description}" if objective else None
        config = self.state.get("config")   # settings hub — record the provider/model used
        constraints = self.state.get("constraints")
        run_name = self.state.get("run_name")  # New field for run name
        deploy_mode = self.state.get("deploy_mode")  # the run's deploy label (ADR 0036)

        with db.session_scope() as session:
            existing = session.get(Run, run_id)
            if existing:
                existing.updated_at = datetime.now(timezone.utc)
                _log.info("bootstrap.run_resumed", status=existing.status)
            else:
                session.add(Run(
                    run_id=run_id,
                    run_name=run_name,
                    repo_url=repo.repo_url,
                    repo_id=repo.repo_id,
                    base_sha=repo.base_sha,
                    status=status.value,
                    task_description=task,
                    deploy_mode=str(deploy_mode) if deploy_mode else None,
                    # Denormalized headline model for the run list — the Coder is the
                    # headline agent (code gen). Per-agent models (GH #71) live on the
                    # checkpointed RunConfig; this column is display-only.
                    llm_provider=config.llm.coder.provider_id if config else None,
                    llm_model=config.llm.coder.model if config else None,
                    max_total_tokens=constraints.max_total_tokens if constraints else None,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                ))
                EventLogger.log(session, run_id, self.name, EventType.RUN_CREATED, {
                    "repo_url": repo.repo_url,
                    "base_sha": repo.base_sha,
                    "trigger_status": status.value,
                })
                _log.info("bootstrap.run_created")
    def _ensure_sandbox_running(self):
        run_id = self.state.get("run_id")

        try:
            self.sandbox_tools.client.images.get(self.spec.image)
        except Exception:
            _log.warning("bootstrap.image_missing", image=self.spec.image,
                         hint="run docker-compose build, or docker pull for a benchmark image")

        result = self.sandbox_tools.sandbox_create(run_id=run_id)
        _log.info("bootstrap.sandbox_ready", sandbox_id=result.sandbox_id,
                  container=result.container_id[:12])

        with db.session_scope() as session:
            EventLogger.log(session, run_id, self.name, EventType.SANDBOX_CREATED, {
                "sandbox_id": result.sandbox_id,
                "container_id": result.container_id[:12],
                "reconnected": result.container_id != result.sandbox_id,
            })

    # Deps (reinstalled by the Tester), caches, build output, secret-bearing files, and the
    # Tester's ephemeral synth-test dir — none of these belong on the pushed run branch. See
    # ADR 0019. `.env` stays in the sandbox (runtime) but is never committed.
    _GITIGNORE_PATTERNS = (
        "node_modules/", "__pycache__/", "*.pyc", ".venv/", "venv/",
        "dist/", "build/", ".env", ".env.*", "*.pem", "*.key", "*.p12",
        "*.keystore", "id_rsa*", ".npmrc", ".agentic_tests/",
    )

    def _ensure_gitignore(self, sandbox_id: str, workspace_path: str) -> None:
        """Seed/merge a `.gitignore` in the cloned repo so `git add` never sweeps in deps,
        caches, secrets, or the Tester's ephemeral synth tests. Idempotent: appends only the
        lines not already present (exact whole-line match), preserving any existing file.
        Best-effort — a failure here must not abort bootstrap (push-time pathspec excludes are
        the backstop)."""
        # Build a POSIX-sh loop; sandbox_exec runs an argv (no shell), so wrap in `sh -c`.
        patterns = " ".join(f"'{p}'" for p in self._GITIGNORE_PATTERNS)
        script = (
            "set -e; touch .gitignore; "
            f"for p in {patterns}; do "
            'grep -qxF "$p" .gitignore || printf "%s\\n" "$p" >> .gitignore; '
            "done"
        )
        res = self.sandbox_tools.sandbox_exec(
            sandbox_id=sandbox_id, cmd=["sh", "-c", script], timeout_s=30, cwd=workspace_path,
        )
        if res.exit_code != 0:
            _log.warning("bootstrap.gitignore_seed_failed", error=res.stderr or res.stdout,
                         recovery="push-time pathspec excludes remain the backstop")
        else:
            _log.info("bootstrap.gitignore_seeded", workspace_path=workspace_path)

    def _materialize_repo(self, sandbox_id: str) -> tuple[str, bool]:
        """Ensure the repo is present in the container. Returns (workspace_path, cloned).

        A PRE-PROVISIONED image already ships the repo (SWE-bench instance images check it
        out at /testbed with its dependency env installed). We detect that by probing for a
        git repo at the spec's `workspace_path` — DERIVED from the container, not declared by
        a flag — and adopt it instead of cloning over the top. Cloning would be both wasteful
        and wrong: the bare base commit lacks the image's build fixes, so the pre-installed
        environment would no longer match the tree.
        """
        expected = self.spec.workspace_path
        if self._is_git_repo(sandbox_id, expected):
            self._adopt_provisioned_repo(sandbox_id, expected)
            return expected, False
        return self._clone_repository(), True

    def _is_git_repo(self, sandbox_id: str, path: str) -> bool:
        """True when `path` is a git work tree inside the container."""
        res = self.sandbox_tools.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "rev-parse", "--is-inside-work-tree"],
            timeout_s=30,
            cwd=path,
        )
        return res.exit_code == 0 and "true" in (res.stdout or "").lower()

    def _adopt_provisioned_repo(self, sandbox_id: str, workspace_path: str) -> None:
        """Adopt a repo the image shipped, verifying it is actually the commit we asked for.

        `base_sha` must be REACHABLE from HEAD rather than equal to it: SWE-bench instance
        images add one commit on top of the base commit (a build fix to pyproject.toml), so an
        equality check would reject every valid image. A base_sha that isn't an ancestor means
        the image doesn't match the instance — fail loudly rather than silently benchmark the
        wrong tree.
        """
        repo = self.state.get("repo")
        # The image's repo is owned by root and checked out by a different build user; git
        # refuses to operate on it as "dubious ownership" without this. Idempotent.
        self.sandbox_tools.sandbox_exec(
            sandbox_id=sandbox_id,
            cmd=["git", "config", "--global", "--add", "safe.directory", workspace_path],
            timeout_s=30,
            cwd=workspace_path,
        )
        head = self.sandbox_tools.sandbox_exec(
            sandbox_id=sandbox_id, cmd=["git", "rev-parse", "HEAD"], timeout_s=30,
            cwd=workspace_path,
        )
        head_sha = (head.stdout or "").strip()

        if repo.base_sha:
            check = self.sandbox_tools.sandbox_exec(
                sandbox_id=sandbox_id,
                cmd=["git", "merge-base", "--is-ancestor", repo.base_sha, "HEAD"],
                timeout_s=30,
                cwd=workspace_path,
            )
            if check.exit_code != 0:
                raise RunExecutionError(
                    phase="bootstrap.adopt",
                    reason=(f"image {self.spec.image} has HEAD {head_sha[:12]}, which does not "
                            f"contain the requested base_sha {repo.base_sha[:12]} — wrong image "
                            f"for this run"),
                )
        _log.info("bootstrap.repo_provisioned", workspace_path=workspace_path,
                  image=self.spec.image, head=head_sha[:12],
                  base_sha=(repo.base_sha or "")[:12], cloned=False)

    def _clone_repository(self) -> str:
        sandbox_state: Sandbox = self.state.get("sandbox")
        sandbox_id = sandbox_state.sandbox_id
        repo = self.state.get("repo")
        repo_dir = self.spec.repo_dir
        _log.info("bootstrap.cloning", repo_url=repo.repo_url, base_ref=repo.base_ref,
                  base_sha=repo.base_sha)
        try:
            workspace_path = self.gitops.clone_and_checkout(
                sandbox_id=sandbox_id,
                repo_url=repo.repo_url,
                base_ref=repo.base_ref,
                base_sha=repo.base_sha,
                token=RepoStore.resolve_token(repo.repo_id),
                repo_dir=repo_dir,
            )
            _log.info("bootstrap.clone_complete", workspace_path=workspace_path)
            return workspace_path
        except SandboxError as e:
            _log.error("bootstrap.clone_failed", error=str(e),
                       hint="for private repos set the GitHub token in Settings")
            raise RunExecutionError(phase="bootstrap.clone", reason=str(e))
