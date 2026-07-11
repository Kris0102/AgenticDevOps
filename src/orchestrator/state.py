import operator
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional
from typing_extensions import TypedDict
from pydantic import BaseModel, Field, field_validator
from langchain_core.messages import AnyMessage
from abc import ABC 

from tool_gateway.Roles import Roles


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
    GITOPS_PR_CREATED = "gitops.pr_created"
    GITOPS_SECRET_BLOCKED = "gitops.secret_blocked"
    RUN_FAILED = "run.failed"
    RUN_STALLED = "run.stalled"
    RUN_RESUMED = "run.resumed"
    TOOL_EXECUTED = "tool.executed"
    TOOL_DENIED = "tool.denied"
    HITL_REQUESTED = "hitl.requested"
    HITL_DECIDED = "hitl.decided"
    REFLECTION_CREATED = "reflection.created"
    SEMANTIC_BASELINE_INGESTED = "semantic.baseline_ingested"
    SEMANTIC_WORKSPACE_SYNCED = "semantic.workspace_synced"
    LLM_USAGE_RECORDED = "llm.usage"
    RUN_TOKEN_BUDGET_RAISED = "run.token_budget_raised"
    RUN_MODEL_CHANGED = "run.model_changed"

    def __str__(self) -> str:
        return self.value
  

class Trigger(BaseModel):
    type: str
    source_url: str
    actor: str
    raw_payload_ref: Optional[str] = None

class Repo(BaseModel):
    repo_url: str
    # Registry reference (repos.repo_id) — resolves the per-repo token and push
    # override at the gitops seams. None for legacy/unregistered runs.
    repo_id: Optional[str] = None
    default_branch: Optional[str] = None
    # Branch the agents work from. None = the repo's default branch (git clone
    # without --branch). base_sha remains an optional pin on top of it.
    base_ref: Optional[str] = None
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


# --------------------------------------------------------------- LLM / run config
# Settings hub (ADR: UI-managed providers/knobs). A run resolves ONE RunConfig at creation
# (per-run override › global DB settings › env seed) and carries it on the blackboard, so
# agents read model/knobs from state instead of os.getenv. See providers/llm.py.

class ProviderType(str, Enum):
    OPENAI_COMPATIBLE = "openai_compatible"   # the cluster, any OpenAI-shaped endpoint
    ANTHROPIC = "anthropic"                     # Claude


class ProviderProfile(BaseModel):
    """A named LLM provider — its endpoint + key + suggested models. Keys are masked when
    served to the UI (SettingsStore.get_public); the raw value is used only by the factory."""
    id: str
    label: str
    type: ProviderType = ProviderType.OPENAI_COMPATIBLE
    base_url: Optional[str] = None             # required for openai_compatible; ignored for anthropic
    api_key: Optional[str] = None
    models: List[str] = Field(default_factory=list)
    # Vendor-specific request params merged into every request body (GH #65) — the
    # escape hatch for the long tail of OpenAI-compatible endpoints (prompt_cache_key,
    # OpenRouter cache blocks, …) without per-vendor code. NOT for secrets (stored
    # unencrypted, returned unmasked). Unknown params 400 on strict endpoints —
    # verify via the provider's Test connection button.
    extra_request_params: Dict[str, Any] = Field(default_factory=dict)
   
class BaseLLMConfig(BaseModel, ABC):
    name: str   
    provider_id: str
    model: str
    temperature: float = 0.0
    max_retries: int = 3 
    
class PlannerLLMConfig(BaseLLMConfig):
    name: str = Roles.PLANNER.value
class ReviewerLLMConfig(BaseLLMConfig):
    name: str = Roles.REVIEWER.value
class CoderLLMConfig(BaseLLMConfig):
    name: str = Roles.CODER.value
class TesterLLMConfig(BaseLLMConfig):
    name: str = Roles.TESTER.value
class ReflectorLLMConfig(BaseLLMConfig):
    name: str = Roles.REFLECTOR.value
class ProbeLLMConfig(BaseLLMConfig):
    # Not a graph agent — a concrete BaseLLMConfig for one-off provider probes
    # (Settings 'Test connection', cache probe). Carries provider_id/model only.
    name: str = "probe"


class Knobs(BaseModel):
    """Every agent tuning value — formerly scattered os.getenv reads. Defaults mirror the
    old env defaults so behaviour is unchanged when nothing is set."""
    planner_max_tool_steps: int = 15
    planner_timeout_seconds: int = 60
    planner_max_synthesis_attempts: int = 3
    reviewer_max_tool_steps: int = 15
    reviewer_timeout_seconds: int = 60
    coder_max_tool_steps: int = 16
    coder_timeout_seconds: int = 120
    coder_max_empty_diff_retries: int = 2
    coder_max_compile_retries: int = 2
    tester_max_tool_steps: int = 6
    tester_timeout_seconds: int = 120
    reflector_max_tool_steps: int = 8
    reflector_timeout_seconds: int = 120
    reflector_max_test_resynth: int = 2

class LLMConfig(BaseModel):
    """Per-agent model selection for a run (GH #71). Each agent carries its own
    `provider_id` + `model`, so cheap models can run investigation (planner/reviewer/tester)
    while the Coder gets a stronger one. Each `provider_id` references a ProviderProfile that
    supplies base_url/api_key — many agents may point at the same provider (one key)."""
    planner: PlannerLLMConfig
    reviewer: ReviewerLLMConfig
    coder: CoderLLMConfig
    tester: TesterLLMConfig
    reflector: ReflectorLLMConfig

    def agents(self) -> List[BaseLLMConfig]:
        """The five per-agent selections, in graph order."""
        return [self.planner, self.reviewer, self.coder, self.tester, self.reflector]

class RunConfig(BaseModel):
    """The effective, resolved config a run executes under — per-agent `llm` selections, the
    ProviderProfiles those selections reference (by id), and `knobs`. Built once at run
    creation and stored on the blackboard (`state["config"]`). `providers` holds only the
    DISTINCT providers actually referenced (one entry per id — shared providers aren't
    duplicated); an agent resolves its own via `provider_for`."""
    llm: LLMConfig
    providers: Dict[str, ProviderProfile]   # by provider id — carries base_url/key
    knobs: Knobs = Field(default_factory=Knobs)

    def provider_for(self, llm: BaseLLMConfig) -> ProviderProfile:
        """The ProviderProfile a given per-agent selection runs on. Raises if the run was
        built without that provider (resolve_run_config validates this up front)."""
        provider = self.providers.get(llm.provider_id)
        if provider is None:
            raise ValueError(f"provider '{llm.provider_id}' not resolved for this run")
        return provider


class RunDefaults(BaseModel):
    """Global defaults the per-run page pre-fills (all overridable per run)."""
    max_total_iterations: int = 6
    max_microtask_attempts: int = 3
    # Default run-wide token budget (Constraints.max_total_tokens). None = no cap.
    max_total_tokens: Optional[int] = None
    hitl_checkpoints: List[Phase] = Field(default_factory=list)
    # Destroy the run's Docker sandbox once it reaches a TERMINAL state (completed/failed).
    # Never fires for a stalled run — its container must survive for /continue.
    teardown_on_terminal: bool = False
    # After a successful deploy push, open a GitHub PR (pushed branch → the run's base
    # branch) with a deterministically generated description. Read live at deploy time
    # (like teardown_on_terminal). A PR failure never fails a run whose push succeeded.
    auto_create_pr: bool = False


class AppSettings(BaseModel):
    """The whole UI-managed settings blob (one DB row). `selected` names the default
    provider+model; `providers` carry endpoints/keys; `knobs`/`defaults` seed new runs."""
    providers: List[ProviderProfile] = Field(default_factory=list)
    selected: LLMConfig
    knobs: Knobs = Field(default_factory=Knobs)
    defaults: RunDefaults = Field(default_factory=RunDefaults)
    # GitHub token for private-repo clone + branch push (gitops). Masked to the UI,
    # encrypted at rest like provider keys; None = anonymous (public repos only).
    github_token: Optional[str] = None

class Constraints(BaseModel):
    time_budget_minutes: Optional[int] = None
    cost_budget_usd: Optional[float] = None
    max_total_iterations: int
    max_microtask_attempts: int
    # Hard cap on total tokens consumed across every agent in this run. None = no
    # budget. Enforced in each node (see orchestrator/token_budget.py, ADR 0025).
    max_total_tokens: Optional[int] = None
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
    artifact_ref: str  # references the artifacts that were created by the tester! Contains information about the test output

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
    # Routing decision that produced this reflection: "code" (coder retry) | "test"
    # (regenerate the synthesized test). Persisted so the tester PASS path can store a
    # code-only lesson without re-deriving the target (ADR 0018).
    fix_target: str = "code"

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
    # Running total of tokens consumed by every agent's LLM calls this run. Accumulated
    # via model_copy after each node's invocation; enforced against
    # Constraints.max_total_tokens (orchestrator/token_budget.py). See ADR 0025.
    tokens_used_total: int = 0

# NOTE: lessons are DURABLE cross-run memory → the `lessons` DB table (db/models.py +
# db/lessons.py), NOT blackboard state (which is per-run and wiped). See ADR 0018.


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
    # Resolved LLM provider/model + tuning knobs for this run (settings hub). Agents read
    # from here, not os.getenv. Optional so pre-config runs / tests still construct.
    config: Optional[RunConfig]

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
    # Run-level baseline for the existing test suite (GH #28/#72): the NUMBER of failing tests
    # in the repo's own suite on the pristine tree, captured once at the first Tester run
    # (None until then) and fixed for the run. The `integration_tests` check gates only on the
    # DELTA — a NEW failure beyond this baseline (current_failures > baseline) is a real
    # regression and fails; failures at or below the baseline are pre-existing and advisory. So
    # a repo that ships a red test doesn't fail every microtask, yet the Coder breaking OTHER
    # existing tests is still caught.
    suite_baseline_failures: Optional[int]

    # Commit message for the deploy push. Seeded from RunRequest.commit_message at run
    # creation; overwritten by a message entered at the deploy HITL gate (approve
    # payload). None/blank → "AgenticDevOps run <run_id>". Read by DeployRunner. ADR 0027.
    deploy_commit_message: Optional[str]

    # Dry-run: when True the graph ends at DONE without deploying (no push, no PR) —
    # the tester/hitl DONE routers go straight to END instead of the deploy node.
    # Requires no push access at run-start (ADR 0029). Default False.
    skip_deploy: bool


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
    config: Optional[RunConfig] = None,
    status: RunStatus = RunStatus.NEEDS_BOOTSTRAP,
    deploy_commit_message: Optional[str] = None,
    skip_deploy: bool = False,
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
        config=config,
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
        suite_baseline_failures=None,
        deploy_commit_message=deploy_commit_message,
        skip_deploy=skip_deploy,
    )
