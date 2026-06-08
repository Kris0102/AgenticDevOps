import os
from datetime import datetime, timezone
from typing import Any, Dict

from db.db import db
from db.event_log import EventLogger
from db.models import Run
from db.run_state import RunStateStore
from orchestrator.state import BlackboardState, EventType, RunStatus, Sandbox, merge_state
from sandbox.sandbox_tools import DockerSandboxTools, SandboxError
from tool_gateway.Roles import Roles


def _authenticated_clone_url(repo_url: str) -> str:
    """Inject GITHUB_TOKEN into HTTPS clone URL so git doesn't prompt for credentials."""
    token = os.getenv("GITHUB_TOKEN")
    if token and repo_url.startswith("https://"):
        return repo_url.replace("https://", f"https://{token}@", 1)
    return repo_url


class BootStrapper:

    def __init__(self, blackboard: BlackboardState):
        self.state = blackboard
        self.name = Roles.BOOTSTRAPPER.value

    def invoke(self) -> Dict[str, Any]:
        run_id = self.state.get("run_id")
        repo = self.state.get("repo")
        sandbox_state: Sandbox = self.state.get("sandbox")
        sandbox_id = sandbox_state.sandbox_id
        status = self.state.get("status")

        if repo is None:
            raise ValueError("BlackboardState missing 'repo'")

        # 1. Upsert Run in DB — always, regardless of status
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
                    status=str(status),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                ))
                EventLogger.log(session, run_id, self.name, EventType.RUN_CREATED, {
                    "repo_url": repo.repo_url,
                    "base_sha": repo.base_sha,
                    "trigger_status": str(status),
                })
                print(f"[Bootstrap] Created run {run_id}")

        # 2. Ensure sandbox container is running — always, regardless of status
        sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
        try:
            sandbox_tools.client.images.get("agentic-sandbox:0.1")
        except Exception:
            print("Warning: agentic-sandbox:0.1 image not found. Run docker-compose build.")

        result = sandbox_tools.sandbox_create(run_id=run_id)
        print(f"[Bootstrap] Sandbox ready: {result.sandbox_id} (container={result.container_id[:12]})")

        with db.session_scope() as session:
            EventLogger.log(session, run_id, self.name, EventType.SANDBOX_CREATED, {
                "sandbox_id": result.sandbox_id,
                "container_id": result.container_id[:12],
                "reconnected": result.container_id != result.sandbox_id,
            })

        # 3. Clone repo — only on first run
        if status == RunStatus.NEEDS_BOOTSTRAP:
            repo_dir = "repo"
            workspace_path = f"/workspace/{repo_dir}"
            clone_url = _authenticated_clone_url(repo.repo_url)
            print(f"[Bootstrap] Cloning {repo.repo_url} @ {repo.base_sha}...")
            try:
                sandbox_tools.git_clone_and_checkout(
                    sandbox_id=sandbox_id,
                    repo_url=clone_url,
                    base_sha=repo.base_sha,
                    repo_dir=repo_dir,
                )
                print(f"[Bootstrap] Clone complete → {workspace_path}")
            except SandboxError as e:
                print(f"[Bootstrap] Clone failed: {e}")
                print("For private repos, ensure GITHUB_TOKEN is present in environment and has repo access.")
                result = {
                    "status": RunStatus.FAILED,
                    "error_logs": [str(e)],
                }
                with db.session_scope() as session:
                    run_record = session.get(Run, run_id)
                    if run_record:
                        run_record.status = str(RunStatus.FAILED)
                        run_record.updated_at = datetime.now(timezone.utc)
                    EventLogger.log(session, run_id, self.name, EventType.RUN_STATUS_CHANGED, {
                        "from": str(status),
                        "to": str(RunStatus.FAILED),
                        "reason": str(e),
                    })
                    RunStateStore.save(session, run_id, merge_state(self.state, result))
                return result

            result = {
                "status": RunStatus.NEEDS_PLAN,
                "sandbox": Sandbox(
                    sandbox_id=sandbox_id,
                    workspace_path=workspace_path,
                    status="running",
                ),
            }
            with db.session_scope() as session:
                run_record = session.get(Run, run_id)
                if run_record:
                    run_record.status = str(RunStatus.NEEDS_PLAN)
                    run_record.updated_at = datetime.now(timezone.utc)
                EventLogger.log(session, run_id, self.name, EventType.REPO_CLONED, {
                    "repo_url": repo.repo_url,
                    "base_sha": repo.base_sha,
                    "workspace_path": workspace_path,
                })
                EventLogger.log(session, run_id, self.name, EventType.RUN_STATUS_CHANGED, {
                    "from": str(RunStatus.NEEDS_BOOTSTRAP),
                    "to": str(RunStatus.NEEDS_PLAN),
                })
                RunStateStore.save(session, run_id, merge_state(self.state, result))

            return result

        # Resume path — sandbox is healthy, keep current status + workspace_path
        # if state is not NEEDS_BOOTSTRAP, we must be resuming an existing run, so preserve existing workspace_path
        result = {
            "sandbox": Sandbox(
                sandbox_id=sandbox_id,
                workspace_path=sandbox_state.workspace_path,
                status="running",
            ),
        }
        with db.session_scope() as session:
            RunStateStore.save(session, run_id, merge_state(self.state, result))
        return result
