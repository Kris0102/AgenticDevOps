import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel

from db.db import db
from db.event_log import EventLogger
from orchestrator.state import (
    ApprovalStatus,
    BlackboardState,
    EventType,
    FeedbackDecision,
    FeedbackSource,
    PendingApproval,
    Phase,
    Reflection,
    ReviewFeedback,
    RunStatus,
)
from tool_gateway.Roles import Roles


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HitlStateError(RuntimeError):
    """The HITL machinery was reached with inconsistent blackboard state (e.g. the
    `hitl` node ran with no pending approval). Signals a routing/topology bug, not a
    bad human decision — fail loudly rather than route on garbage."""


class HitlDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class GateSpec:
    """Static per-phase routing for a HITL gate.

    `trigger` is the forward status the phase node emits when it wants to advance
    past this gate (approve re-emits it). `reject` is where a rejection loops back to.
    """
    trigger: RunStatus
    reject: RunStatus


class HitlRequest(BaseModel):
    """What the human sees when the graph suspends — crosses the `interrupt()` JSON
    boundary via `.model_dump(mode="json")`, never as a raw dict literal."""
    approval_id: str
    phase: str
    requested_at: str
    current_microtask_id: Optional[str] = None
    prompt: str
    error: Optional[str] = None


class HitlResume(BaseModel):
    """The human's verdict, injected via `Command(resume=...)`. Validated from the
    raw resume payload so an ill-formed decision fails loudly, not silently."""
    decision: HitlDecision
    note: Optional[str] = None
    reviewer: str = "cli-user"


class HitlGate:
    """Single code path for every mid-run Human-In-The-Loop checkpoint.

    A phase node funnels its would-be result through `apply(phase, result, state)`:
    if `phase` is in `policy.hitl_checkpoints` AND the node produced the forward
    status this gate sits in front of (`GateSpec.trigger`), the status is rewritten
    to NEEDS_HITL_APPROVAL and a `PendingApproval` is recorded — the graph then routes
    to the shared `hitl` node, which suspends via langgraph `interrupt()`.

    On resume, `resolve(state, decision)` applies the human verdict:
    - approve -> proceed with the originally-intended forward status
    - reject  -> loop back to the owning phase, carrying the (mandatory) note into
                 that phase's feedback channel (plan -> review_feedback;
                 code/test -> a Reflection for the reflector).

    Interaction with main.py (connection is through LangGraph's checkpoint JSON
    boundary, not direct calls):
      apply()           called by phase nodes in orchestrator.py, before the interrupt
      request_payload() called by the `hitl` node to build the interrupt() payload
      resolve()         called by the `hitl` node after interrupt() returns the resume

    main.py never calls HitlGate directly. It only builds
    Command(resume=HitlResume.model_dump()) which LangGraph stores in the checkpoint
    and delivers back to interrupt()'s return value when graph.invoke(Command(...)) is
    called. The `hitl` node then deserializes it with HitlResume.model_validate() and
    passes it to resolve().
    """

    GATES: Dict[Phase, GateSpec] = {
        Phase.PLAN: GateSpec(trigger=RunStatus.NEEDS_CODE, reject=RunStatus.NEEDS_PLAN),
        Phase.CODE: GateSpec(trigger=RunStatus.NEEDS_TEST, reject=RunStatus.REFLECT),
        # tester's pass status is provisional until the real Tester lands — it marks
        # the microtask done / run done on a green check. Revisit when wired.
        Phase.TEST: GateSpec(trigger=RunStatus.DONE, reject=RunStatus.REFLECT),
        # deploy is a one-shot outward push; approve → push, reject → end the run
        # un-deployed (no loop-back, so no feedback channel — see _write_feedback).
        Phase.DEPLOY: GateSpec(trigger=RunStatus.READY_TO_DEPLOY, reject=RunStatus.DONE),
    }

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _latest_approval(state: BlackboardState) -> PendingApproval:
        """The approval the `hitl` node is currently resolving — always the most
        recently appended one. Raises if the blackboard has none (a routing bug)."""
        hitl = state.get("hitl")
        if hitl is None or not hitl.pending_approvals:
            raise HitlStateError(
                "hitl node reached with no pending approval on the blackboard"
            )
        return hitl.pending_approvals[-1]

    @staticmethod
    def _log(state: BlackboardState, actor: str, event: EventType, payload: Dict[str, Any]) -> None:
        with db.session_scope() as session:
            EventLogger.log(session, state.get("run_id"), actor, event.value, payload)

    # -------------------------------------------------------------- gate points

    @staticmethod
    def apply(phase: Phase, result: Dict[str, Any], state: BlackboardState) -> Dict[str, Any]:
        """Gate a phase node's result. If `phase` has a HITL gate, is enabled in
        `hitl_checkpoints`, AND the node produced this gate's trigger status: rewrite
        the status to NEEDS_HITL_APPROVAL, record a PendingApproval, and return the
        updated dict for the `hitl` node. Otherwise return `result` unmodified."""
        gate = HitlGate.GATES.get(phase)
        hitl = state.get("hitl")
        if gate is None or hitl is None or phase.value not in hitl.hitl_checkpoints:
            return result
        if result.get("status") != gate.trigger:
            # node didn't produce the advance-status this gate guards — nothing to gate
            return result

        approval = PendingApproval(
            approval_id=f"appr_{uuid.uuid4().hex[:12]}",
            type=phase.value,
            requested_at=_utc_now_iso(),
            status=ApprovalStatus.PENDING,
        )
        new_hitl = hitl.model_copy(update={
            "required": True,
            "pending_approvals": list(hitl.pending_approvals) + [approval],
        })

        HitlGate._log(state, Roles.ORCHESTRATOR.value, EventType.HITL_REQUESTED, {
            "approval_id": approval.approval_id,
            "phase": phase.value,
            "current_microtask_id": state.get("current_microtask_id"),
        })

        return {**result, "status": RunStatus.NEEDS_HITL_APPROVAL, "hitl": new_hitl}

    @staticmethod
    def request_payload(state: BlackboardState) -> HitlRequest:
        """Build the interrupt() payload the human acts on, from the pending approval."""
        approval = HitlGate._latest_approval(state)
        phase = approval.type

        prompt = f"Approve or reject the '{phase}' phase. Reject requires a note."
        plan_file = HitlGate._plan_file_ref(state) if phase == Phase.PLAN.value else None
        if plan_file:
            prompt += f"\nPlan file: {plan_file}"

        return HitlRequest(
            approval_id=approval.approval_id,
            phase=phase,
            requested_at=approval.requested_at,
            current_microtask_id=state.get("current_microtask_id"),
            prompt=prompt,
        )

    @staticmethod
    def resolve(state: BlackboardState, decision: HitlResume) -> Dict[str, Any]:
        """Apply a resolved human verdict. The caller (the `hitl` node) has already
        enforced 'reject requires a note'. Approve advances with the gate's trigger
        status; reject loops back with the gate's reject status + seeded feedback."""
        approval = HitlGate._latest_approval(state)
        phase = Phase(approval.type)
        gate = HitlGate.GATES[phase]

        if decision.decision == HitlDecision.APPROVE:
            next_status = gate.trigger
            resolved = ApprovalStatus.APPROVED
            feedback: Dict[str, Any] = {}
        else:
            next_status = gate.reject
            resolved = ApprovalStatus.REJECTED
            feedback = HitlGate._write_feedback(phase, decision.note or "", state)

        updated_approval = approval.model_copy(update={
            "status": resolved,
            "reviewer": decision.reviewer,
            "notes": decision.note,
        })
        hitl = state.get("hitl")
        new_hitl = hitl.model_copy(update={
            "required": False,
            "pending_approvals": list(hitl.pending_approvals[:-1]) + [updated_approval],
        })

        HitlGate._log(state, Roles.HUMAN.value, EventType.HITL_DECIDED, {
            "approval_id": approval.approval_id,
            "phase": phase.value,
            "decision": decision.decision.value,
            "reviewer": decision.reviewer,
            "notes": decision.note,
        })

        return {"status": next_status, "hitl": new_hitl, **feedback}

    # --------------------------------------------------------- feedback routing

    @staticmethod
    def _plan_file_ref(state: BlackboardState) -> Optional[str]:
        """Latest `plan_file` artifact ref, surfaced to the human reviewing a plan."""
        artifacts = state.get("artifacts_index") or []
        plan_artifacts = [a for a in artifacts if a.kind == "plan_file"]
        return plan_artifacts[-1].artifact_ref if plan_artifacts else None

    @staticmethod
    def _write_feedback(phase: Phase, note: str, state: BlackboardState) -> Dict[str, Any]:
        """Route a rejection note into the channel its loop-back phase reads:
        - plan       -> append a ReviewFeedback the planner reads on a revision pass
        - code/test  -> seed a Reflection the reflector turns into coder guidance
        - deploy     -> no loop-back phase, so no feedback channel
        """
        if phase == Phase.PLAN:
            plan = state.get("plan")
            if plan is None:
                return {}
            entry = ReviewFeedback(
                decision=FeedbackDecision.REJECT,
                source=FeedbackSource.HUMAN,
                notes=note,
                ts=_utc_now_iso(),
            )
            new_plan = plan.model_copy(update={
                "review_feedback": list(plan.review_feedback) + [entry],
            })
            return {"plan": new_plan}

        if phase == Phase.DEPLOY:
            # deploy reject ends the run (status → DONE); nothing downstream to seed
            return {}

        # code / test -> seed a Reflection the reflector picks up
        reflection = Reflection(
            timestamp=_utc_now_iso(),
            microtask_id=state.get("current_microtask_id"),
            failure_type="human_rejection",
            root_cause=note,
            fix_instructions=note,
            linked_artifacts=[],
            should_store_in_semantic_memory=False,
        )
        return {"reflections": list(state.get("reflections") or []) + [reflection]}
