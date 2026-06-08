
from argparse import ArgumentParser
import uuid
import os

from db.db import db
from db.run_state import RunStateStore
from orchestrator.orchestrator import graph
from orchestrator.state import (
    Constraints,
    Objective,
    Policy,
    Repo,
    RunStatus,
    Trigger,
    create_initial_blackboard_state,
)

parser = ArgumentParser()
parser.add_argument("--run_id", type=str, required=False, help="Unique identifier for this orchestrator run. If not provided, a random UUID will be generated.")
parser.add_argument("--repo_url", type=str, required=True, help="URL of the repository to operate on.")
parser.add_argument("--base-sha", type=str, required=False, default=None, help="Base commit SHA. Omit for brand-new repos with no commits.")
parser.add_argument("--task_description", type=str, required=True, help="Natural language description of the task to accomplish.")

args = parser.parse_args()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def check_if_env_files_exist():
    if not os.path.exists(os.path.join(BASE_DIR, "../.env")):
        raise FileNotFoundError("The .env file does not exist. Please create it. Check the .env.example file for the required variables.")




class RunLauncher:
    """Owns the CLI run lifecycle: load-or-create state, then invoke the graph."""

    def __init__(self, args):
        self.args = args
        self.run_id = args.run_id if args.run_id else str(uuid.uuid4())

    def run(self) -> None:
        check_if_env_files_exist()

        restored_state = self._load_snapshot()
        if restored_state is not None:
            initial_state = self._resume_from_snapshot(restored_state)
        else:
            initial_state = self._build_fresh_state()

        self._invoke_graph(initial_state)

    def _load_snapshot(self):
        with db.session_scope() as session:
            return RunStateStore.load(session, self.run_id)

    def _resume_from_snapshot(self, restored_state):
        print(f"[Main] Reconstructed blackboard for run {self.run_id} from run_state snapshot "
              f"(status={restored_state['status']}).")

        repo = restored_state["repo"]
        objective = restored_state["objective"]
        not_yet_cloned = restored_state["sandbox"].workspace_path == "/workspace"
        no_plan_yet = restored_state["plan"] is None

        repo = self._override_repo_url(repo, not_yet_cloned)
        repo = self._override_base_sha(repo, not_yet_cloned)
        objective = self._override_task_description(objective, no_plan_yet, restored_state)

        restored_state["repo"] = repo
        restored_state["objective"] = objective
        return restored_state

    def _override_repo_url(self, repo, not_yet_cloned: bool):
        args = self.args
        if args.repo_url and args.repo_url != repo.repo_url:
            if not_yet_cloned:
                print(f"[Main] Repo not yet cloned — applying new --repo_url={args.repo_url!r}")
                return repo.model_copy(update={"repo_url": args.repo_url})
            #TODO color this warning red for better visibility
            print(f"[Main] WARNING: repo already cloned as {repo.repo_url!r} — "
                  f"ignoring --repo_url={args.repo_url!r}. Start a new --run_id to use a different repo.")
        return repo

    def _override_base_sha(self, repo, not_yet_cloned: bool):
        args = self.args
        if args.base_sha and args.base_sha != repo.base_sha:
            if not_yet_cloned:
                print(f"[Main] Repo not yet cloned — applying new --base-sha={args.base_sha!r}")
                return repo.model_copy(update={"base_sha": args.base_sha})
            print(f"[Main] WARNING: repo already cloned at {repo.base_sha!r} — ignoring --base-sha={args.base_sha!r}.")
        return repo

    def _override_task_description(self, objective, no_plan_yet: bool, restored_state):
        args = self.args
        if args.task_description and args.task_description != objective.description:
            if no_plan_yet:
                print("[Main] No plan drafted yet — applying new --task_description")
                return objective.model_copy(update={"description": args.task_description})
            print(f"[Main] WARNING: a plan already exists for the old objective — "
                  f"ignoring --task_description (would invalidate {len(restored_state['microtasks'])} microtask(s)).")
        return objective

    def _build_fresh_state(self):
        objective = Objective(
            title="Implement Feature X",
            description=self.args.task_description,
            definition_of_done=[
                "Code changes are made to implement Feature X.",
                "Unit tests are added to cover the new code.",
                "All tests pass successfully.",
                "Code is reviewed and approved by a human.",
                "Changes are merged into the main branch."
            ],
            non_goals=[
                "Refactoring existing code without implementing Feature X.",
                "Making changes unrelated to Feature X."
            ]
        )

        trigger = Trigger(
            type="cli",
            source_url="",
            actor="user",
            raw_payload_ref=None
        )

        repo = Repo(
            repo_url=self.args.repo_url,
            default_branch=None,
            base_ref="main",
            base_sha=self.args.base_sha,
            workspace_branch="agentic-devops-workspace",
            pr_url=None
        )
        constraints = Constraints(
            time_budget_minutes=60,
            cost_budget_usd=10.0,
            max_total_iterations=5,
            max_microtask_attempts=3,
            allowed_languages=["python", "javascript"],
            forbidden_paths=["src/secrets/"],
            allowed_network="none",
            risk_level="medium"
        )
        policy = Policy(
            require_hitl_for_pr=True,
            require_hitl_for_merge=True,
            require_hitl_for_prod_deploy=True,
            security_scans_required=["bandit", "safety"],
            ci_required=True,
            protected_branches=["main", "master"]
        )

        return create_initial_blackboard_state(
            run_id=self.run_id,
            trigger=trigger,
            repo=repo,
            objective=objective,
            constraints=constraints,
            policy=policy,
            status=RunStatus.NEEDS_BOOTSTRAP,
        )

    def _invoke_graph(self, initial_state) -> None:
        result = graph.invoke(initial_state)
        print("Graph execution finished. Final state:")
        print(result)


if __name__ == "__main__":
    RunLauncher(args).run()

