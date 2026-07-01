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
