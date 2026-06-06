
from argparse import ArgumentParser
import uuid
import os

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




if __name__ == "__main__":

    check_if_env_files_exist()

    objective = Objective(
        title="Implement Feature X",
        description=args.task_description,
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
        repo_url=args.repo_url,
        default_branch=None,
        base_ref="main",
        base_sha=args.base_sha,
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
    
    
    
    
    initial_state = create_initial_blackboard_state(
        run_id=args.run_id if args.run_id else str(uuid.uuid4()),
        trigger=trigger,
        repo=repo,
        objective=objective,
        constraints=constraints,
        policy=policy,
        status=RunStatus.NEEDS_BOOTSTRAP,
    )

    result = graph.invoke(initial_state)
    print("Graph execution finished. Final state:")
    print(result)


