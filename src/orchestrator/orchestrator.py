from typing import Any, Dict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
import os
import sys

# Ensure required imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from orchestrator.state import BlackboardState, RunStatus
from orchestrator.checkpointer import checkpointer
from orchestrator.planner_node import PlannerRunner

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
        print("[Orchestrator] Invoking Planner...")
        return PlannerRunner(state).invoke()

    # Stubs — once these become real agents, give each its own <phase>_node.py
    # Runner class (mirroring PlannerRunner) and call it here. Real runners should
    # raise RunExecutionError on unrecoverable failure to get RunFailureHandler's
    # status/event bookkeeping for free (state snapshotting is automatic via the
    # checkpointer — no manual RunStateStore.save needed).

    def _plan_reviewer(self, state: BlackboardState) -> Dict[str, Any]:
        print("[Orchestrator] Invoking Plan Reviewer (Critic)...")
        return {}

    def _coder(self, state: BlackboardState) -> Dict[str, Any]:
        print("[Orchestrator] Invoking Coder...")
        return {"status": RunStatus.NEEDS_TEST}

    def _tester(self, state: BlackboardState) -> Dict[str, Any]:
        print("[Orchestrator] Invoking Tester / Sandbox Runner...")
        return {}

    def _reflector(self, state: BlackboardState) -> Dict[str, Any]:
        print("[Orchestrator] Invoking Reflector...")
        return {}

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
        NEEDS_PLAN means 'revise and resubmit' (back to planner), NEEDS_CODE means
        'frozen, proceed' (forward to coder). Any other status (FAILED,
        NEEDS_HITL_APPROVAL, ...) halts at END rather than guessing a direction."""
        status = state.get("status")
        if status == RunStatus.NEEDS_PLAN:
            return "planner"
        if status == RunStatus.NEEDS_CODE:
            return "coder"
        return END

    @staticmethod
    def _tester_router(state: BlackboardState) -> str:
        """Execution (Grounded) Loop verdict. Tester only ever reports grounded
        results — it never talks to Coder directly (no blind retries off an
        undiagnosed failure). REFLECT means 'tests failed, diagnose root cause
        first' → reflector, which is the SOLE path back to coder. Any other status
        (DONE, FAILED, NEEDS_HITL_APPROVAL, ...) halts at END."""
        if state.get("status") == RunStatus.REFLECT:
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

        # bootstrapper runs outside the checkpointed graph (RunLauncher calls it
        # directly, every invocation) — the graph starts unconditionally at planner.
        builder.add_edge(START, "planner")

        # Planning loop: planner → reviewer → (planner | coder)
        builder.add_conditional_edges(
            "planner",
            self._status_gate({RunStatus.NEEDS_REVIEW}, "plan_reviewer"),
            {"plan_reviewer": "plan_reviewer", END: END},
        )
        builder.add_conditional_edges(
            "plan_reviewer",
            self._plan_review_router,
            {"planner": "planner", "coder": "coder", END: END},
        )

        # Execution loop: coder → tester → reflector → coder
        builder.add_conditional_edges(
            "coder",
            self._status_gate({RunStatus.NEEDS_TEST}, "tester"),
            {"tester": "tester", END: END},
        )
        builder.add_conditional_edges(
            "tester",
            self._tester_router,
            {"reflector": "reflector", END: END},
        )
        builder.add_conditional_edges(
            "reflector",
            self._status_gate({RunStatus.NEEDS_CODE}, "coder"),
            {"coder": "coder", END: END},
        )

        return builder


graph = OrchestratorGraph().compile()
