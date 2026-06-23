from argparse import ArgumentParser
import uuid
import os
from typing import Dict, Any, Optional
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from db.db import db
from observability import Category, bind_run, clear_context, configure_logging, get_logger
from orchestrator.hitl import HitlDecision, HitlResume
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
import structlog
from dotenv import load_dotenv

load_dotenv() # load .env file

_log = get_logger(Category.ORCHESTRATOR)

parser = ArgumentParser()
parser.add_argument("--run_id", type=str, required=False, help="Unique identifier for this orchestrator run. If not provided, a random UUID will be generated.")
parser.add_argument("--repo_url", type=str, required=True, help="URL of the repository to operate on.")
parser.add_argument("--base-sha", type=str, required=False, default=None, help="Base commit SHA. Omit for brand-new repos with no commits.")
parser.add_argument("--task_description", type=str, required=True, help="Natural language description of the task to accomplish.")
parser.add_argument("--hitl-checkpoints", type=str, default="", help="Comma-separated phases that pause for human approval, e.g. 'plan,code'. Empty = no mid-run gates.")
parser.add_argument("--approve", action="store_true", help="Resume a run paused at a HITL checkpoint by approving it.")
parser.add_argument("--reject", action="store_true", help="Resume a run paused at a HITL checkpoint by rejecting it (requires --note).")
parser.add_argument("--note", type=str, default=None, help="Reviewer feedback. Required with --reject; optional with --approve.")

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
        structlog.contextvars.bind_contextvars(run_id=self.run_id)
        self.logger = structlog.get_logger()

    def run(self) -> None:
        configure_logging()           # reads LOG_LEVEL / LOG_LEVEL_<CAT> / LOG_JSON from env
        bind_run(self.run_id)         # run_id now auto-attached to every log line this run
        log = get_logger(Category.ORCHESTRATOR)
        log.info("run.start", repo_url=self.args.repo_url, base_sha=self.args.base_sha)
        try:
            self._run()
        finally:
            log.info("run.end")
            clear_context()

    def _run(self) -> None:
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
            _log.error("run.bootstrap_failed", detail="not invoking graph")
            return

        # If the run is paused at a HITL checkpoint and a decision flag was passed,
        # resume with a Command instead of continuing the normal flow.
        hitl_resume = self._build_hitl_resume()
        if hitl_resume is not None:
            graph_input = hitl_resume

        self._invoke_graph(graph_input)

    def _build_hitl_resume(self):
        # Scripted/crash-recovery path: user re-invoked the process after it stopped
        # (crash, Ctrl+C, timeout) while paused at a HITL gate. Builds a Command to
        # inject as graph_input before _invoke_graph, bypassing the interactive loop.
        # Normal in-process HITL is handled by _prompt_hitl_decision() inside _invoke_graph.
        if not (self.args.approve or self.args.reject):
            return None
        if self.args.approve and self.args.reject:
            raise SystemExit("[Main] Pass only one of --approve / --reject.")
        if self.args.reject and not self.args.note:
            raise SystemExit("[Main] --reject requires --note explaining why.")

        if not self._has_pending_interrupt():
            _log.warning("hitl.not_paused", detail="ignoring --approve/--reject")
            return None

        decision = HitlResume(
            decision=HitlDecision.APPROVE if self.args.approve else HitlDecision.REJECT,
            note=self.args.note,
            reviewer="cli-user",
        )
        _log.info("hitl.resuming", decision=decision.decision.value)
        return Command(resume=decision.model_dump(mode="json"))

    def _has_pending_interrupt(self) -> bool:
        snapshot = graph.get_state(self.config)
        return any(task.interrupts for task in snapshot.tasks)

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
        _log.info("run.resume_from_checkpoint", status=str(checkpoint_values['status']))

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
                _log.info("run.override.repo_url", repo_url=args.repo_url)
                return repo.model_copy(update={"repo_url": args.repo_url})
            _log.warning("run.override.repo_url_ignored", cloned_as=repo.repo_url,
                         requested=args.repo_url, hint="start a new --run_id for a different repo")
        return repo

    def _override_base_sha(self, repo, not_yet_cloned: bool):
        args = self.args
        if args.base_sha and args.base_sha != repo.base_sha:
            if not_yet_cloned:
                _log.info("run.override.base_sha", base_sha=args.base_sha)
                return repo.model_copy(update={"base_sha": args.base_sha})
            _log.warning("run.override.base_sha_ignored", cloned_at=repo.base_sha, requested=args.base_sha)
        return repo

    def _override_task_description(self, objective, no_plan_yet: bool, restored_state):
        args = self.args
        if args.task_description and args.task_description != objective.description:
            if no_plan_yet:
                _log.info("run.override.task_description")
                return objective.model_copy(update={"description": args.task_description})
            _log.warning("run.override.task_description_ignored",
                         microtasks_affected=len(restored_state['microtasks']))
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
        hitl_checkpoints = [p.strip() for p in self.args.hitl_checkpoints.split(",") if p.strip()]
        policy = Policy(
            require_hitl_for_pr=True,
            require_hitl_for_merge=True,
            require_hitl_for_prod_deploy=True,
            hitl_checkpoints=hitl_checkpoints,
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
        # Drives the graph to completion, handling HITL checkpoints interactively.
        # If the run was already paused (scripted path), graph_input is a Command(resume=...)
        # and the loop runs once then continues. If the run hits a gate mid-flight,
        # _prompt_hitl_decision() prompts the user in-process and resumes without re-invoking.
        current_input = graph_input
        while True:
            graph.invoke(current_input, config=self.config)
            if not self._has_pending_interrupt():
                _log.info("run.complete")
                break
            resume_cmd = self._prompt_hitl_decision()
            if resume_cmd is None:
                break
            current_input = resume_cmd

    def _prompt_hitl_decision(self) -> "Optional[Command]":
        # Interactive half of the HITL mechanism. The graph suspended at interrupt() in
        # orchestrator._hitl; we read the interrupt payload from the checkpoint, prompt
        # the user, and return Command(resume=HitlResume.model_dump()) which LangGraph
        # delivers back to interrupt()'s return value when graph.invoke() is called again.
        snapshot = graph.get_state(self.config)
        payload: dict = {}
        for task in snapshot.tasks:
            for intr in task.interrupts:
                if isinstance(intr.value, dict):
                    payload = intr.value
                    break

        phase = payload.get("phase", "?")
        prompt_text = payload.get("prompt", "")
        plan_line = next((l for l in prompt_text.splitlines() if "Plan file:" in l), None)

        print("\n" + "=" * 60)
        print(f"  HITL checkpoint — phase: {phase}")
        if plan_line:
            print(f"  {plan_line}")
        print("=" * 60)

        while True:
            raw = input("  Decision [approve/reject]: ").strip().lower()
            if raw not in ("approve", "reject"):
                print("  Type 'approve' or 'reject'.")
                continue
            if raw == "reject":
                note = input("  Feedback (required): ").strip()
                if not note:
                    print("  Feedback is required for reject.")
                    continue
            else:
                note_input = input("  Note (optional, press enter to skip): ").strip()
                note = note_input or None
            break

        decision = HitlResume(
            decision=HitlDecision.APPROVE if raw == "approve" else HitlDecision.REJECT,
            note=note,
            reviewer="cli-user",
        )
        print("=" * 60 + "\n")
        return Command(resume=decision.model_dump(mode="json"))


if __name__ == "__main__":
    RunLauncher(args).run()

