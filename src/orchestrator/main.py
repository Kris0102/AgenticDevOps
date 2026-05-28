
from argparse import ArgumentParser
from orchestrator.state import BlackboardState
from orchestrator.orchestrator import graph

parser = ArgumentParser()
parser.add_argument("--run_id", type=str, required=True, help="Unique identifier for this orchestrator run.")
parser.add_argument("--repo_url", type=str, required=True, help="URL of the repository to operate on.")
parser.add_argument("--base-sha", type=str, required=True, help="Base commit SHA to compare against.")
parser.add_argument("--task_description", type=str, required=True, help="Natural language description of the task to accomplish.")

args = parser.parse_args()




if __name__ == "__main__":
    initial_state = BlackboardState(
        run_id=args.run_id,
        trigger={
            "type": "cli",
            "source_url": None,
            "actor": "user",
            "raw_payload_ref": None
        },
        repo={
            "repo_url": args.repo_url,
            "default_branch": None,
            "base_ref": None,
            "base_sha": args.base_sha,
            "workspace_branch": None,
            "pr_url": None
        },
        sandbox=None,
        objective={
            "title": None,
            "description": args.task_description,
            "definition_of_done": [],
            "non_goals": []
        },
        constraints={
            "time_budget_minutes": 60,
            "cost_budget_usd": 10.0,
            "max_total_iterations": 5,
            "max_microtask_attempts": 3,
            "allowed_languages": ["python", "javascript"],
            "forbidden_paths": ["src/secrets/"],
            "allowed_network": "none",
            "risk_level": "medium"
        },
        policy={
            "require_hitl_for_pr": True,
            "require_hitl_for_merge": True,
            "require_hitl_for_prod_deploy": True,
            "security_scans_required": ["bandit", "safety"]
        },
        run_status="NEEDS_BOOTSTRAP"
    )

    graph.run(initial_state)


