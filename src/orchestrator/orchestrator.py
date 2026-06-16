from typing import Any, Dict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
import os
import sys

# Ensure required imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from observability import Category, get_logger
from orchestrator.state import BlackboardState, RunStatus
from orchestrator.checkpointer import checkpointer

_log = get_logger(Category.ORCHESTRATOR)
from orchestrator.hitl import HitlDecision, HitlGate, HitlResume, Phase
from orchestrator.coder_node import CoderRunner
from orchestrator.planner_node import PlannerRunner
from orchestrator.tester_node import TesterRunner

load_dotenv()


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

    # Stubs — once these become real agents, give each its own <phase>_node.py
    # Runner class (mirroring PlannerRunner) and call it here. Real runners should
    # raise RunExecutionError on unrecoverable failure to get RunFailureHandler's
    # status/event bookkeeping for free (state snapshotting is automatic via the
    # checkpointer — no manual RunStateStore.save needed).

    def _plan_reviewer(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="plan_reviewer")
        return HitlGate.apply(Phase.PLAN, {"status": RunStatus.NEEDS_CODE}, state)

    def _coder(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="coder")
        result = CoderRunner(state).invoke()
        return HitlGate.apply(Phase.CODE, result, state)

    def _tester(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="tester")
        result = TesterRunner(state).invoke()
        return HitlGate.apply(Phase.TEST, result, state)

    def _reflector(self, state: BlackboardState) -> Dict[str, Any]:
        _log.info("node.enter", node="reflector")
        return {}

    def _hitl(self, state: BlackboardState) -> Dict[str, Any]:
        """Shared HITL pause point. Suspends the graph via langgraph `interrupt()`
        until a human decision arrives (Command(resume={...})). Reject without a
        note is invalid — re-interrupt rather than route on un-actionable input."""
        _log.info("node.enter", node="hitl", detail="awaiting human decision")
        payload = HitlGate.request_payload(state)
        while True:
            decision = HitlResume.model_validate(interrupt(payload.model_dump(mode="json")))
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
        """Coder verdict: NEEDS_HITL_APPROVAL means a 'code' gate fired (→ hitl);
        NEEDS_TEST means 'verify this diff' (→ tester). Any other status halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_HITL_APPROVAL:
            return "hitl"
        if status == RunStatus.NEEDS_TEST:
            return "tester"
        return END

    @staticmethod
    def _tester_router(state: BlackboardState) -> str:
        """Execution (Grounded) Loop verdict. NEEDS_HITL_APPROVAL means a 'test' gate
        fired (→ hitl). REFLECT means 'tests failed, diagnose root cause first' →
        reflector, the SOLE path back to coder after a FAILURE (no blind retries off
        an undiagnosed failure). NEEDS_CODE means 'this microtask passed, more
        PENDING microtasks remain' → straight back to coder for the next one (not a
        retry of a failure, so reflector isn't involved). Any other status (DONE,
        FAILED, ...) halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_HITL_APPROVAL:
            return "hitl"
        if status == RunStatus.REFLECT:
            return "reflector"
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        return END

    @staticmethod
    def _hitl_router(state: BlackboardState) -> str:
        """Post-decision dispatch out of the hitl node — `resolve()` has already set
        the loop-back/forward status: approve advances (NEEDS_CODE→coder,
        NEEDS_TEST→tester), reject loops back (NEEDS_PLAN→planner, REFLECT→reflector).
        DONE / anything else halts at END."""
        status = state.get("status")
        if status == RunStatus.NEEDS_PLAN:
            return "planner"
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        if status == RunStatus.NEEDS_TEST:
            return "tester"
        if status == RunStatus.REFLECT:
            return "reflector"
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
        builder.add_node("hitl", self._hitl)

        # bootstrapper runs outside the checkpointed graph (RunLauncher calls it
        # directly, every invocation) — the graph starts unconditionally at planner.
        builder.add_edge(START, "planner")

        # Planning loop: planner → reviewer → (planner | coder), gated by `hitl`
        builder.add_conditional_edges(
            "planner",
            self._status_gate({RunStatus.NEEDS_REVIEW}, "plan_reviewer"),
            {"plan_reviewer": "plan_reviewer", END: END}, # all possible states after planner node 
        )
        builder.add_conditional_edges(
            "plan_reviewer",
            self._plan_review_router,
            {"hitl": "hitl", "planner": "planner", "coder": "coder", END: END}, # all possible states after plan reviewer node
        )

        # Execution loop: coder → tester → reflector → coder, gated by `hitl`
        builder.add_conditional_edges(
            "coder",
            self._coder_router,
            {"hitl": "hitl", "tester": "tester", END: END},
        )
        builder.add_conditional_edges(
            "tester",
            self._tester_router,
            {"hitl": "hitl", "reflector": "reflector", "coder": "coder", END: END},  # right now it is possible to route back from the tester to the coder, but in the future,
            #once we have a real tester that can distinguish between "fixable" failures (e.g. flake8 errors) and more complex ones (e.g. failed unit tests), we may want to route only the former back to coder and the latter to reflector for diagnosis before retrying. In that case, the tester_router would need to be updated to route to reflector on certain failure states instead of coder, and this edge would need to be updated accordingly.
        )
        builder.add_conditional_edges(
            "reflector",
            self._status_gate({RunStatus.NEEDS_CODE}, "coder"),
            {"coder": "coder", END: END},
        )

        # Shared HITL gate: suspends via interrupt(), then dispatches on the
        # human-resolved status (approve advances, reject loops back).
        builder.add_conditional_edges(
            "hitl",
            self._hitl_router,
            {"planner": "planner", "coder": "coder", "tester": "tester",
             "reflector": "reflector", END: END},
        )

        return builder


graph = OrchestratorGraph().compile()
