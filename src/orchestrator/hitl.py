import enum
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from db.db import db
from db.event_log import EventLogger
from orchestrator.state import (
    ApprovalStatus,
    BlackboardState,
    EventType,
    FeedbackDecision,
    FeedbackSource,
    Microtask,
    MicrotaskStatus,
    PendingApproval,
    Reflection,
    ReviewFeedback,
    RunStatus,
)
from tool_gateway.Roles import Roles


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HitlDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True)
class GateSpec:
    """Static per-phase routing for a HITL gate.

    `trigger` is the forward status (or tuple of statuses) the phase node emits when it
    wants to advance past this gate.  On approve, `resolve()` re-emits the *original*
    status stored in `PendingApproval.forward_status` (so multi-trigger gates preserve
    the exact advance status — e.g. TEST fires on both NEEDS_CODE and DONE).
    `reject` is where a rejection loops back to.
    """
    trigger: Any  # RunStatus or tuple[RunStatus, ...]
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

class Phase(str, Enum):
    PLAN = "plan"
    CODE = "code"
    TEST = "test"
    DEPLOY = "deploy"
    def __to_enum__(self, word):
        return self.__class__(word)
    def __str__(self) -> str:
        return self.value  
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

    Interaction with main.py (connection is through LangGraph's checkpoint JSON boundary,
    not direct calls):
      apply()           called by phase stubs in orchestrator.py, before the interrupt
      request_payload() called by the _hitl node to build the interrupt() payload
      resolve()         called by the _hitl node after interrupt() returns the resume value

    main.py never calls HitlGate directly. It only builds Command(resume=HitlResume.model_dump())
    which LangGraph stores in the checkpoint and delivers back to interrupt()'s return value
    when graph.invoke(Command(...)) is called. The _hitl node then deserializes it with
    HitlResume.model_validate() and passes it to resolve().
    """

    GATES: Dict[str, GateSpec] = {
        Phase.PLAN: GateSpec(trigger=RunStatus.NEEDS_CODE, reject=RunStatus.NEEDS_PLAN),
        Phase.CODE: GateSpec(trigger=RunStatus.NEEDS_TEST, reject=RunStatus.REFLECT),
        # TEST fires on every passing verdict: NEEDS_CODE (intermediate microtask) and
        # DONE (final microtask).  Approve re-emits the original status via
        # PendingApproval.forward_status so routing continues correctly.
        # Reject loops back to NEEDS_TEST → the tester rewrites the tests with the
        # human note injected into the prompt (preferred over reflector→coder because
        # the human is reviewing test quality, not implementation correctness).
        Phase.TEST: GateSpec(
            trigger=(RunStatus.NEEDS_CODE, RunStatus.DONE),
            reject=RunStatus.NEEDS_TEST,
        ),
        # deploy is a one-shot outward push; approve → push, reject → end the run
        # un-deployed (no loop-back, so no feedback channel — see _write_feedback).
        Phase.DEPLOY: GateSpec(trigger=RunStatus.READY_TO_DEPLOY, reject=RunStatus.DONE),
    }

    @staticmethod
    def apply(phase: Phase, result: Dict[str, Any], state: BlackboardState) -> Dict[str, Any]:
        """Check if this phase has a HITL gate and if the result triggers it; if so, rewrite status, record PendingApproval,
        and return updated state for the `hitl` node; else return original result unmodified."""
        gate = HitlGate.GATES.get(phase)
        policy = state.get("policy")
        if gate is None or policy is None or phase.value not in policy.hitl_checkpoints:
            return result

        forward = result.get("status")
        triggers = gate.trigger if isinstance(gate.trigger, tuple) else (gate.trigger,)
        if forward not in triggers:
            # node didn't produce an advance-status this gate guards — nothing to gate
            return result

        # Capture the tested microtask_id (set by TesterRunner for TEST gates) and
        # strip the internal key so it never reaches LangGraph's TypedDict state.
        tested_microtask_id: Optional[str] = result.get("_tested_microtask_id")
        clean_result = {k: v for k, v in result.items() if k != "_tested_microtask_id"}

        approval = PendingApproval(
            approval_id=f"appr_{uuid.uuid4().hex[:12]}",
            type=phase.value,
            requested_at=_utc_now_iso(),
            status=ApprovalStatus.PENDING,
            # Preserve the exact advance status so resolve() re-emits it on approve.
            forward_status=forward.value if hasattr(forward, "value") else str(forward),
            # Store which microtask triggered this gate so rejection can reset it.
            microtask_id=tested_microtask_id,
        )
        hitl = state.get("hitl")
        new_hitl = hitl.model_copy(update={
            "required": True,
            "pending_approvals": list(hitl.pending_approvals) + [approval],
        })

        with db.session_scope() as session:
            EventLogger.log(session, state.get("run_id"), Roles.ORCHESTRATOR.value, EventType.HITL_REQUESTED.value, {
                "approval_id": approval.approval_id,
                "phase": phase.value,
                "microtask_id": tested_microtask_id,
                "current_microtask_id": state.get("current_microtask_id"),
            })

        return {**clean_result, "status": RunStatus.NEEDS_HITL_APPROVAL, "hitl": new_hitl}

    @staticmethod
    def request_payload(state: BlackboardState) -> HitlRequest:
        '''Construct the HITL checkpoint payload for the human, based on the current state.'''
        approval = state.get("hitl").pending_approvals[-1]
        phase = approval.type

        plan_file: Optional[str] = None
        if phase == "plan":
            artifacts = state.get("artifacts_index") or []
            plan_artifacts = [a for a in artifacts if a.kind == "plan_file"]
            if plan_artifacts:
                plan_file = plan_artifacts[-1].artifact_ref

        if phase == "plan":
            prompt = "Approve or reject the plan. Reject requires a note (it will be fed back to the planner as revision context)."
            if plan_file:
                prompt += f"\nPlan file: {plan_file}"
        elif phase == "code":
            prompt = "Approve or reject the coder's diff. Reject requires a note (it seeds a Reflection for the reflector → coder path)."
        elif phase == "test":
            test_output_path = state.get("last_test_output_path")
            prompt = (
                "Approve or reject the test output. Reject requires a note.\n"
                "  • Approve → proceed to the next microtask (or deploy if this was the final one).\n"
                "  • Reject  → the tester rewrites the tests using your note as guidance."
            )
            if test_output_path:
                prompt += f"\nTest output file: {test_output_path}"
        elif phase == "deploy":
            prompt = "Approve to push to the branch and create a PR. Reject to end the run without deploying (no note required)."
        else:
            prompt = f"Approve or reject the '{phase}' phase. Reject requires a note."

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
        enforced 'reject requires a note'."""
        hitl = state.get("hitl")
        approval = hitl.pending_approvals[-1]
        phase = Phase(approval.type)
        gate = HitlGate.GATES[phase]

        if decision.decision == HitlDecision.APPROVE:
            # Re-emit the original advance status (stored when the gate fired).
            # Falls back to the first trigger for backward-compat approvals without forward_status.
            if approval.forward_status:
                next_status = RunStatus(approval.forward_status)
            else:
                triggers = gate.trigger if isinstance(gate.trigger, tuple) else (gate.trigger,)
                next_status = triggers[0]
            resolved = ApprovalStatus.APPROVED
            feedback: Dict[str, Any] = {}
        else:
            next_status = gate.reject # this ensures we loop back to the owning phase for a re-do with feedback
            resolved = ApprovalStatus.REJECTED
            feedback = HitlGate._write_feedback(phase, decision.note or "", state, approval)

        updated_approval = approval.model_copy(update={
            "status": resolved,
            "reviewer": decision.reviewer,
            "notes": decision.note,
        })
        new_hitl = hitl.model_copy(update={
            "required": False,
            "pending_approvals": list(hitl.pending_approvals[:-1]) + [updated_approval],
        })

        with db.session_scope() as session:
            EventLogger.log(session, state.get("run_id"), Roles.HUMAN.value, EventType.HITL_DECIDED.value, {
                "approval_id": approval.approval_id,
                "phase": phase,
                "decision": decision.decision.value,
                "reviewer": decision.reviewer,
                "notes": decision.note,
            })

        return {"status": next_status, "hitl": new_hitl, **feedback}

    @staticmethod
    def _write_feedback(
        phase: Phase,
        note: str,
        state: BlackboardState,
        approval: Optional[PendingApproval] = None,
    ) -> Dict[str, Any]:
        """Route the human's rejection note into the channel the loop-back phase reads."""
        if phase == Phase.PLAN:
            plan = state.get("plan")
            entry = ReviewFeedback(
                decision=FeedbackDecision.REJECT,
                source=FeedbackSource.HUMAN,
                notes=note,
                ts=_utc_now_iso(),
            )
            if plan is None:
                return {}
            new_plan = plan.model_copy(update={
                "review_feedback": list(plan.review_feedback) + [entry],
            })
            return {"plan": new_plan}

        if phase == Phase.DEPLOY:
            return {}

        if phase == Phase.TEST:
            feedback: Dict[str, Any] = {"test_rejection_note": note}
            # The microtask was already marked COMPLETED before the gate fired.
            # Reset it to IN_PROGRESS so _select_microtask can find it on the next
            # tester invocation; also restore current_microtask_id so the tester
            # picks up exactly the right task.
            microtask_id = approval.microtask_id if approval else None
            if microtask_id:
                microtasks: List[Microtask] = list(state.get("microtasks") or [])
                feedback["microtasks"] = [
                    mt.model_copy(update={"status": MicrotaskStatus.IN_PROGRESS})
                    if mt.microtask_id == microtask_id else mt
                    for mt in microtasks
                ]
                feedback["current_microtask_id"] = microtask_id
            return feedback

        # code reject → seed a Reflection the reflector picks up (reflector → coder path)
        reflection = Reflection(
            timestamp=_utc_now_iso(),
            microtask_id=state.get("current_microtask_id"),
            failure_type="human_rejection",
            root_cause=note,
            fix_instructions=note,
            linked_artifacts=[],
            should_store_in_semantic_memory=False,
        )
        return {"reflections": list(state.get("reflections")) + [reflection]}
