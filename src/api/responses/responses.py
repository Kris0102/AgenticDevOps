from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from gitops.github_api import RepoValidationResult


class AgentModelView(BaseModel):
    """One agent's model selection for a run (GH #64) — editable mid-run."""
    agent: str          # planner | reviewer | coder | tester | reflector
    provider_id: str
    model: str


class ProviderOption(BaseModel):
    """A provider for the run-settings model pickers — no secrets."""
    id: str
    label: str
    models: List[str]


class CostView(BaseModel):
    """USD cost breakdown for one (phase, model) bucket — the caching slices are the
    cache-priced part of the input spend. `None` cost fields never happen here (an
    unpriced model omits the whole line via `priced=False` on the parent)."""
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class PhaseCostView(BaseModel):
    """One agent×model slice of a run's spend (GH #62). A model can switch mid-run, so a
    single phase may appear on more than one line. `priced=False` means the model has no
    list price (self-hosted cluster) — tokens are still shown, `cost` is None."""
    phase: str                       # planner | reviewer | coder | tester | reflector
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    total_tokens: int
    priced: bool
    cost: Optional[CostView] = None


class RunCostView(BaseModel):
    """Detailed per-phase/per-model cost for a run — derived from the durable `llm.usage`
    events, so it's correct live, after a clean finish, and after a crash (ADR 0025 /
    GH #62). `total_cost` sums only the priced lines; `has_unpriced` flags that some
    models (cluster) had no rate, making the total a lower bound."""
    lines: List[PhaseCostView] = []
    total_cost: float = 0.0
    total_tokens: int = 0
    has_unpriced: bool = False
    currency: str = "USD"


class RunConfigView(BaseModel):
    """The effective config a run is executing under (run-settings view). Everything is
    read-only EXCEPT `models` (per-agent), which can be changed mid-run via PATCH
    /runs/{id}/model while the run is not terminal."""
    models: List[AgentModelView]
    providers: List[ProviderOption]
    knobs: Dict[str, Any]                       # per-agent tuning — editable only when expert_editable
    constraints: Dict[str, Optional[int]]       # max_planner_reviewer_iterations / max_microtask_attempts / max_total_tokens
    hitl_checkpoints: List[str]                 # active review gates — read-only
    editable: bool                              # models can be changed now (run not terminal)
    # Knobs + loop-caps can be edited now — ONLY while the run is stalled or paused at a gate
    # (a running run's checkpoint would race the driver; a terminal run is frozen). GH #64
    # follow-up: the run-settings panel becomes a recovery console when this is true.
    expert_editable: bool  # Important for the frontend to know whehter to grey out 
    #the knobs/loop-caps or to allow editing. GH #64 follow-up.


class RepoView(BaseModel):
    """One registered repo (GH #41). `token` is the mask when a per-repo token is
    stored, else None — raw tokens never cross the API."""
    repo_id: str
    repo_url: str
    label: str
    token: Optional[str] = None
    default_branch: Optional[str] = None
    push_branch_override: Optional[str] = None
    last_validated_at: Optional[datetime] = None
    last_validation_result: Optional[RepoValidationResult] = None
    created_at: Optional[datetime] = None


class BranchListResponse(BaseModel):
    """Live GitHub branch list for a registered repo — never cached in the DB."""
    branches: List[str]


class EventView(BaseModel):
    """One durable domain event — a row from the `events` table. Powers the persistent
    timeline / history (polled via ?after=<seq>). `seq` is the monotonic DB cursor."""
    seq: int
    ts: str
    actor: str
    event_type: str
    payload: Dict[str, Any]


class LogView(BaseModel):
    """One live log line, tee'd from structlog into the per-run bus. Ephemeral (live
    feed only — the durable record is EventView). `seq` is an in-run buffer cursor,
    not a DB id, so it resets per process and is only meaningful within one stream."""
    seq: int
    ts: Optional[str] = None
    level: Optional[str] = None
    category: Optional[str] = None
    event: str
    fields: Dict[str, Any] = {}


class RunState(str, Enum):
    """Coarse lifecycle the dashboard polls on — derived from the blackboard status,
    not a second source of truth. RUNNING means the graph is mid-flight (background
    task still executing); PAUSED_HITL means it suspended at a gate awaiting a human."""
    RUNNING = "running"
    PAUSED_HITL = "paused_hitl"
    COMPLETED = "completed"
    FAILED = "failed"
    # Non-terminal + no pending interrupt + no worker driving it: the process that was
    # running the graph died (uvicorn crash/restart, OOM-kill, reboot) before the _drive
    # guard could mark it FAILED. Recoverable — POST /runs/{id}/continue re-drives from the
    # checkpoint. Distinct from RUNNING (a worker IS driving it). See ADR 0019.
    STALLED = "stalled"
    # Terminal: a human hard-stopped the run early via POST /runs/{id}/stop. Distinct from
    # FAILED — rendered gray, labeled "manually stopped". See ADR 0033.
    STOPPED = "stopped"


class MicrotaskProgress(BaseModel):
    """The microtask currently being worked by the coder/tester/reflector loop —
    its position among all microtasks and its attempt count against the cap. See
    ADR 0028."""
    idx: int
    total: int
    title: str
    status: str
    attempts: int
    max_attempts: int


class PipelineProgress(BaseModel):
    """Iteration counters read straight off the checkpointed `Iteration`/`Microtask`
    state (ADR 0028) — lets the dashboard show "review pass N" and "microtask #i/N,
    attempt a/b" instead of guessing a coarse phase from the raw status string."""
    planner_reviewer_iteration: int
    planner_reviewer_max: int
    microtask: Optional[MicrotaskProgress] = None


class RunResponse(BaseModel):
    run_id: str
    run_state: RunState
    status: Optional[str] = None              # raw blackboard RunStatus value
    # set only when run_state == paused_hitl — lets a single poll tell the dashboard
    # a gate is waiting AND which kind, so it can fetch the right review context.
    hitl_phase: Optional[str] = None
    hitl_approval_id: Optional[str] = None
    # set only when run_state == stalled AND the stall carried a reason (transient LLM
    # error or an uncaught crash) — shown on the continue bar. None for a worker-died
    # stall (generic message).
    stalled_reason: Optional[str] = None
    # set only when run_state == stalled — 'token_budget' (Constraints.max_total_tokens
    # reached) vs 'transient' (LLM hiccup / uncaught crash / worker died). Lets the
    # dashboard show the raise-cap input only for the former (ADR 0025).
    stalled_kind: Optional[str] = None
    # set only when run_state == failed — the run.failed reason, so the dashboard can
    # say WHY instead of just showing a red badge.
    failure_reason: Optional[str] = None
    # set only when run_state == stopped — the run.stopped reason, for the gray banner.
    stopped_reason: Optional[str] = None
    # planner-loop / microtask-loop iteration counts, sourced from the checkpoint —
    # None before the graph has produced any Iteration/Microtask state yet (ADR 0028).
    progress: Optional[PipelineProgress] = None

class RunResponseWithDetails(RunResponse):
    """A run row for the dashboard's previous-runs list. Everything the run-box needs as
    labels + sort keys, so the list renders from one `GET /runs` call."""
    repo_url: str
    repo_name: str                    # owner/repo derived from repo_url — the box's title
    base_sha: Optional[str] = None
    task_description: Optional[str] = None   # what the run was asked to do
    pushed_files: Optional[int] = None        # files shipped by the deploy push (None = no push)
    llm_provider: Optional[str] = None        # settings hub — provider/model this run used
    llm_model: Optional[str] = None
    tokens_used_total: int = 0                # running total across every agent (ADR 0025)
    max_total_tokens: Optional[int] = None    # this run's token budget, if one was set
    created_at: datetime
    updated_at: datetime
    
    
    


# --------------------------------------------------------------- run detail views
# Durable per-run data (events/artifacts/microtasks tables) shaped for the dashboard's
# run-detail page — this is what makes an OLD run readable after its live log is gone.

class ArtifactView(BaseModel):
    """One artifacts-table row WITHOUT the (potentially huge) body — the browse list.
    Fetch the body on demand via GET /runs/{id}/artifacts/{artifact_id}."""
    artifact_id: str
    kind: str                      # plan | review | diff | test-<check> | reflection | ...
    microtask_id: Optional[str] = None  # producing microtask (diff/test/reflection); None = run-level
    created_at: datetime
    size: int                      # len(content_text) — lets the UI hint big payloads
    preview: str                   # first line(s), truncated — list-item subtitle


class ArtifactContent(ArtifactView):
    """Full artifact body — one artifact, fetched when the user opens it."""
    content_text: str


class IOExampleView(BaseModel):
    """One concrete input→output example backing an acceptance criterion."""
    given: Dict[str, Any] = {}
    expect: Any = None


class AcceptanceCriterionView(BaseModel):
    """One observable behaviour a microtask must satisfy, with any concrete
    input→output examples the Planner supplied."""
    text: str
    examples: List[IOExampleView] = []


class MicrotaskRow(BaseModel):
    """One microtasks-table row for the run-detail progress board (durable — works for
    finished runs, unlike the checkpoint-derived HITL PlanView). Carries the full record
    (description/approach/acceptance criteria/etc) so the dashboard's Microtasks tab can
    render complete detail without a second fetch."""
    microtask_id: str
    idx: int
    title: str
    description: str
    approach: Optional[str] = None
    status: str
    attempts: int
    acceptance_criteria: List[AcceptanceCriterionView] = []
    target_files: List[str]
    context_files: List[str] = []
    dependencies: List[str] = []
    files_touched: List[str]
    test_strategy: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class LessonView(BaseModel):
    """One per-repo lesson (ADR 0018) for the dashboard — what past runs on this repo
    taught the system."""
    lesson_id: str
    run_id: str
    scope: str                     # plan | code
    failure_type: Optional[str] = None
    summary: str
    guidance: Optional[str] = None
    created_at: datetime


class RunExportBundle(BaseModel):
    """Self-contained snapshot of one run's durable record — download as
    `agentic_run_<id>_<timestamp>.json`, import on another machine/DB via
    POST /runs/import. Carries its own identity (run_id inside `run`) so the import
    always lands on the correct run."""
    format_version: int = 1
    exported_at: datetime
    run: RunResponseWithDetails
    events: List[EventView]
    microtasks: List[MicrotaskRow]
    artifacts: List[ArtifactContent]           # full bodies — the point of an export


# --------------------------------------------------------------- HITL review views
# Lean projections of the blackboard, shaped for what the human reviews at a gate.
# Deliberately NOT the raw state models — the dashboard depends on these, not on
# internal state shapes that may drift.

class MicrotaskView(BaseModel):
    microtask_id: str
    title: str
    description: str
    approach: str
    acceptance_criteria: List[str]
    target_files: List[str]
    status: str


class PlanView(BaseModel):
    """Shown at a `plan` gate — the plan summary plus the microtasks it produced."""
    summary: str
    architecture_notes: str
    assumptions: List[str]
    risks: List[str]
    open_questions: List[str]
    microtasks: List[MicrotaskView]


class DiffView(BaseModel):
    """Shown at a `code` gate — `diff_text` is the raw unified `git diff` the
    dashboard renders with green/red (react-diff-view / diff2html)."""
    microtask_id: Optional[str] = None
    summary: str
    base_sha: str
    diff_text: str


class HitlReviewContext(BaseModel):
    """Everything the human needs to approve/reject the paused gate. `plan` is set
    for a plan gate, `diff` for a code gate; both None means the run isn't paused."""
    run_id: str
    approval_id: str
    phase: str
    current_microtask_id: Optional[str] = None
    prompt: str
    plan: Optional[PlanView] = None
    diff: Optional[DiffView] = None
    # Deploy gate only: the commit message that WOULD be used (per-run value or the
    # default), so the UI can prefill its editable commit-message input. ADR 0027.
    deploy_commit_message: Optional[str] = None
