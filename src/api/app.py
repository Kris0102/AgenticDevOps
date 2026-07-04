import uuid
from typing import List

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from api.requests.requests import RunRequest
from api.responses.responses import (
    ArtifactContent,
    ArtifactView,
    EventView,
    HitlReviewContext,
    LessonView,
    LogView,
    MicrotaskRow,
    RunExportBundle,
    RunResponse,
    RunResponseWithDetails,
    RunState,
)
from api.service.service import RunAlreadyExistsError, Service
from orchestrator.hitl import HitlDecision, HitlResume

app = FastAPI(title="AgenticDevOps Control API")
service = Service()


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=RunResponse)
def create_run(request: RunRequest, background: BackgroundTasks) -> RunResponse:
    """Start a run. Returns immediately with the run_id; the graph executes in the
    background (bootstrap + planning can take minutes). Poll GET /runs/{id} for state."""
    run_id = str(uuid.uuid4())
    service.mark_active(run_id)   # sync, before the task starts → poll never sees STALLED
    background.add_task(service.run, request, run_id)
    return RunResponse(run_id=run_id, run_state=RunState.RUNNING)


@app.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str) -> RunResponse:
    """Poll target: current lifecycle state + pending HITL payload (if any)."""  # We will run this method to check whether the run is paused at a HTIL gate!
    return service.status(run_id)


@app.get("/runs/{run_id}/hitl", response_model=HitlReviewContext)
def get_hitl(run_id: str) -> HitlReviewContext:
    """What the human reviews at the gate: plan + microtasks for a `plan` gate, the
    raw green/red diff for a `code` gate. 404 when the run isn't paused at a gate."""
    context = service.review_context(run_id)
    if context is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run is not paused at a HITL gate")
    return context

@app.post("/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED, response_model=RunResponse)
def resume_run(run_id: str, decision: HitlResume, background: BackgroundTasks) -> RunResponse:
    """Apply a human verdict (approve/reject) and resume. Validated up front so a bad
    decision fails fast; the graph then advances in the background. Poll GET /runs/{id}."""
    if not service.is_paused(run_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "run is not paused at a HITL gate")
    if decision.decision == HitlDecision.REJECT and not decision.note:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "reject requires a note")
    service.mark_active(run_id)   # sync, before the task starts → poll never sees STALLED
    background.add_task(service.resume, run_id, decision)
    return RunResponse(run_id=run_id, run_state=RunState.RUNNING)


@app.post("/runs/{run_id}/continue", status_code=status.HTTP_202_ACCEPTED, response_model=RunResponse)
def continue_run(run_id: str, background: BackgroundTasks) -> RunResponse:
    """Re-drive a STALLED run (worker died mid-graph, checkpoint left non-terminal with no
    pending interrupt). Distinct from /resume, which answers a HITL gate. Rejected if the
    run is terminal, paused at a gate (use /resume), or already being driven."""
    current = service.status(run_id)
    if current.run_state in (RunState.COMPLETED, RunState.FAILED):
        raise HTTPException(status.HTTP_409_CONFLICT, "run is already terminal")
    if current.run_state == RunState.PAUSED_HITL:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is paused at a HITL gate — use /resume")
    if current.run_state == RunState.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is already being driven")
    service.mark_active(run_id)   # sync, before the task starts → poll never sees STALLED
    background.add_task(service.continue_run, run_id)
    return RunResponse(run_id=run_id, run_state=RunState.RUNNING)


@app.get("/runs/{run_id}/events", response_model=List[EventView])
def get_events(run_id: str, after: int = 0, limit: int = 200) -> List[EventView]:
    """Durable history / timeline: domain events with seq > `after`, oldest first.
    Survives restarts. Page by passing the last seq you saw back as `after`. Poll it."""
    return service.events(run_id, after=after, limit=limit)


@app.get("/runs/{run_id}/logs", response_model=List[LogView])
def get_logs(run_id: str, after: int = 0) -> List[LogView]:
    """Polled fallback for the live log (in-memory, ephemeral). Prefer the SSE stream."""
    return service.logs(run_id, after=after)


@app.get("/runs/{run_id}/logs/stream")
async def stream_logs(run_id: str, after: int = 0) -> StreamingResponse:
    """Live log over SSE — drains the in-memory structlog bus for this run. Ephemeral
    (live only); the durable record is GET /events. Closes when the run finishes."""
    return StreamingResponse(
        service.stream_logs(run_id, after=after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # tell nginx not to buffer the stream
        },
    )
    
@app.get("/runs", response_model=List[RunResponseWithDetails])
def get_runs() -> List[RunResponseWithDetails]:
    """All previous runs, newest first — the dashboard home list."""
    return service.get_runs()


@app.get("/runs/{run_id}/artifacts", response_model=List[ArtifactView])
def get_artifacts(run_id: str) -> List[ArtifactView]:
    """Durable artifact list (plan/review/diff/test-*/reflection) without bodies."""
    return service.artifacts(run_id)


@app.get("/runs/{run_id}/artifacts/{artifact_id}", response_model=ArtifactContent)
def get_artifact(run_id: str, artifact_id: str) -> ArtifactContent:
    """One artifact with its full body — fetched when the user opens it."""
    content = service.artifact(run_id, artifact_id)
    if content is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found for this run")
    return content


@app.get("/runs/{run_id}/microtasks", response_model=List[MicrotaskRow])
def get_microtasks(run_id: str) -> List[MicrotaskRow]:
    """The run's microtask board (durable) — status + attempts per task, by idx."""
    return service.microtasks(run_id)


@app.get("/runs/{run_id}/lessons", response_model=List[LessonView])
def get_lessons(run_id: str) -> List[LessonView]:
    """Lessons learned on this run's REPO across all runs (ADR 0018), newest first."""
    return service.lessons(run_id)


@app.get("/runs/{run_id}/export")
def export_run(run_id: str) -> Response:
    """Download the run's full durable record as a self-describing JSON bundle. The
    filename carries run_id + export timestamp; the body carries the run_id too, so an
    import always lands on the correct run."""
    bundle = service.export_run(run_id)
    if bundle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    stamp = bundle.exported_at.strftime("%Y%m%d-%H%M%S")
    filename = f"agentic_run_{run_id}_{stamp}.json"
    return Response(
        content=bundle.model_dump_json(indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/runs/import", status_code=status.HTTP_201_CREATED, response_model=RunResponseWithDetails)
def import_run(bundle: RunExportBundle) -> RunResponseWithDetails:
    """Restore an exported bundle into this database (fresh machine / wiped DB).
    409 when the run_id already exists — import is a restore, never an overwrite."""
    try:
        return service.import_run(bundle)
    except RunAlreadyExistsError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
