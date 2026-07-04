from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


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


class RunResponse(BaseModel):
    run_id: str
    run_state: RunState
    status: Optional[str] = None              # raw blackboard RunStatus value
    # set only when run_state == paused_hitl — lets a single poll tell the dashboard
    # a gate is waiting AND which kind, so it can fetch the right review context.
    hitl_phase: Optional[str] = None
    hitl_approval_id: Optional[str] = None
    # set only when run_state == stalled AND the stall was a transient LLM error — the
    # reason to show on the continue bar. None for a worker-died stall (generic message).
    stalled_reason: Optional[str] = None

class RunResponseWithDetails(RunResponse):
    """A run row for the dashboard's previous-runs list. Everything the run-box needs as
    labels + sort keys, so the list renders from one `GET /runs` call."""
    repo_url: str
    repo_name: str                    # owner/repo derived from repo_url — the box's title
    base_sha: Optional[str] = None
    task_description: Optional[str] = None   # what the run was asked to do
    pushed_files: Optional[int] = None        # files shipped by the deploy push (None = no push)
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
    created_at: datetime
    size: int                      # len(content_text) — lets the UI hint big payloads
    preview: str                   # first line(s), truncated — list-item subtitle


class ArtifactContent(ArtifactView):
    """Full artifact body — one artifact, fetched when the user opens it."""
    content_text: str


class MicrotaskRow(BaseModel):
    """One microtasks-table row for the run-detail progress board (durable — works for
    finished runs, unlike the checkpoint-derived HITL PlanView)."""
    microtask_id: str
    idx: int
    title: str
    status: str
    attempts: int
    target_files: List[str]
    files_touched: List[str]


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
