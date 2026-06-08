from typing import Any, Dict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from orchestrator.state import BlackboardState, RunStatus
import os
import sys

# Ensure required imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tool_gateway.Roles import Roles
from db.db import db
from Exceptions.RunExecutionError import RunExecutionError
from orchestrator.bootstrap_node import BootStrapper
from orchestrator.planner_node import PlannerRunner
from orchestrator.run_failure import RunFailureHandler

load_dotenv()

# ==========================================
# 1. Agent Wrapper Nodes
# ==========================================


def bootstrapper_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Bootstrapper...")
    try:
        return BootStrapper(state).invoke()
    except RunExecutionError as e:
        with db.session_scope() as session:
            return RunFailureHandler.handle(session, state["run_id"], Roles.BOOTSTRAPPER.value, state, e)


def planner_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Planner...")
    return PlannerRunner(state).invoke()


# Stubs — once these become real agents, raise RunExecutionError on unrecoverable
# failure and wrap their invocation like bootstrapper_node/planner_node to get
# RunFailureHandler's status/event/snapshot bookkeeping for free.

def plan_reviewer_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Plan Reviewer (Critic)...")
    return {}


def coder_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Coder...")
    return {"status": RunStatus.NEEDS_TEST}


def tester_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Tester / Sandbox Runner...")
    return {}


# ==========================================
# 2. Orchestrator Routing Logic
# ==========================================

def phase_router(state: BlackboardState) -> str:
    """Routes to the correct agent after bootstrapper_node runs."""
    status = state.get("status")
    if status == RunStatus.NEEDS_PLAN:
        return "planner"
    elif status == RunStatus.NEEDS_CODE:
        return "coder"
    elif status == RunStatus.NEEDS_TEST:
        return "tester"
    return END


def plan_verification_router(state: BlackboardState) -> str:
    """Hierarchical Verification Loop 1: Planning Loop"""
    plan = state.get("plan")
    if plan and getattr(plan, "review_feedback", None) and plan.review_feedback[-1].get("decision") == "approve":
        return "coder"
    return "planner"


def execution_verification_router(state: BlackboardState) -> str:
    """Hierarchical Verification Loop 2: Execution (Grounded) Loop"""
    evidence = state.get("evidence")
    if evidence and getattr(evidence, "checks", None) and evidence.checks.unit_tests and evidence.checks.unit_tests.exit_code == 0:
        return END
    return "coder"


# ==========================================
# 3. LangGraph Construction
# ==========================================

builder = StateGraph(BlackboardState)

builder.add_node("bootstrapper", bootstrapper_node)
builder.add_node("planner", planner_node)
builder.add_node("plan_reviewer", plan_reviewer_node)
builder.add_node("coder", coder_node)
builder.add_node("tester", tester_node)

# bootstrapper always runs first — unconditional
builder.add_edge(START, "bootstrapper")
builder.add_conditional_edges("bootstrapper", phase_router)

builder.add_edge("planner", END)
builder.add_conditional_edges("plan_reviewer", plan_verification_router)
builder.add_edge("coder", "tester")
builder.add_conditional_edges("tester", execution_verification_router)

graph = builder.compile()
