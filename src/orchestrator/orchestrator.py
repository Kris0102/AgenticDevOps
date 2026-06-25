import os
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

# Allow `python orchestrator/orchestrator.py`-style entry: make `src/` importable
# before the intra-package imports below resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from observability import Category, get_logger
from orchestrator.checkpointer import checkpointer
from orchestrator.coder_node import CoderRunner
from orchestrator.deploy_node import DeployRunner
from orchestrator.hitl import HitlDecision, HitlGate, HitlResume, Phase
from orchestrator.planner_node import PlannerRunner
from orchestrator.reflector_node import ReflectorRunner
from orchestrator.reviewer_node import ReviewerRunner
from orchestrator.state import BlackboardState, RunStatus
from orchestrator.tester_node import TesterRunner

load_dotenv()

_log = get_logger(Category.ORCHESTRATOR)


class OrchestratorGraph:
    """Owns the LangGraph topology: node wrappers, status-driven routers, and
    graph construction. One class so the whole state machine reads top-to-bottom
    in one place; collaborators (PlannerRunner, checkpointer) stay injected.

    bootstrapper is intentionally NOT a graph node — it's an idempotent
    environment-readiness check (sandbox running? repo cloned?) that must
    re-verify on EVERY invocation, including resumes. LangGraph's checkpoint
    resume continues from the last completed node rather than restarting at
    START, so a graph-node bootstrapper would simply be skipped on resume.
    RunLauncher (main.py) calls BootStrapper directly, before graph.invoke().
    See docs/decisions/0005-bootstrap-outside-checkpointed-graph.md.
    """

    def __init__(self) -> None:
        self.checkpointer = checkpointer

    def compile(self):
        return self._build().compile(checkpointer=self.checkpointer)

    # ==========================================
    # 1. Agent Wrapper Nodes
    # ==========================================

    def _planner(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="planner")
        return PlannerRunner(state).invoke()

    def _plan_reviewer(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="plan_reviewer")
        result = ReviewerRunner(state).invoke()
        return HitlGate.apply(Phase.PLAN, result, state)

    def _coder(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="coder")
        return CoderRunner(state).invoke()

    def _tester(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="tester")
        result = TesterRunner(state).invoke()
        # On a PASS the Tester advances (NEEDS_CODE/DONE) and sets _tested_microtask_id;
        # the CODE gate fires here so the human reviews the diff + test output before
        # advancing. A FAIL (REFLECT) has no _tested_microtask_id → passes through ungated.
        if result.get("_tested_microtask_id"):
            return HitlGate.apply(Phase.CODE, result, state)
        return result

    def _reflector(self, state: BlackboardState) -> Dict[str, Any]:
        """Failure-only diagnosis: only reached on a tester FAIL (REFLECT). Diagnoses the
        failure, seeds a Reflection, and routes back to the coder (NEEDS_CODE). It never
        approves or gates — the Tester owns the PASS verdict (ADR 0016)."""
        _log.info("node.enter", node="reflector")
        return ReflectorRunner(state).invoke()

    def _deploy(self, state: BlackboardState) -> Dict[str, Any]:
        """Gate producer for the deploy phase. Emits READY_TO_DEPLOY and funnels it
        through the shared HITL gate — when 'deploy' is in hitl.hitl_checkpoints the
        push pauses for human approval; otherwise it passes straight to deploy_push."""
        _log.info("node.enter", node="deploy")
        return HitlGate.apply(Phase.DEPLOY, {"status": RunStatus.READY_TO_DEPLOY}, state)

    def _deploy_push(self, state: BlackboardState) -> Dict[str, Any]:
        """Actor: stages/commits/pushes the sandbox to a run_id-named branch. Reached
        only after the deploy gate clears (or when no gate is configured)."""
        _log.info("node.enter", node="deploy_push")
        result = DeployRunner(state).invoke()
        return {**result, "status": RunStatus.DONE}
    
    def cleanup(self, state: BlackboardState) -> None:
        """Cleanup after the run completes (success or failure)."""
    


    def _hitl(self, state: BlackboardState) -> Dict[str, Any]:
        """Shared HITL pause point. Suspends the graph via langgraph `interrupt()`
        until a human decision arrives (Command(resume={...})). Reject without a
        note is invalid — re-interrupt rather than route on un-actionable input."""
        _log.info("node.enter", node="hitl", detail="awaiting human decision")
        payload = HitlGate.request_payload(state)
        while True:
            decision = HitlResume.model_validate(interrupt(payload.model_dump(mode="json")))  # the graph is interrupted unitil Command(resume={...}) arrives; the payload is the same as the review context the dashboard fetches, so the human has all the context they need to make a decision and doesn't have to do a separate fetch
            if decision.decision == HitlDecision.REJECT and not decision.note:
                payload = payload.model_copy(update={"error": "reject requires a note — resubmit with --note"})
                continue
            break
        return HitlGate.resolve(state, decision)

    # ==========================================
    # 2. Routing Logic
    # ==========================================

    @staticmethod
    def _status_gate(expected_statuses: set, next_node: str):
        """Conditional-edge factory: proceed to `next_node` only when `status` is
        one of `expected_statuses` for THIS phase transition; any other status —
        FAILED or otherwise inconsistent (e.g. coder mistakenly leaving status at
        NEEDS_PLAN) — halts at END instead of advancing on a status that doesn't
        belong to this edge. Derive the legal next phase from status; don't just
        rule out the one bad value."""
        def _router(state: BlackboardState) -> str:
            if state.get("status") in expected_statuses:
                return next_node
            return END
        return _router

    @staticmethod
    def _plan_review_router(state: BlackboardState) -> str:
        """Planning Loop verdict — expressed as `status`, not a side-channel field:
        NEEDS_HITL_APPROVAL means a 'plan' gate fired (→ hitl); NEEDS_PLAN means
        'revise and resubmit' (back to planner); NEEDS_CODE means 'frozen, proceed'
        (forward to coder). Any other status (FAILED, ...) halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_HITL_APPROVAL:
            return "hitl"
        if status == RunStatus.NEEDS_PLAN:
            return "planner"
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        return END

    @staticmethod
    def _coder_router(state: BlackboardState) -> str:
        """Coder verdict: NEEDS_TEST → tester (no gate at the coder level;
        CODE gate fires at the reflector after the full coder+tester+reflector
        cycle). Any other status halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_TEST:
            return "tester"
        return END

    @staticmethod
    def _tester_router(state: BlackboardState) -> str:
        """Tester verdict (ADR 0016): REFLECT (fail) → reflector; NEEDS_HITL_APPROVAL
        (CODE gate fired on a pass) → hitl; NEEDS_CODE (pass, more microtasks) → coder;
        DONE (pass, final) → deploy. Any other status halts at END."""
        status = state.get("status")
        if status == RunStatus.REFLECT:
            return "reflector"
        if status == RunStatus.NEEDS_HITL_APPROVAL:
            return "hitl"
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        if status == RunStatus.DONE:
            return "deploy"
        return END

    @staticmethod
    def _reflector_router(state: BlackboardState) -> str:
        """Failure-only Reflector: always diagnoses and routes back to the coder
        (NEEDS_CODE). Any other status halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        return END

    @staticmethod
    def _hitl_router(state: BlackboardState) -> str:
        """Post-decision dispatch out of the hitl node.

        NEEDS_PLAN      → planner    (plan gate rejected — revise plan)
        NEEDS_CODE      → coder      (code gate rejected — coder retries microtask)
        READY_TO_DEPLOY → deploy_push (deploy gate approved)
        DONE            → deploy     when CODE gate approved the final microtask;
                        → END        when DEPLOY gate rejected (run ends un-deployed)
        """
        status = state.get("status")
        if status == RunStatus.NEEDS_PLAN:
            return "planner"
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        if status == RunStatus.READY_TO_DEPLOY:
            return "deploy_push"
        if status == RunStatus.DONE:
            # Distinguish CODE gate approve (→ deploy) from DEPLOY gate reject (→ END).
            hitl_state = state.get("hitl")
            if hitl_state and hitl_state.pending_approvals:
                if hitl_state.pending_approvals[-1].type == Phase.CODE.value:
                    return "deploy"
        return END

    @staticmethod
    def _deploy_router(state: BlackboardState) -> str:
        """Deploy gate verdict: NEEDS_HITL_APPROVAL → hitl (await human); READY_TO_DEPLOY
        → deploy_push (gate cleared / not configured). Any other status halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_HITL_APPROVAL:
            return "hitl"
        if status == RunStatus.READY_TO_DEPLOY:
            return "deploy_push"
        return END

    # ==========================================
    # 3. Graph Construction
    # ==========================================

    def _build(self) -> StateGraph:
        builder = StateGraph(BlackboardState)

        builder.add_node("planner", self._planner)
        builder.add_node("plan_reviewer", self._plan_reviewer)
        builder.add_node("coder", self._coder)
        builder.add_node("tester", self._tester)
        builder.add_node("reflector", self._reflector)
        builder.add_node("deploy", self._deploy)
        builder.add_node("deploy_push", self._deploy_push)
        builder.add_node("hitl", self._hitl)

        # bootstrapper runs outside the checkpointed graph (RunLauncher calls it
        # directly, every invocation) — the graph starts unconditionally at planner.
        builder.add_edge(START, "planner")

        # Planning loop: planner → reviewer → (planner | coder), gated by `hitl`
        builder.add_conditional_edges(
            "planner",
            self._status_gate({RunStatus.NEEDS_REVIEW}, "plan_reviewer"),
            {"plan_reviewer": "plan_reviewer", END: END},
        )
        builder.add_conditional_edges(
            "plan_reviewer",
            self._plan_review_router,
            {"hitl": "hitl", "planner": "planner", "coder": "coder", END: END},
        )

        # Execution loop: coder → tester → (pass: CODE gate → coder/deploy | fail: reflector → coder)
        builder.add_conditional_edges(
            "coder",
            self._coder_router,
            {"tester": "tester", END: END},
        )
        builder.add_conditional_edges(
            "tester",
            self._tester_router,
            {"reflector": "reflector", "hitl": "hitl", "coder": "coder", "deploy": "deploy", END: END},
        )
        builder.add_conditional_edges(
            "reflector",
            self._reflector_router,
            {"coder": "coder", END: END},
        )

        # Deploy (terminal): reflector DONE → deploy gate → (hitl) → deploy_push → END.
        builder.add_conditional_edges(
            "deploy",
            self._deploy_router,
            {"hitl": "hitl", "deploy_push": "deploy_push", END: END},
        )
        builder.add_edge("deploy_push", END)

        # Shared HITL gate: suspends via interrupt(), then dispatches on the
        # human-resolved status (approve advances, reject loops back).
        builder.add_conditional_edges(
            "hitl",
            self._hitl_router,
            {"planner": "planner", "coder": "coder", "deploy": "deploy",
             "deploy_push": "deploy_push", END: END},
        )

        return builder


graph = OrchestratorGraph().compile()
