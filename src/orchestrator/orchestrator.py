from datetime import datetime, timezone
from typing import Any, Dict

from dotenv import load_dotenv
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from orchestrator.state import BlackboardState, RunStatus
import os
import sys

# Ensure required imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tool_gateway.Roles import Roles
from tool_gateway.gateway import ToolGateway
from sandbox.sandbox_tools import DockerSandboxTools
from db.db import db
from db.event_log import EventLogger
from db.models import Artifact, MicrotaskRecord, Run
from agents.Planning.Planner.planner import Planner
from agents.Planning.Planner.planner_tools import PlannerTools
from orchestrator.bootstrap_node import BootStrapper

load_dotenv()

# ==========================================
# 1. Agent Wrapper Nodes
# ==========================================


def bootstrapper_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Bootstrapper...")
    return BootStrapper(state).invoke()


def planner_node(state: BlackboardState) -> Dict[str, Any]:
    print("[Orchestrator] Invoking Planner...")
    run_id = state.get("run_id")
    sandbox_tools: DockerSandboxTools | None = None
    tools: list[BaseTool] = []

    with db.session_scope() as db_session:
        try:
            sandbox_tools = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
            gateway = ToolGateway(db_session=db_session, run_id=run_id, sandbox_tools=sandbox_tools)
            planner_tools = PlannerTools(gateway=gateway)
            use_tools = os.getenv("USE_TOOLS", "true").lower() == "true"
            tools = planner_tools.build_tools() if use_tools else []
        except Exception as e:
            print(f"Warning: Could not initialize planner tools. Fallback to no tools. Error: {e}")
            tools = []
            sandbox_tools = None

        try:
            planner = Planner(run_id=run_id, tools=tools)
            result = planner.invoke(state)
        except Exception as e:
            print(f"[Orchestrator] Error caught during Planner execution: {e}")
            raise
        finally:
            if sandbox_tools and run_id and os.getenv("TEAR_DOWN_SANDBOX_AFTER_RUN", "false").lower() == "true":
                try:
                    sandbox_tools.sandbox_destroy(sandbox_id=f"sbox_{run_id}")
                except Exception as teardown_e:
                    print(f"[Orchestrator] Failed to destroy sandbox: {teardown_e}")

        # --- Persist planner output to DB ---
        now = datetime.now(timezone.utc)
        microtasks = result.get("microtasks", [])
        plan = result.get("plan")
        new_status = result.get("status", RunStatus.NEEDS_REVIEW)

        # Write / upsert each microtask
        for mt in microtasks:
            db_session.merge(MicrotaskRecord(
                microtask_id=mt.microtask_id,
                run_id=run_id,
                idx=mt.idx,
                title=mt.title,
                description=mt.description,
                approach=mt.approach,
                status=str(mt.status),
                attempts=mt.attempts,
                acceptance_criteria=list(mt.acceptance_criteria),
                target_files=list(mt.target_files),
                context_files=list(mt.context_files),
                dependencies=list(mt.dependencies),
                files_touched=list(mt.files_touched),
                test_strategy=mt.test_strategy,
                created_at=now,
                updated_at=now,
            ))

        # Update Run.status in DB
        run_record = db_session.get(Run, run_id)
        if run_record:
            run_record.status = str(new_status)
            run_record.updated_at = now

        # Store plan summary as artifact
        if plan:
            db_session.merge(Artifact(
                artifact_id=f"art_{run_id}_plan_v1",
                run_id=run_id,
                kind="plan",
                content_text=plan.summary,
                created_at=now,
            ))
    
        # Emit plan.drafted event
        EventLogger.log(db_session, run_id, Roles.PLANNER.value, "plan.drafted", {
            "microtasks_count": len(microtasks),
            "plan_version": 1,
            "plan_summary": plan.summary[:300] if plan else "",
        })

    return result


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
