from langgraph.graph import StateGraph, START, END
from typing import Dict, Any

from orchestrator.state import BlackboardState, RunStatus

# ==========================================
# 1. Agent Wrapper Nodes
# ==========================================
# These functions act as the Orchestrator's wrappers around the agents.
# They project the relevant part of the Blackboard, invoke the LLM or tool,
# and return dictionary updates that mutate the BlackboardState.

def planner_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Planner...")
    return {"status": RunStatus.NEEDS_PLAN} 

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
# Enforces the Star Topology: Agents do not explicitly map their own next steps.
# The orchestrator reads the blackboard and conditionally routes via LangGraph.

def initial_router(state: BlackboardState) -> str:
    """Determines where a run should pick up from its current Status."""
    status = state.get("status")
    if status in (RunStatus.NEEDS_BOOTSTRAP, RunStatus.NEEDS_PLAN):
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

builder.add_node("planner", planner_node)
builder.add_node("plan_reviewer", plan_reviewer_node)
builder.add_node("coder", coder_node)
builder.add_node("tester", tester_node)

builder.add_conditional_edges(START, initial_router)
builder.add_edge("planner", "plan_reviewer")
builder.add_conditional_edges("plan_reviewer", plan_verification_router)
builder.add_edge("coder", "tester")
builder.add_conditional_edges("tester", execution_verification_router)

graph = builder.compile()

