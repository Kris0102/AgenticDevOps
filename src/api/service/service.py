import asyncio
from typing import AsyncIterator, List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from api.requests.requests import RunRequest
from api.responses.responses import (
    DiffView,
    EventView,
    HitlReviewContext,
    LogView,
    MicrotaskView,
    PlanView,
    RunResponse,
    RunState,
)
from db.db import db
from db.event_log import EventLogger
from db.models import Artifact as ArtifactRow
from db.models import Event as EventRow
from Exceptions.RunExecutionError import RunExecutionError
from observability import Category, bind_run, clear_context, configure_logging, get_logger
from observability.log_bus import log_bus
from orchestrator.bootstrap_node import BootStrapper
from orchestrator.checkpointer import setup_checkpointer
from orchestrator.hitl import HitlResume, Phase
from orchestrator.orchestrator import graph
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import EventType, RunStatus, create_initial_blackboard_state
from tool_gateway.Roles import Roles

# How often the live-log SSE drains the in-memory bus. Localhost + in-memory, so this
# can be tight; 0.5s is imperceptible and cheap.
_LOG_STREAM_POLL_SECONDS = 0.5
# Keys promoted to LogView columns; everything else on a log line goes into `fields`.
_LOG_PROMOTED_KEYS = {"timestamp", "level", "category", "event", "run_id"}
_PRIMITIVE = (str, int, float, bool, type(None))

_log = get_logger(Category.ORCHESTRATOR)


class HitlNotPausedError(RuntimeError):
    """resume() was called on a run that isn't suspended at a HITL gate."""


class Service:
    """Headless run engine behind the HTTP control API. Mirrors RunLauncher (main.py)
    minus the interactive CLI: a single `graph.invoke()` drives the graph to the next
    `interrupt()` or END. So `run` advances to the first HITL gate (or completion), and
    each `resume` advances exactly one gate further — one HTTP round-trip per gate.

    All durable state lives in the LangGraph checkpoint, keyed by thread_id=run_id;
    this class holds no per-run state and is safe to construct once at app startup.

    NOTE (concurrency): the module-global `graph`/`checkpointer` share one psycopg
    connection. Running multiple runs truly in parallel needs a connection pool —
    fine for the single-user dashboard MVP, harden before multi-tenant. See ADR 0012.
    """

    def __init__(self) -> None:
        configure_logging()      # reads LOG_LEVEL / LOG_JSON from env
        setup_checkpointer()     # idempotent — ensures checkpoint tables exist

    # --------------------------------------------------------------- public API

    def run(self, request: RunRequest, run_id: str) -> RunResponse:
        """Start a fresh run: bootstrap the environment, then drive the graph to the
        first HITL pause or to completion. `run_id` is minted by the caller so the
        HTTP layer can return it before this (background) work finishes."""
        bind_run(run_id)
        try:
            _log.info("run.start", repo_url=request.repo.repo_url, base_sha=request.repo.base_sha)
            state = create_initial_blackboard_state(
                run_id=run_id,
                trigger=request.trigger,
                repo=request.repo,
                objective=request.objective,
                constraints=request.constraints,
                policy=request.policy,
                hitl=request.hitl,
                status=RunStatus.NEEDS_BOOTSTRAP,
            )
            bootstrap = self._bootstrap(run_id, state)
            if bootstrap.get("status") == RunStatus.FAILED:
                _log.error("run.bootstrap_failed", detail="not invoking graph")
                return self._response(run_id)
            self._drive(run_id, {**state, **bootstrap})
            self._finalize(run_id)
            return self._response(run_id)
        finally:
            clear_context()

    def resume(self, run_id: str, decision: HitlResume) -> RunResponse:
        """Apply a human verdict to a paused run and drive the graph to the next pause
        or to completion.

        No bootstrap here, unlike the CLI: a HITL gate is a *mid-graph pause* inside
        this same long-lived process, so the sandbox is still up and the checkpoint
        holds full state. Resume just delivers the verdict to the suspended
        `interrupt()` — `HitlGate.resolve()` then sets the next status and routes on.
        (The CLI re-bootstraps because each `--approve` invocation is a cold, separate
        process that can't assume the sandbox survived — see ADR 0005. Not our case.)"""
        bind_run(run_id)
        try:
            if not self._has_pending_interrupt(run_id):
                raise HitlNotPausedError(f"run {run_id} is not paused at a HITL gate")
            log_bus.reset(run_id)   # paused run is producing again — reopen its live stream
            _log.info("hitl.resuming", decision=decision.decision.value)
            self._drive(run_id, Command(resume=decision.model_dump(mode="json")))
            self._finalize(run_id)
            return self._response(run_id)
        finally:
            clear_context()

    def status(self, run_id: str) -> RunResponse:
        """Current lifecycle snapshot for the run — the dashboard's poll target."""
        return self._response(run_id)

    def review_context(self, run_id: str) -> Optional[HitlReviewContext]:
        """Everything the human reviews at the paused gate: the plan + microtasks for
        a `plan` gate, the raw diff for a `code` gate. None if the run isn't paused.
        Assembled on demand from the checkpoint + artifacts so the large diff text
        never has to ride the interrupt() payload across every superstep."""
        payload = self._pending_payload(run_id)
        if not payload:
            return None
        state = graph.get_state(self._config(run_id)).values or {}
        phase = payload.get("phase")
        return HitlReviewContext(
            run_id=run_id,
            approval_id=payload.get("approval_id", ""),
            phase=phase or "",
            current_microtask_id=payload.get("current_microtask_id"),
            prompt=payload.get("prompt", ""),
            plan=self._plan_view(state) if phase == Phase.PLAN.value else None,
            diff=self._diff_view(state) if phase == Phase.CODE.value else None,
        )

    def is_paused(self, run_id: str) -> bool:
        return self._has_pending_interrupt(run_id)

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> List[EventView]:
        """Durable log feed for a run: events with seq > `after`, oldest first. Powers
        both the polled timeline and (looped) the SSE tail. Reads the app DB engine —
        independent of the checkpointer connection the background run is using."""
        with db.session_scope() as session:
            rows = (
                session.query(EventRow)
                .filter(EventRow.run_id == run_id, EventRow.seq > after)
                .order_by(EventRow.seq.asc())
                .limit(limit)
                .all()
            )
            return [self._event_view(r) for r in rows]

    def logs(self, run_id: str, after: int = 0) -> List[LogView]:
        """Polled snapshot of the in-memory live log (entries with seq > `after`).
        SSE fallback — prefer stream_logs for a real-time feed."""
        entries, _ = log_bus.since(run_id, after)
        return [self._log_view(seq, ed) for seq, ed in entries]

    async def stream_logs(self, run_id: str, after: int = 0) -> AsyncIterator[str]:
        """Live log over SSE — drains the in-memory structlog bus for this run every
        ~0.5s. Granular ops detail (node.enter, tool steps), not just domain events.
        Ephemeral: closes once the run is marked done. (Durable history is GET /events.)"""
        cursor = after
        while True:
            entries, cursor = log_bus.since(run_id, cursor)
            for seq, event_dict in entries:
                yield self._sse_log_frame(seq, event_dict)
            if not entries:
                if log_bus.is_done(run_id):
                    return
                yield ": keep-alive\n\n"   # comment frame keeps the connection warm
            await asyncio.sleep(_LOG_STREAM_POLL_SECONDS)

    # ----------------------------------------------------------------- helpers

    def _config(self, run_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": run_id}}

    def _drive(self, run_id: str, graph_input) -> None:
        """One invoke == run until the next interrupt() or END (LangGraph batches all
        intervening supersteps). Headless: no in-process HITL prompt loop."""
        graph.invoke(graph_input, config=self._config(run_id))

    def _bootstrap(self, run_id: str, state) -> dict:
        try:
            return BootStrapper(state).invoke()
        except RunExecutionError as e:
            with db.session_scope() as session:
                return RunFailureHandler.handle(session, run_id, Roles.BOOTSTRAPPER.value, state, e)

    def _has_pending_interrupt(self, run_id: str) -> bool:
        snapshot = graph.get_state(self._config(run_id))
        return any(task.interrupts for task in snapshot.tasks)

    def _pending_payload(self, run_id: str) -> Optional[dict]:
        snapshot = graph.get_state(self._config(run_id))
        for task in snapshot.tasks:
            for intr in task.interrupts:
                if isinstance(intr.value, dict):
                    return intr.value
        return None

    def _response(self, run_id: str) -> RunResponse:
        snapshot = graph.get_state(self._config(run_id))
        status = snapshot.values.get("status") if snapshot.values else None
        pending = self._pending_payload(run_id)
        return RunResponse(
            run_id=run_id,
            run_state=self._derive_state(status, pending),
            status=status.value if isinstance(status, RunStatus) else status,
            hitl_phase=pending.get("phase") if pending else None,
            hitl_approval_id=pending.get("approval_id") if pending else None,
        )

    @staticmethod
    def _plan_view(state: dict) -> Optional[PlanView]:
        plan = state.get("plan")
        if plan is None:
            return None
        microtasks = state.get("microtasks") or []
        return PlanView(
            summary=plan.summary,
            architecture_notes=plan.architecture_notes,
            assumptions=plan.assumptions,
            risks=plan.risks,
            open_questions=plan.open_questions,
            microtasks=[
                MicrotaskView(
                    microtask_id=m.microtask_id,
                    title=m.title,
                    description=m.description,
                    approach=m.approach,
                    acceptance_criteria=m.acceptance_criteria,
                    target_files=m.target_files,
                    status=str(m.status),
                )
                for m in microtasks
            ],
        )

    def _diff_view(self, state: dict) -> Optional[DiffView]:
        evidence = state.get("evidence")
        latest = evidence.latest_diff if evidence else None
        if latest is None:
            return None
        return DiffView(
            microtask_id=state.get("current_microtask_id"),
            summary=latest.summary,
            base_sha=latest.base_sha,
            diff_text=self._artifact_text(latest.diff_artifact_ref) or "",
        )

    @staticmethod
    def _artifact_text(artifact_ref: str) -> Optional[str]:
        """Raw artifact body (e.g. the unified diff) from the artifacts table."""
        with db.session_scope() as session:
            row = session.get(ArtifactRow, artifact_ref)
            return row.content_text if row else None

    @staticmethod
    def _event_view(row: EventRow) -> EventView:
        return EventView(
            seq=row.seq,
            ts=row.ts.isoformat(),
            actor=row.actor,
            event_type=row.event_type,
            payload=row.payload_json,
        )

    @classmethod
    def _log_view(cls, seq: int, event_dict: dict) -> LogView:
        fields = {
            k: (v if isinstance(v, _PRIMITIVE) else str(v))
            for k, v in event_dict.items()
            if k not in _LOG_PROMOTED_KEYS
        }
        return LogView(
            seq=seq,
            ts=event_dict.get("timestamp"),
            level=event_dict.get("level"),
            category=event_dict.get("category"),
            event=str(event_dict.get("event", "")),
            fields=fields,
        )

    @classmethod
    def _sse_log_frame(cls, seq: int, event_dict: dict) -> str:
        # model_dump_json() is single-line, so the SSE `data:` field stays one line.
        return f"id: {seq}\nevent: log\ndata: {cls._log_view(seq, event_dict).model_dump_json()}\n\n"

    def _finalize(self, run_id: str) -> None:
        """After a drive returns, close out the run if it's terminal (not paused):
        record `run.completed` on a clean DONE (RUN_FAILED is already emitted by the
        failure handlers), and mark the live-log bus done so its SSE stream closes.
        Safe to read the checkpoint here — the invoke has returned, connection idle."""
        snapshot = graph.get_state(self._config(run_id))
        if any(task.interrupts for task in snapshot.tasks):
            return  # paused at a gate — more to come, leave the stream open
        status = snapshot.values.get("status") if snapshot.values else None
        if status == RunStatus.DONE:
            with db.session_scope() as session:
                EventLogger.log(
                    session, run_id, Roles.ORCHESTRATOR.value,
                    EventType.RUN_COMPLETED.value, {"status": RunStatus.DONE.value},
                )
        log_bus.mark_done(run_id)

    @staticmethod
    def _derive_state(status, pending: Optional[dict]) -> RunState:
        """Coarse lifecycle derived from the blackboard status — single source of
        truth, no parallel state field."""
        if pending is not None:
            return RunState.PAUSED_HITL
        if status == RunStatus.FAILED:
            return RunState.FAILED
        if status == RunStatus.DONE:
            return RunState.COMPLETED
        return RunState.RUNNING
