import operator
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional
from typing_extensions import TypedDict
from pydantic import BaseModel, Field, field_validator
from langchain_core.messages import AnyMessage


class Phase(str, Enum):
    """The gateable phases of a run. Lives here (not in hitl.py) so the state models
    can type `hitl_checkpoints` against it without a state<->hitl import cycle; hitl.py
    imports it back from here."""
    PLAN = "plan"
    CODE = "code"
    TEST = "test"
    DEPLOY = "deploy"

    @classmethod
    def try_from_str(cls, s: str) -> Optional["Phase"]:
        try:
            return cls(s)
        except ValueError:
            return None

    def __str__(self) -> str:
        return self.value


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
    READY_TO_DEPLOY = "READY_TO_DEPLOY"
    WAITING_FOR_CI = "WAITING_FOR_CI"
    NEEDS_HITL_APPROVAL = "NEEDS_HITL_APPROVAL"
    DONE = "DONE"
    FAILED = "FAILED"
    
    def __str__(self) -> str:
        return self.value
    
class EventType(str, Enum):
    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status_changed"
    REPO_CLONED = "repo.cloned"
    PLAN_DRAFTED = "plan.drafted"
    PLAN_REVIEWED = "plan.reviewed"
    MICROTASK_CREATED = "microtask.created"
    MICROTASK_UPDATED = "microtask.updated"
    MICROTASK_COMPLETED = "microtask.completed"
    MICROTASK_FAILED = "microtask.failed"
    SANDBOX_CREATED = "sandbox.created"
    RUN_COMPLETED = "run.completed"
    GITOPS_PUSHED = "gitops.pushed"
    RUN_FAILED = "run.failed"
    TOOL_EXECUTED = "tool.executed"
    TOOL_DENIED = "tool.denied"
    HITL_REQUESTED = "hitl.requested"
    HITL_DECIDED = "hitl.decided"
    REFLECTION_CREATED = "reflection.created"
    SEMANTIC_BASELINE_INGESTED = "semantic.baseline_ingested"
    SEMANTIC_WORKSPACE_SYNCED = "semantic.workspace_synced"

    def __str__(self) -> str:
        return self.value
  

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

    @field_validator("repo_url", "base_ref", "base_sha", "default_branch", "workspace_branch", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        # Stray leading/trailing whitespace (copy-paste, trailing newline, form input) breaks
        # `git clone` — "protocol '  https' is not supported". Strip it at the boundary.
        return v.strip() if isinstance(v, str) else v

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
    security_scans_required: List[str]
    ci_required: bool
    protected_branches: List[str]
    # Security/Policy Auditor agent is optional (see ARCHITECTURE_preview.md §5.6) —
    # off by default; Phase D routes READY_FOR_PR directly when disabled.
    security_audit_enabled: bool = False

class FeedbackSource(str, Enum):
    HUMAN = "human"
    REVIEWER = "reviewer"

class FeedbackDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"

class ReviewFeedback(BaseModel):
    """One verdict on a plan — from the LLM Plan Reviewer or a human HITL reviewer.
    The planner reads these on a re-plan to revise rather than regenerate blind.
    This is used for the HITL rejection notes which are then injected into the planner's revision context.
    """
    decision: FeedbackDecision
    source: FeedbackSource
    notes: str
    ts: str

class Plan(BaseModel):
    summary: str
    architecture_notes: str
    data_contracts: List[str] = Field(default_factory=list)  # shared type/schema definitions all microtasks must honour
    assumptions: List[str]
    risks: List[str]
    open_questions: List[str]
    review_feedback: List[ReviewFeedback] = Field(default_factory=list)

class IOExample(BaseModel):
    """A concrete input→output example for an acceptance criterion. When present, the
    Tester writes assertions directly from this planner-supplied data (deterministic,
    no LLM re-interpretation of prose) — the strongest grounding against tautological
    tests. `expect` is the expected return/result for the given inputs."""
    given: Dict[str, Any] = Field(default_factory=dict)
    expect: Any = None


class AcceptanceCriterion(BaseModel):
    """One observable behaviour a microtask must satisfy. `text` is the human-readable
    intent (authored by the Planner, independent of the Coder's implementation);
    `examples` are optional concrete I/O cases the Tester can assert on directly.
    See ADR 0015/0016 — this is the Planner→Tester grounding contract."""
    text: str
    examples: List[IOExample] = Field(default_factory=list)

    def render(self) -> str:
        """One-line human/prompt rendering: intent text plus any concrete examples."""
        line = self.text
        if self.examples:
            cases = "; ".join(f"given {e.given} → expect {e.expect!r}" for e in self.examples)
            line += f"  (e.g. {cases})"
        return line


class Microtask(BaseModel):
    """A single deterministic objective given to the execution loop."""
    microtask_id: str
    title: str
    description: str  # WHAT to build and WHERE — file paths + what goes in them
    approach: str  # HOW — concrete technical plan: signatures, data shapes, sequencing, edge cases
    acceptance_criteria: List[AcceptanceCriterion]  # observable behaviours the Tester grounds tests on (text + optional concrete examples)
    target_files: List[str] = Field(default_factory=list)  # exact paths to create/edit — declared before coding
    context_files: List[str] = Field(default_factory=list)  # paths the Coder should read (not edit) to match conventions
    status: MicrotaskStatus = MicrotaskStatus.PENDING
    idx: int = 0
    dependencies: List[str] = Field(default_factory=list)
    attempts: int = 0
    max_attempts: Optional[int] = None
    files_touched: List[str] = Field(default_factory=list)  # filled after coding, not before
    test_strategy: Optional[str] = None  # This field should be used by the tester to determine how to test the microtask's output. It will be used to generate the test cases for the microtask.
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

class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class PendingApproval(BaseModel):
    """A single HITL gate request, recorded when a phase pauses for human approval."""
    approval_id: str
    type: str  # gated phase name — matches a hitl.hitl_checkpoints entry ("plan"/"code"/"test")
    requested_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer: Optional[str] = None
    notes: Optional[str] = None
    # Original advance status before it was rewritten to NEEDS_HITL_APPROVAL.
    # Used by resolve() to re-emit the correct status on approve (critical for gates
    # with multiple triggers, e.g. TEST fires on both NEEDS_CODE and DONE).
    forward_status: Optional[str] = None
    # Microtask that was being processed when the gate fired.
    # Required by TEST rejection to reset the microtask back to IN_PROGRESS so the
    # tester can re-select and re-run it (the microtask is already COMPLETED in state
    # by the time the gate fires — without this the tester finds no pending work).
    microtask_id: Optional[str] = None


class HITL(BaseModel):
    required: bool
    pending_approvals: List[PendingApproval] = Field(default_factory=list)
    # Phases that pause the run at NEEDS_HITL_APPROVAL before proceeding —
    # e.g. [Phase.PLAN, Phase.CODE, Phase.TEST]. One gate mechanism, configured per-run.
    hitl_checkpoints: List[Phase] = Field(default_factory=list)

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
class Iteration(BaseModel):
    global_iteration:int
    planner_reviewer_iteration_current:int
    coder_tester_reflector_iteration_current:int
    global_iterations_used:int    
    
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
    iteration: Iteration
    
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

    # Tester feedback channel: set by HITL reject on the test gate OR by the Reflector
    # when it judges the synthesized TEST (not the code) wrong; cleared by the tester
    # after it reads it so it doesn't leak into later microtasks.
    test_rejection_note: Optional[str]
    # Path to the on-disk tester output file — surfaced in the HITL prompt so the
    # reviewer knows where to look for the full test output.
    last_test_output_path: Optional[str]
    # Bounds the reflector→tester test re-synthesis sub-loop (ADR 0016/GH #33): the
    # Reflector increments it on each fix_target="test" decision; once it hits
    # REFLECTOR_MAX_TEST_RESYNTH the reflector forces the code path. Reset by the coder.
    test_resynth_count: int


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_initial_blackboard_state(
    *,
    run_id: str,
    trigger: Trigger,
    repo: Repo,
    objective: Objective,
    constraints: Constraints,
    policy: Policy,
    hitl: HITL,
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
        iteration=Iteration(
            global_iteration=0,
            planner_reviewer_iteration_current=0,
            coder_tester_reflector_iteration_current=0,
            global_iterations_used=0,
        ),
        plan=None,
        microtasks=[],
        current_microtask_id=None,
        evidence=Evidence(),
        artifacts_index=[],
        reflections=[],
        security=Security(),
        hitl=hitl,   # use the caller's HITL (gate checkpoints) — NOT a fresh empty one
        result=None,
        events=EventsPointer(stream_id=run_id),
        messages=[],
        error_logs=[],
        test_rejection_note=None,
        last_test_output_path=None,
        test_resynth_count=0,
    )
