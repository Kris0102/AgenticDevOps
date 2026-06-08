import operator
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, cast
from typing_extensions import TypedDict
from pydantic import BaseModel, Field
from langchain_core.messages import AnyMessage

class MicrotaskStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DONE = "DONE"
    BLOCKED = "BLOCKED"

class RunStatus(str, Enum):
    NEEDS_BOOTSTRAP = "NEEDS_BOOTSTRAP"
    NEEDS_PLAN = "NEEDS_PLAN"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_CODE = "NEEDS_CODE"
    NEEDS_TEST = "NEEDS_TEST"
    REFLECT = "REFLECT"
    NEEDS_FINAL_TEST = "NEEDS_FINAL_TEST"
    NEEDS_SECURITY_AUDIT = "NEEDS_SECURITY_AUDIT"
    READY_FOR_PR = "READY_FOR_PR"
    WAITING_FOR_CI = "WAITING_FOR_CI"
    NEEDS_HITL_APPROVAL = "NEEDS_HITL_APPROVAL"
    DONE = "DONE"
    FAILED = "FAILED"
    
class EventType(str, Enum):
    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status_changed"
    REPO_CLONED = "repo.cloned"
    PLAN_DRAFTED = "plan.drafted"
    MICROTASK_CREATED = "microtask.created"
    MICROTASK_UPDATED = "microtask.updated"
    MICROTASK_COMPLETED = "microtask.completed"
    MICROTASK_FAILED = "microtask.failed"
    SANDBOX_CREATED = "sandbox.created"
  

class Trigger(BaseModel):
    type: str
    source_url: str
    actor: str
    raw_payload_ref: Optional[str] = None

class Repo(BaseModel):
    repo_url: str
    default_branch: Optional[str] = None
    base_ref: str
    base_sha: Optional[str] = None
    workspace_branch: str
    pr_url: Optional[str] = None

class Sandbox(BaseModel):
    sandbox_id: str
    workspace_path: str
    status: str
    last_command: Optional[Dict[str, Any]] = None

class Objective(BaseModel):
    title: str
    description: str
    definition_of_done: List[str]
    non_goals: List[str]

class Constraints(BaseModel):
    time_budget_minutes: Optional[int] = None
    cost_budget_usd: Optional[float] = None
    max_total_iterations: int
    max_microtask_attempts: int
    allowed_languages: Optional[List[str]] = None
    forbidden_paths: Optional[List[str]] = None
    allowed_network: str
    risk_level: str

class Policy(BaseModel):
    require_hitl_for_pr: bool
    require_hitl_for_merge: bool
    require_hitl_for_prod_deploy: bool
    # Phase names that pause the run at NEEDS_HITL_APPROVAL before proceeding —
    # e.g. ["plan", "code", "test"]. One gate mechanism, configured per-run/risk-level.
    hitl_checkpoints: List[str] = Field(default_factory=list)
    security_scans_required: List[str]
    ci_required: bool
    protected_branches: List[str]
    # Security/Policy Auditor agent is optional (see ARCHITECTURE_preview.md §5.6) —
    # off by default; Phase D routes READY_FOR_PR directly when disabled.
    security_audit_enabled: bool = False

class Plan(BaseModel):
    summary: str
    architecture_notes: str
    data_contracts: List[str] = Field(default_factory=list)  # shared type/schema definitions all microtasks must honour
    assumptions: List[str]
    risks: List[str]
    open_questions: List[str]
    review_feedback: List[Dict[str, Any]] = Field(default_factory=list)

class Microtask(BaseModel):
    """A single deterministic objective given to the execution loop."""
    microtask_id: str
    title: str
    description: str  # WHAT to build and WHERE — file paths + what goes in them
    approach: str  # HOW — concrete technical plan: signatures, data shapes, sequencing, edge cases
    acceptance_criteria: List[str]
    target_files: List[str] = Field(default_factory=list)  # exact paths to create/edit — declared before coding
    context_files: List[str] = Field(default_factory=list)  # paths the Coder should read (not edit) to match conventions
    status: MicrotaskStatus = MicrotaskStatus.PENDING
    idx: int = 0
    dependencies: List[str] = Field(default_factory=list)
    attempts: int = 0
    max_attempts: Optional[int] = None
    files_touched: List[str] = Field(default_factory=list)  # filled after coding, not before
    test_strategy: Optional[str] = None
    notes: List[str] = Field(default_factory=list)

class Diff(BaseModel):
    base_sha: str
    diff_artifact_ref: str
    summary: str

class CheckOutput(BaseModel):
    status: str
    exit_code: int
    artifact_ref: str

class Checks(BaseModel):
    lint: Optional[CheckOutput] = None
    typecheck: Optional[CheckOutput] = None
    unit_tests: Optional[CheckOutput] = None
    integration_tests: Optional[CheckOutput] = None
    build: Optional[CheckOutput] = None

class CI(BaseModel):
    status: str
    provider: str
    run_url: Optional[str] = None
    failed_jobs: List[Dict[str, Any]] = Field(default_factory=list)

class Evidence(BaseModel):
    latest_diff: Optional[Diff] = None
    checks: Checks = Field(default_factory=Checks)
    ci: Optional[CI] = None

class Artifact(BaseModel):
    """Records logs, traces, or diffs deposited in the episodic memory."""
    artifact_ref: str
    kind: str  # e.g., "log", "diff", "scan_report"
    created_at: str
    summary: str

class Reflection(BaseModel):
    timestamp: str
    microtask_id: Optional[str] = None
    failure_type: str
    root_cause: str
    fix_instructions: str
    linked_artifacts: List[str] = Field(default_factory=list)
    should_store_in_semantic_memory: bool

class Security(BaseModel):
    scan_results: List[Dict[str, Any]] = Field(default_factory=list)
    final_decision: Optional[str] = None
    notes: Optional[str] = None

class HITL(BaseModel):
    required: bool
    pending_approvals: List[Dict[str, Any]] = Field(default_factory=list)

class Result(BaseModel):
    outcome: str
    reason: str
    summary: str
    final_artifacts: List[str] = Field(default_factory=list)
    ended_at: str

EventSeverity = Literal["debug", "info", "warning", "error", "critical"]

class Event(BaseModel):
    event_id: str
    run_id: str
    seq: int
    timestamp: str # ISO8601
    actor: str
    type: str
    severity: EventSeverity = "info"
    message: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    artifact_refs: List[str] = Field(default_factory=list)

class EventsPointer(BaseModel):
    store: str = "postgres"
    stream_id: str
    last_event_id: Optional[str] = None
    last_seq: Optional[int] = None
    tail: List[Event] = Field(default_factory=list)

class BlackboardState(TypedDict):
    """
    The authoritative Run State Machine (Blackboard) for a run.
    Owned by the Orchestrator. Agents communicate by reading/writing to this state via Orchestrator events.
    """
    schema_version: str
    run_id: str
    created_at: str
    updated_at: str
    
    trigger: Trigger
    repo: Repo
    sandbox: Sandbox
    objective: Objective
    constraints: Constraints
    policy: Policy
    
    status: RunStatus
    phase: Optional[str]
    iteration: Dict[str, Any]
    
    plan: Optional[Plan]
    microtasks: List[Microtask]
    current_microtask_id: Optional[str]
    
    evidence: Evidence
    artifacts_index: List[Artifact]
    reflections: List[Reflection]
    security: Security
    hitl: HITL
    
    result: Optional[Result]
    events: EventsPointer
    
    # Working memory for the current step (appends new messages automatically via langgraph)
    messages: Annotated[List[AnyMessage], operator.add]
    
    # Execution diagnostics
    error_logs: List[str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def merge_state(state: BlackboardState, partial: Dict[str, Any]) -> BlackboardState:
    """Merge a node's partial-update dict into the current state, preserving the
    BlackboardState TypedDict shape for the type checker (graph.invoke does the
    same shallow merge internally — this just lets us see the merged view early,
    e.g. to snapshot it before the framework applies it)."""
    return cast(BlackboardState, {**state, **partial})


def blackboard_state_to_dict(state: BlackboardState) -> Dict[str, Any]:
    """Serialize BlackboardState to a JSON-safe dict for run_state.state_json.

    `messages` is excluded — it's per-step working memory (langgraph appends to
    it automatically) and is reset to [] on reconstruction, not persisted.
    """
    plan = state["plan"]
    result = state["result"]
    return {
        "schema_version": state["schema_version"],
        "run_id": state["run_id"],
        "created_at": state["created_at"],
        "updated_at": state["updated_at"],
        "trigger": state["trigger"].model_dump(mode="json"),
        "repo": state["repo"].model_dump(mode="json"),
        "sandbox": state["sandbox"].model_dump(mode="json"),
        "objective": state["objective"].model_dump(mode="json"),
        "constraints": state["constraints"].model_dump(mode="json"),
        "policy": state["policy"].model_dump(mode="json"),
        "status": state["status"].value,
        "phase": state["phase"],
        "iteration": state["iteration"],
        "plan": plan.model_dump(mode="json") if plan else None,
        "microtasks": [mt.model_dump(mode="json") for mt in state["microtasks"]],
        "current_microtask_id": state["current_microtask_id"],
        "evidence": state["evidence"].model_dump(mode="json"),
        "artifacts_index": [a.model_dump(mode="json") for a in state["artifacts_index"]],
        "reflections": [r.model_dump(mode="json") for r in state["reflections"]],
        "security": state["security"].model_dump(mode="json"),
        "hitl": state["hitl"].model_dump(mode="json"),
        "result": result.model_dump(mode="json") if result else None,
        "events": state["events"].model_dump(mode="json"),
        "error_logs": state["error_logs"],
    }


def blackboard_state_from_dict(data: Dict[str, Any]) -> BlackboardState:
    """Rehydrate a BlackboardState from a run_state.state_json snapshot.

    `messages` is reset to [] — ephemeral working memory, never persisted.
    """
    plan = data.get("plan")
    result = data.get("result")
    return BlackboardState(
        schema_version=data["schema_version"],
        run_id=data["run_id"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        trigger=Trigger(**data["trigger"]),
        repo=Repo(**data["repo"]),
        sandbox=Sandbox(**data["sandbox"]),
        objective=Objective(**data["objective"]),
        constraints=Constraints(**data["constraints"]),
        policy=Policy(**data["policy"]),
        status=RunStatus(data["status"]),
        phase=data.get("phase"),
        iteration=data["iteration"],
        plan=Plan(**plan) if plan else None,
        microtasks=[Microtask(**mt) for mt in data.get("microtasks", [])],
        current_microtask_id=data.get("current_microtask_id"),
        evidence=Evidence(**data["evidence"]),
        artifacts_index=[Artifact(**a) for a in data.get("artifacts_index", [])],
        reflections=[Reflection(**r) for r in data.get("reflections", [])],
        security=Security(**data["security"]),
        hitl=HITL(**data["hitl"]),
        result=Result(**result) if result else None,
        events=EventsPointer(**data["events"]),
        messages=[],
        error_logs=data.get("error_logs", []),
    )


def create_initial_blackboard_state(
    *,
    run_id: str,
    trigger: Trigger,
    repo: Repo,
    objective: Objective,
    constraints: Constraints,
    policy: Policy,
    status: RunStatus = RunStatus.NEEDS_BOOTSTRAP,
) -> BlackboardState:
    """Build a complete BlackboardState for graph.invoke (all TypedDict keys required)."""
    now = _utc_now_iso()
    return BlackboardState(
        schema_version="1.0.0",
        run_id=run_id,
        created_at=now,
        updated_at=now,
        trigger=trigger,
        repo=repo,
        sandbox=Sandbox(
            sandbox_id=f"sbox_{run_id}",
            workspace_path="/workspace",
            status="not_created",
        ),
        objective=objective,
        constraints=constraints,
        policy=policy,
        status=status,
        phase="bootstrap",
        iteration={
            "global_step": 0,
            "global_iterations_used": 0,
            "current_loop_reason": None,
        },
        plan=None,
        microtasks=[],
        current_microtask_id=None,
        evidence=Evidence(),
        artifacts_index=[],
        reflections=[],
        security=Security(),
        hitl=HITL(required=False),
        result=None,
        events=EventsPointer(stream_id=run_id),
        messages=[],
        error_logs=[],
    )
