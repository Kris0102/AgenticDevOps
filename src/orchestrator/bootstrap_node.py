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
from sandbox.sandbox_tools import DockerSandboxTools, SandboxError
from tool_gateway.Roles import Roles

_log = get_logger(Category.BOOTSTRAP)


class BootStrapper:

    IMAGE = "agentic-sandbox:0.1"
    WORKDIR = "/workspace"

    def __init__(self, blackboard: BlackboardState):
        self.state = blackboard
        self.name = Roles.BOOTSTRAPPER.value
        self.sandbox_tools = DockerSandboxTools(image=self.IMAGE, workdir=self.WORKDIR)
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

        # 3. Clone repo — retry whenever it hasn't actually happened yet (covers both
        # the first run and a resume after a clone failure). workspace_path is still
        # the pre-clone default (WORKDIR) until GitOps.clone_and_checkout succeeds.
        if sandbox_state.workspace_path == self.WORKDIR:
            workspace_path = self._clone_repository()
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

        with db.session_scope() as session:
            existing = session.get(Run, run_id)
            if existing:
                existing.updated_at = datetime.now(timezone.utc)
                _log.info("bootstrap.run_resumed", status=existing.status)
            else:
                session.add(Run(
                    run_id=run_id,
                    repo_url=repo.repo_url,
                    repo_id=repo.repo_id,
                    base_sha=repo.base_sha,
                    status=status.value,
                    task_description=task,
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
            self.sandbox_tools.client.images.get(self.IMAGE)
        except Exception:
            _log.warning("bootstrap.image_missing", image=self.IMAGE,
                         hint="run docker-compose build")

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

    def _clone_repository(self) -> str:
        sandbox_state: Sandbox = self.state.get("sandbox")
        sandbox_id = sandbox_state.sandbox_id
        repo = self.state.get("repo")
        repo_dir = "repo"
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
