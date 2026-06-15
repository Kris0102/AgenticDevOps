import os
from datetime import datetime, timezone
from typing import Any, Dict

from db.db import db
from db.event_log import EventLogger
from db.models import Run
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.state import BlackboardState, EventType, RunStatus, Sandbox
from sandbox.sandbox_tools import DockerSandboxTools, SandboxError
from tool_gateway.Roles import Roles


def _authenticated_clone_url(repo_url: str) -> str:
    """Inject GITHUB_TOKEN into HTTPS clone URL so git doesn't prompt for credentials."""
    token = os.getenv("GITHUB_TOKEN")
    if token and repo_url.startswith("https://"):
        return repo_url.replace("https://", f"https://{token}@", 1)
    return repo_url


class BootStrapper:

    IMAGE = "agentic-sandbox:0.1"
    WORKDIR = "/workspace"

    def __init__(self, blackboard: BlackboardState):
        self.state = blackboard
        self.name = Roles.BOOTSTRAPPER.value
        self.sandbox_tools = DockerSandboxTools(image=self.IMAGE, workdir=self.WORKDIR)

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
        # the pre-clone default (WORKDIR) until git_clone_and_checkout succeeds.
        if sandbox_state.workspace_path == self.WORKDIR:
            workspace_path = self._clone_repository()
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
            print(f"[Bootstrap] Baseline semantic ingest failed (non-fatal): {e}")
            return 0

    def _upsert_run_record(self):
        repo = self.state.get("repo")
        run_id = self.state.get("run_id")
        status = self.state.get("status")

        with db.session_scope() as session:
            existing = session.get(Run, run_id)
            if existing:
                existing.updated_at = datetime.now(timezone.utc)
                print(f"[Bootstrap] Resuming run {run_id} (status={existing.status})")
            else:
                session.add(Run(
                    run_id=run_id,
                    repo_url=repo.repo_url,
                    base_sha=repo.base_sha,
                    status=status.value,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                ))
                EventLogger.log(session, run_id, self.name, EventType.RUN_CREATED, {
                    "repo_url": repo.repo_url,
                    "base_sha": repo.base_sha,
                    "trigger_status": status.value,
                })
                print(f"[Bootstrap] Created run {run_id}")
    def _ensure_sandbox_running(self):
        run_id = self.state.get("run_id")

        try:
            self.sandbox_tools.client.images.get(self.IMAGE)
        except Exception:
            print(f"Warning: {self.IMAGE} image not found. Run docker-compose build.")

        result = self.sandbox_tools.sandbox_create(run_id=run_id)
        print(f"[Bootstrap] Sandbox ready: {result.sandbox_id} (container={result.container_id[:12]})")

        with db.session_scope() as session:
            EventLogger.log(session, run_id, self.name, EventType.SANDBOX_CREATED, {
                "sandbox_id": result.sandbox_id,
                "container_id": result.container_id[:12],
                "reconnected": result.container_id != result.sandbox_id,
            })

    def _clone_repository(self) -> str:
        sandbox_state: Sandbox = self.state.get("sandbox")
        sandbox_id = sandbox_state.sandbox_id
        repo = self.state.get("repo")
        repo_dir = "repo"
        workspace_path = f"{self.WORKDIR}/{repo_dir}"
        clone_url = _authenticated_clone_url(repo.repo_url)
        print(f"[Bootstrap] Cloning {repo.repo_url} @ {repo.base_sha}...")
        try:
            self.sandbox_tools.git_clone_and_checkout(
                sandbox_id=sandbox_id,
                repo_url=clone_url,
                base_sha=repo.base_sha,
                repo_dir=repo_dir,
            )
            print(f"[Bootstrap] Clone complete → {workspace_path}")
            return workspace_path
        except SandboxError as e:
            print(f"[Bootstrap] Clone failed: {e}")
            print("For private repos, ensure GITHUB_TOKEN is present in environment and has repo access.")
            raise RunExecutionError(phase="bootstrap.clone", reason=str(e))
