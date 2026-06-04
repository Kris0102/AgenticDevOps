from typing import Any, Dict

from dotenv import load_dotenv
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from orchestrator.state import BlackboardState, RunStatus
import os
import sys
from db.models import Run

# Ensure required imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tool_gateway.gateway import ToolGateway
from sandbox.sandbox_tools import DockerSandboxTools
from db.db import db
from agents.Planning.planner import Planner
from agents.Planning.planner_tools import PlannerTools

load_dotenv()

# ==========================================
# 1. Agent Wrapper Nodes
# ==========================================
# These functions act as the Orchestrator's wrappers around the agents.
# They project the relevant part of the Blackboard, invoke the LLM or tool,
# and return dictionary updates that mutate the BlackboardState.



def planner_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Planner...")
    run_id = state.get("run_id")
    sandbox_tools: DockerSandboxTools | None = None
    tools: list[BaseTool] = []

    with db.session_scope() as db_session:
        try:
            sandbox_tools = DockerSandboxTools(
                image="agentic-sandbox:0.1", workdir="/workspace"
            )
            try:
                sandbox_tools.client.images.get("agentic-sandbox:0.1")
            except Exception:
                print(
                    "Warning: agentic-sandbox:0.1 not found. "
                    "Did you run docker-compose build?"
                )

            sandbox_tools.sandbox_create(run_id=run_id)
            gateway = ToolGateway(
                db_session=db_session,
                run_id=run_id,
                sandbox_tools=sandbox_tools,
            )
            planner_tools = PlannerTools(gateway=gateway)
            use_tools = os.getenv("USE_TOOLS", "true").lower() == "true"
            tools = planner_tools.build_tools() if use_tools else []
        except Exception as e:
            print(
                "Warning: Could not initialize Docker Sandbox. "
                f"Fallback to no tools. Error: {e}"
            )
            tools = []
            sandbox_tools = None

        try:
            planner = Planner(tools=tools)
            return planner.invoke(state)
        except Exception as e:
            print(f"[Orchestrator] Error caught during Planner execution: {e}")
            raise
        finally:
            if sandbox_tools and run_id:
                try:
                    print(
                        f"[Orchestrator] Tearing down sandbox container "
                        f"for run {run_id}..."
                    )
                    sandbox_tools.sandbox_destroy(sandbox_id=f"sbox_{run_id}")
                except Exception as teardown_e:
                    print(
                        f"[Orchestrator] Failed to destroy sandbox container: "
                        f"{teardown_e}"
                    )

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
    repo = state.get("repo")
    if repo is None:
        raise ValueError("Blackboard state is missing 'repo'")

    with db.session_scope() as db_session:
        run = Run(
            run_id=state.get("run_id"),
            repo_url=repo.repo_url,
            base_sha=repo.base_sha,
            status=state.get("status"),
            created_at=state.get("created_at"),
            updated_at=state.get("updated_at"),
        )
        db_session.add(run)

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
#builder.add_edge("planner", "plan_reviewer")
builder.add_edge("planner", END)
# builder.add_edge("planner", "plan_reviewer")
builder.add_conditional_edges("plan_reviewer", plan_verification_router)
builder.add_edge("coder", "tester")
builder.add_conditional_edges("tester", execution_verification_router)

graph = builder.compile()

