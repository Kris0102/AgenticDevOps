
from argparse import ArgumentParser
import uuid
import os

from langchain_core.runnables import RunnableConfig

from db.db import db
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.bootstrap_node import BootStrapper
from orchestrator.checkpointer import setup_checkpointer
from orchestrator.orchestrator import graph
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import (
    Constraints,
    Objective,
    Policy,
    Repo,
    RunStatus,
    Trigger,
    create_initial_blackboard_state,
)
from tool_gateway.Roles import Roles

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
    """Owns the CLI run lifecycle: run bootstrap (always), then load-or-create
    graph state and invoke/resume the checkpointed graph.

    Persistence is split across two lifecycles that don't fit one mechanism:
    - bootstrap is an idempotent readiness check that must re-run on EVERY
      invocation (including resumes) — so it lives outside the checkpointed
      graph and is invoked directly, here.
    - planner-onward is multi-step resumable agent work — LangGraph's
      PostgresSaver checkpointer snapshots it automatically per superstep,
      keyed by thread_id=run_id. See docs/decisions/0005-bootstrap-outside-checkpointed-graph.md.
    """

    def __init__(self, args):
        self.args = args
        self.run_id = args.run_id if args.run_id else str(uuid.uuid4())
        self.config: RunnableConfig = {"configurable": {"thread_id": self.run_id}}

    def run(self) -> None:
        check_if_env_files_exist()
        setup_checkpointer()

        checkpoint_values = self._load_checkpoint()
        if checkpoint_values:
            seed_state = self._resume_from_checkpoint(checkpoint_values)
            graph_input = None  # `None` input == "continue from checkpoint"
        else:
            seed_state = self._build_fresh_state()
            graph_input = seed_state

        bootstrap_result = self._run_bootstrap(seed_state)
        graph_input = self._apply_bootstrap_result(checkpoint_values, graph_input, bootstrap_result)

        if bootstrap_result.get("status") == RunStatus.FAILED:
            print(f"[Main] Bootstrap failed for run {self.run_id} — not invoking graph.")
            return

        self._invoke_graph(graph_input)

    def _load_checkpoint(self):
        snapshot = graph.get_state(self.config)
        return snapshot.values or None

    def _apply_bootstrap_result(self, checkpoint_values, graph_input, bootstrap_result):
        if checkpoint_values:
            # Resuming: graph_input is None ("continue from checkpoint"). Push
            # only the changed fields (CLI overrides + bootstrap's sandbox/status)
            # — NOT the whole checkpoint dict. `messages` uses an `operator.add`
            # reducer; re-submitting the existing list would double-append it.
            graph.update_state(self.config, {
                "repo": checkpoint_values["repo"],
                "objective": checkpoint_values["objective"],
                **bootstrap_result,
            })
            return graph_input
        # Fresh run: graph_input IS the seed state — merge bootstrap's result
        # (sandbox/status) into it before it becomes the graph's initial input.
        return {**graph_input, **bootstrap_result}

    def _run_bootstrap(self, state):
        try:
            return BootStrapper(state).invoke()
        except RunExecutionError as e:
            with db.session_scope() as session:
                return RunFailureHandler.handle(session, self.run_id, Roles.BOOTSTRAPPER.value, state, e)

    def _resume_from_checkpoint(self, checkpoint_values):
        print(f"[Main] Resuming run {self.run_id} from checkpoint "
              f"(status={checkpoint_values['status']}).")

        repo = checkpoint_values["repo"]
        objective = checkpoint_values["objective"]
        not_yet_cloned = checkpoint_values["sandbox"].workspace_path == "/workspace"
        no_plan_yet = checkpoint_values["plan"] is None

        repo = self._override_repo_url(repo, not_yet_cloned)
        repo = self._override_base_sha(repo, not_yet_cloned)
        objective = self._override_task_description(objective, no_plan_yet, checkpoint_values)

        checkpoint_values["repo"] = repo
        checkpoint_values["objective"] = objective
        return checkpoint_values

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

    def _invoke_graph(self, graph_input) -> None:
        result = graph.invoke(graph_input, config=self.config)
        print("Graph execution finished. Final state:")
        print(result)


if __name__ == "__main__":
    RunLauncher(args).run()

