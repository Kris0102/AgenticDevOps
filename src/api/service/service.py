import asyncio
import uuid
from typing import AsyncIterator, List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from api.requests.requests import RunRequest
from datetime import datetime, timezone

from api.responses.responses import (
    ArtifactContent,
    ArtifactView,
    DiffView,
    EventView,
    HitlReviewContext,
    LessonView,
    LogView,
    MicrotaskRow,
    MicrotaskView,
    PlanView,
    RunExportBundle,
    RunResponse,
    RunResponseWithDetails,
    RunState,
)
from db.db import db
from db.event_log import EventLogger
from db.lessons import LessonStore
from db.models import Artifact as ArtifactRow, MicrotaskRecord, Run
from db.models import Event as EventRow
from Exceptions.RunExecutionError import RunExecutionError
from observability import Category, bind_run, clear_context, configure_logging, get_logger
from observability.log_bus import log_bus
from orchestrator.bootstrap_node import BootStrapper
from orchestrator.checkpointer import setup_checkpointer
from orchestrator.hitl import HitlResume, Phase
from orchestrator.orchestrator import graph
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import BlackboardState, EventType, RunStatus, create_initial_blackboard_state
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


class RunAlreadyExistsError(RuntimeError):
    """import_run() targeted a run_id that is already in this database."""


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
        # run_ids currently being driven by a background worker in THIS process. Lets us
        # tell a genuinely-RUNNING run from a STALLED one (checkpoint non-terminal but no
        # worker driving it — the process died before the _drive guard could FAIL it). The
        # set is empty after a restart, so every previously-running run reads as STALLED and
        # becomes continuable. Single-worker assumption (see class note + ADR 0019).
        self._active: set[str] = set()

    def mark_active(self, run_id: str) -> None:
        """Register a run as being driven NOW. Called synchronously from the route handler
        before the BackgroundTask starts, so the 202 response never briefly reports STALLED."""
        self._active.add(run_id)

    def is_active(self, run_id: str) -> bool:
        return run_id in self._active

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
            self._active.discard(run_id)
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
            self._active.discard(run_id)
            clear_context()

    def continue_run(self, run_id: str) -> RunResponse:
        """Re-drive a STALLED run from its checkpoint. Used when the worker that was driving
        the graph died (uvicorn crash/restart, OOM-kill) leaving a non-terminal checkpoint
        with no pending interrupt. `graph.invoke(None, ...)` is the canonical LangGraph
        resume — no bootstrap (sandbox may be gone, but the graph continues from state; a
        node that needs the sandbox re-creates via the idempotent bootstrapper path) and no
        Command (there is no interrupt to answer). Runs to the next pause or END.

        Distinct from resume(): resume answers a HITL interrupt; continue restarts a graph
        that simply stopped being driven."""
        bind_run(run_id)
        try:
            log_bus.reset(run_id)   # stalled run is producing again — reopen its live stream
            _log.info("run.continue", run_id=run_id)
            self._drive(run_id, None)
            self._finalize(run_id)
            return self._response(run_id)
        finally:
            self._active.discard(run_id)
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
            diff=self._gate_diff(phase, state),
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
            
    def get_runs(self) -> List[RunResponseWithDetails]:
        """All runs for the dashboard home, newest first. One SQL query — the coarse
        run_state is derived from the persisted `status` + the in-memory active set, so this
        does NOT read the checkpoint per run (`_pending_payload` is an expensive per-run
        `graph.get_state`; fine for a single run, not a whole-list scan). The precise
        paused-gate context still comes from GET /runs/{id} when a run is opened."""
        with db.session_scope() as session:
            runs = session.query(Run).order_by(Run.created_at.desc()).all()
            pushed = self._pushed_files_by_run(session)
            return [self._run_details(session, run, pushed.get(run.run_id)) for run in runs]

    def _run_details(self, session, run: Run, pushed_files: Optional[int]) -> RunResponseWithDetails:
        return RunResponseWithDetails(
            run_id=run.run_id,
            run_state=self._list_state(run.status, self.is_active(run.run_id)),
            status=run.status,
            repo_url=run.repo_url,
            repo_name=self._repo_name(run.repo_url),
            base_sha=run.base_sha,
            task_description=self._ensure_task(session, run),
            pushed_files=pushed_files,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    def _ensure_task(self, session, run: Run) -> Optional[str]:
        """The run's task, lazily backfilled: rows created before the task_description
        column existed get it read ONCE from their checkpoint (objective) and persisted,
        so the expensive graph.get_state never repeats for the same run."""
        if run.task_description:
            return run.task_description
        try:
            snapshot = graph.get_state(self._config(run.run_id))
            objective = (snapshot.values or {}).get("objective")
        except Exception:
            return None
        if objective is None:
            return None
        task = f"{getattr(objective, 'title', '')}: {getattr(objective, 'description', '')}".strip(": ")
        if task:
            run.task_description = task   # persisted by the caller's session_scope commit
        return task or None

    @staticmethod
    def _pushed_files_by_run(session) -> dict[str, int]:
        """run_id → file count of its deploy push, from gitops.pushed events (one query
        for the whole home list). Falls back to `steps`-era events without file_count."""
        rows = (
            session.query(EventRow.run_id, EventRow.payload_json)
            .filter(EventRow.event_type == EventType.GITOPS_PUSHED.value)
            .order_by(EventRow.seq.asc())
            .all()
        )
        out: dict[str, int] = {}
        for run_id, payload in rows:
            count = (payload or {}).get("file_count")
            if isinstance(count, int):
                out[run_id] = count   # latest push wins (rows ordered by seq)
        return out

    def lessons(self, run_id: str) -> List[LessonView]:
        """Lessons learned on THIS run's repo (all runs, newest first) — the dashboard's
        'what the system knows about this repo' panel. Repo resolved from the run row."""
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            repo_url = run.repo_url if run else ""
        return [
            LessonView(
                lesson_id=l.lesson_id,
                run_id=l.run_id,
                scope=l.scope,
                failure_type=l.failure_type,
                summary=l.summary,
                guidance=l.guidance,
                created_at=l.created_at,
            )
            for l in LessonStore.list_for_repo(repo_url)
        ]

    def export_run(self, run_id: str) -> Optional[RunExportBundle]:
        """Everything durable about one run in one self-describing JSON bundle (run row +
        full event history + microtasks + artifact bodies). None if the run is unknown."""
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            details = self._run_details(session, run, self._pushed_files_by_run(session).get(run_id))
        return RunExportBundle(
            exported_at=datetime.now(timezone.utc),
            run=details,
            events=self.events(run_id, after=0, limit=100_000),
            microtasks=self.microtasks(run_id),
            artifacts=[c for a in self.artifacts(run_id)
                       if (c := self.artifact(run_id, a.artifact_id)) is not None],
        )

    def import_run(self, bundle: RunExportBundle) -> RunResponseWithDetails:
        """Restore an exported run into THIS database (new machine / wiped DB). Refuses
        when the run_id already exists — import is a restore, not a merge. Events get
        fresh `seq` values (identity column) but keep their original order and event
        timestamps, so cursors and the timeline behave normally."""
        run = bundle.run
        with db.session_scope() as session:
            if session.get(Run, run.run_id) is not None:
                raise RunAlreadyExistsError(f"run {run.run_id} already exists — refusing to overwrite")
            session.add(Run(
                run_id=run.run_id,
                repo_url=run.repo_url,
                base_sha=run.base_sha,
                status=run.status or "FAILED",
                task_description=run.task_description,
                created_at=run.created_at,
                updated_at=run.updated_at,
            ))
            for m in bundle.microtasks:
                session.add(MicrotaskRecord(
                    microtask_id=m.microtask_id, run_id=run.run_id, idx=m.idx,
                    title=m.title, description="", status=m.status, attempts=m.attempts,
                    target_files=m.target_files, files_touched=m.files_touched,
                ))
            for a in bundle.artifacts:
                session.add(ArtifactRow(
                    artifact_id=a.artifact_id, run_id=run.run_id, kind=a.kind,
                    content_text=a.content_text, created_at=a.created_at,
                ))
            for e in sorted(bundle.events, key=lambda e: e.seq):
                # Restored verbatim, NOT via EventLogger — the logger stamps ts=now, which
                # would falsify the imported history. This is the one place Event rows are
                # built directly: it restores existing audit rows, it doesn't log new ones.
                session.add(EventRow(
                    event_id=f"evt_{uuid.uuid4().hex[:12]}",
                    run_id=run.run_id,
                    ts=datetime.fromisoformat(e.ts),
                    actor=e.actor,
                    event_type=e.event_type,
                    payload_json=e.payload,
                ))
        return self.get_run_details(run.run_id)

    def get_run_details(self, run_id: str) -> RunResponseWithDetails:
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise KeyError(run_id)
            return self._run_details(session, run, self._pushed_files_by_run(session).get(run_id))

    @staticmethod
    def _list_state(status: Optional[str], is_active: bool) -> RunState:
        """Coarse lifecycle from the stored status string only (no checkpoint read).
        A run persisted at NEEDS_HITL_APPROVAL is paused; terminal statuses map directly;
        otherwise it's RUNNING if a worker drives it, else STALLED (orphaned)."""
        if status == RunStatus.FAILED.value:
            return RunState.FAILED
        if status == RunStatus.DONE.value:
            return RunState.COMPLETED
        if status == RunStatus.NEEDS_HITL_APPROVAL.value:
            return RunState.PAUSED_HITL
        return RunState.RUNNING if is_active else RunState.STALLED

    @staticmethod
    def _repo_name(repo_url: str) -> str:
        """owner/repo from a git URL (strips scheme + trailing .git); falls back to the raw
        value if it isn't the expected shape."""
        s = (repo_url or "").rstrip("/")
        if s.endswith(".git"):
            s = s[:-4]
        parts = [p for p in s.split("/") if p]
        return "/".join(parts[-2:]) if len(parts) >= 2 else (repo_url or "")

    def artifacts(self, run_id: str) -> List[ArtifactView]:
        """Browse list of the run's durable artifacts (plan/review/diff/test-*/reflection),
        oldest first, WITHOUT bodies — a diff or test log can be tens of KB, so the list
        stays light and the body is fetched per-artifact on open."""
        with db.session_scope() as session:
            rows = (
                session.query(ArtifactRow)
                .filter(ArtifactRow.run_id == run_id)
                .order_by(ArtifactRow.created_at.asc())
                .all()
            )
            return [self._artifact_view(r) for r in rows]

    def artifact(self, run_id: str, artifact_id: str) -> Optional[ArtifactContent]:
        """One artifact with its full body. run_id filter so an id can't read across runs."""
        with db.session_scope() as session:
            row = (
                session.query(ArtifactRow)
                .filter(ArtifactRow.run_id == run_id, ArtifactRow.artifact_id == artifact_id)
                .one_or_none()
            )
            if row is None:
                return None
            base = self._artifact_view(row)
            return ArtifactContent(**base.model_dump(), content_text=row.content_text or "")

    def microtasks(self, run_id: str) -> List[MicrotaskRow]:
        """The run's microtask board from the durable microtasks table — status/attempts
        per task, ordered by idx. Works for finished runs (unlike the HITL PlanView,
        which only exists while paused at a gate)."""
        with db.session_scope() as session:
            rows = (
                session.query(MicrotaskRecord)
                .filter(MicrotaskRecord.run_id == run_id)
                .order_by(MicrotaskRecord.idx.asc())
                .all()
            )
            return [
                MicrotaskRow(
                    microtask_id=r.microtask_id,
                    idx=r.idx,
                    title=r.title,
                    status=r.status,
                    attempts=r.attempts,
                    target_files=list(r.target_files or []),
                    files_touched=list(r.files_touched or []),
                )
                for r in rows
            ]

    @staticmethod
    def _artifact_view(row: ArtifactRow) -> ArtifactView:
        text = row.content_text or ""
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return ArtifactView(
            artifact_id=row.artifact_id,
            kind=row.kind,
            created_at=row.created_at,
            size=len(text),
            preview=first_line[:120],
        )

    # ----------------------------------------------------------------- helpers

    def _config(self, run_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": run_id}}

    def _drive(self, run_id: str, graph_input) -> None:
        """One invoke == run until the next interrupt() or END (LangGraph batches all
        intervening supersteps). Headless: no in-process HITL prompt loop.

        Safety net: any exception a node did NOT convert to a clean FAILED escapes
        `graph.invoke` here. Without this guard it would unwind through the background
        worker and leave `Run.status` non-terminal — the run would look stuck forever.
        HITL pauses do NOT hit this: `interrupt()` suspends and `invoke` returns normally
        (LangGraph swallows its internal GraphInterrupt), so catching Exception is safe."""
        try:
            graph.invoke(graph_input, config=self._config(run_id))
        except Exception as exc:
            _log.error("graph.uncaught", run_id=run_id, error=str(exc), exc_info=True)
            self._force_failed(run_id, f"Uncaught {type(exc).__name__}: {exc}")

    def _force_failed(self, run_id: str, reason: str) -> None:
        """Terminal FAILED bookkeeping for a crash no node handled. Reuses
        RunFailureHandler so the runs-table status + RUN_FAILED events match every other
        failure path. Pulls the last checkpointed state for the `from` status; tolerates
        that read failing too (best-effort — the point is to reach a terminal status)."""
        try:
            snapshot = graph.get_state(self._config(run_id))
            state: BlackboardState = snapshot.values if snapshot else {}  # type: ignore[assignment]
        except Exception:
            state = {}  # type: ignore[assignment]  # handle() does state.get(...) — must not be None
        with db.session_scope() as session:
            RunFailureHandler.handle(
                session, run_id, Roles.ORCHESTRATOR.value, state,
                RunExecutionError(phase="graph", reason=reason),
            )

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
            run_state=self._derive_state(status, pending, self.is_active(run_id)),
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
                    acceptance_criteria=[c.render() for c in m.acceptance_criteria],
                    target_files=m.target_files,
                    status=str(m.status),
                )
                for m in microtasks
            ],
        )

    def _gate_diff(self, phase: Optional[str], state: dict) -> Optional[DiffView]:
        """Which diff a gate shows:
        - code   → the current microtask's diff (focused, per-step review)
        - deploy → the FULL cumulative diff about to be pushed (holistic, end review)
        Both read `evidence.latest_diff`: the coder's `git diff` is the whole working
        tree, nothing commits before the deploy gate, so at deploy `latest_diff` already
        spans every microtask. Only the framing label differs."""
        if phase == Phase.CODE.value:
            return self._diff_view(state)
        if phase == Phase.DEPLOY.value:
            return self._diff_view(state, summary="Full cumulative diff to be pushed")
        return None

    def _diff_view(self, state: dict, summary: Optional[str] = None) -> Optional[DiffView]:
        evidence = state.get("evidence")
        latest = evidence.latest_diff if evidence else None
        if latest is None:
            return None
        return DiffView(
            microtask_id=state.get("current_microtask_id"),
            summary=summary or latest.summary,
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
    def _derive_state(status, pending: Optional[dict], is_active: bool) -> RunState:
        """Coarse lifecycle derived from the blackboard status — single source of
        truth, no parallel state field. `is_active` = a worker in THIS process is driving
        the graph right now; it separates RUNNING from STALLED (same non-terminal status,
        but STALLED has no driver — the process died before the _drive guard could FAIL it)."""
        if pending is not None:
            return RunState.PAUSED_HITL
        if status == RunStatus.FAILED:
            return RunState.FAILED
        if status == RunStatus.DONE:
            return RunState.COMPLETED
        if is_active:
            return RunState.RUNNING
        return RunState.STALLED
