import uuid
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response, status
from fastapi.responses import StreamingResponse

from api.requests.requests import ContinueRunRequest, RepoCreateRequest, RepoUpdateRequest, RunRequest
from api.responses.responses import (
    ArtifactContent,
    ArtifactView,
    BranchListResponse,
    EventView,
    HitlReviewContext,
    LessonView,
    LogView,
    MicrotaskRow,
    RepoView,
    RunExportBundle,
    RunResponse,
    RunResponseWithDetails,
    RunState,
)
from api.service.service import (
    InvalidTokenBudgetError,
    ProviderUnreachableError,
    RepoValidationFailedError,
    RunAlreadyExistsError,
    Service,
)
from db.repo_store import DuplicateRepoError, RepoNotFoundError
from gitops.github_api import BranchListError, RepoValidationKind, RepoValidationResult
from orchestrator.hitl import HitlDecision, HitlResume
from orchestrator.state import AppSettings
from api.settings_schema import SETTINGS_SCHEMA

app = FastAPI(title="AgenticDevOps Control API")
service = Service()

# Repo-validation kind → HTTP status (mirrors the ProviderUnreachableError mapping).
_REPO_KIND_STATUS = {
    RepoValidationKind.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RepoValidationKind.NO_ACCESS: status.HTTP_401_UNAUTHORIZED,
    RepoValidationKind.NO_PUSH_ACCESS: status.HTTP_403_FORBIDDEN,
    RepoValidationKind.NO_PR_ACCESS: status.HTTP_403_FORBIDDEN,
    RepoValidationKind.UNREACHABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    RepoValidationKind.UNEXPECTED: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _repo_validation_http(e: RepoValidationFailedError) -> HTTPException:
    code = _REPO_KIND_STATUS.get(e.kind, status.HTTP_503_SERVICE_UNAVAILABLE)
    return HTTPException(code, f"repo not usable ({e.kind.value}): {e.reason}")


@app.post("/runs", status_code=status.HTTP_202_ACCEPTED, response_model=RunResponse)
def create_run(request: RunRequest, background: BackgroundTasks) -> RunResponse:
    """Start a run. **Preflights the repo AND the LLM provider first** — an invalid/
    inaccessible repo or a dead/mis-keyed provider is rejected here and NO run is
    created. On success returns immediately with the run_id; the graph executes in
    the background. Poll GET /runs/{id} for state."""
    try:
        resolved = service.resolve_repo(request)
    except RepoNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo_id not found in the registry")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except RepoValidationFailedError as e:
        raise _repo_validation_http(e)
    request = request.model_copy(update={"repo": resolved})
    try:
        config = service.resolve_and_preflight(request)
    except ProviderUnreachableError as e:
        code = status.HTTP_401_UNAUTHORIZED if e.kind == "auth" else status.HTTP_503_SERVICE_UNAVAILABLE
        raise HTTPException(code, f"LLM provider not usable ({e.kind}): {e.reason}")
    run_id = str(uuid.uuid4())
    service.mark_active(run_id)   # sync, before the task starts → poll never sees STALLED
    background.add_task(service.run, request, run_id, config)
    return RunResponse(run_id=run_id, run_state=RunState.RUNNING)


@app.get("/settings", response_model=AppSettings)
def get_settings() -> AppSettings:
    """Global settings (API keys masked)."""
    return service.get_settings()


@app.put("/settings", response_model=AppSettings)
def put_settings(new: AppSettings) -> AppSettings:
    """Save global settings. A provider key left as the mask keeps its stored value."""
    return service.update_settings(new)


@app.get("/settings/schema")
def get_settings_schema() -> list:
    """Per-field metadata (label/description/group) that drives the settings form + info
    tooltips — docs live here, next to nothing else."""
    return SETTINGS_SCHEMA


@app.post("/settings/providers/{provider_id}/test")
def test_provider(provider_id: str) -> dict:
    """Health-probe a stored provider ('Test connection' button)."""
    return service.test_provider(provider_id)


@app.post("/settings/github/test")
def test_github() -> dict:
    """Validate the stored GitHub token against api.github.com ('Test connection')."""
    return service.test_github()


# ------------------------------------------------------------------- repo registry
# GH #41 — repos are added/managed here, then picked (not typed) on the new-run form.

@app.get("/repos", response_model=List[RepoView])
def list_repos() -> List[RepoView]:
    """All registered repos, tokens masked."""
    return service.list_repos()


@app.post("/repos", status_code=status.HTTP_201_CREATED, response_model=RepoView)
def create_repo(request: RepoCreateRequest) -> RepoView:
    """Register a repo: URL canonicalized, then validated against GitHub with the
    provided (else global) token — only a passing repo is stored."""
    try:
        return service.add_repo(request)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    except DuplicateRepoError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, f"repo already registered: {e}")
    except RepoValidationFailedError as e:
        raise _repo_validation_http(e)


@app.put("/repos/{repo_id}", response_model=RepoView)
def update_repo(repo_id: str, request: RepoUpdateRequest) -> RepoView:
    """Edit a registered repo. A token left as the mask keeps its stored value; a
    re-typed token is re-validated immediately."""
    try:
        return service.update_repo(repo_id, request)
    except RepoNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


@app.delete("/repos/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repo(repo_id: str) -> Response:
    """Remove a repo from the registry. Past runs keep their repo_url snapshot."""
    try:
        service.delete_repo(repo_id)
    except RepoNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/repos/{repo_id}/validate", response_model=RepoValidationResult)
def validate_repo(repo_id: str) -> RepoValidationResult:
    """Re-validate a repo ('Test connection' button) — updates the stored status dot."""
    try:
        return service.validate_repo(repo_id)
    except RepoNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")


@app.get("/repos/{repo_id}/branches", response_model=BranchListResponse)
def repo_branches(repo_id: str) -> BranchListResponse:
    """Live GitHub branch list for the base-branch picker — never cached."""
    try:
        return BranchListResponse(branches=service.repo_branches(repo_id))
    except RepoNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repo not found")
    except BranchListError as e:
        code = _REPO_KIND_STATUS.get(e.kind, status.HTTP_503_SERVICE_UNAVAILABLE)
        raise HTTPException(code, f"branch list failed ({e.kind.value}): {e.reason}")


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
def continue_run(run_id: str, background: BackgroundTasks, body: Optional[ContinueRunRequest] = None) -> RunResponse:
    """Re-drive a STALLED run (worker died mid-graph, checkpoint left non-terminal with no
    pending interrupt — or a token-budget stop, ADR 0025) from its checkpoint. Distinct from
    /resume, which answers a HITL gate. Rejected if the run is terminal, paused at a gate
    (use /resume), or already being driven.

    Optional `body.additional_tokens` ADDS that many tokens to the checkpointed
    Constraints.max_total_tokens BEFORE re-driving (ADR 0025 amendment) — the follow-up to a
    token-budget stall, so /continue can actually raise the cap instead of immediately
    re-tripping the same check. Validated synchronously here so a bad value 422s the request
    instead of silently re-stalling in the background task."""
    current = service.status(run_id)
    if current.run_state in (RunState.COMPLETED, RunState.FAILED):
        raise HTTPException(status.HTTP_409_CONFLICT, "run is already terminal")
    if current.run_state == RunState.PAUSED_HITL:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is paused at a HITL gate — use /resume")
    if current.run_state == RunState.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is already being driven")
    if current.stalled_kind == "token_budget" and (body is None or body.additional_tokens is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                             "additional_tokens is required to continue a token-budget stall")
    if body and body.additional_tokens is not None:
        try:
            service.raise_token_budget(run_id, body.additional_tokens)
        except InvalidTokenBudgetError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))
    service.mark_active(run_id)   # sync, before the task starts → poll never sees STALLED
    background.add_task(service.continue_run, run_id)
    return RunResponse(run_id=run_id, run_state=RunState.RUNNING)


@app.post("/runs/{run_id}/fail", response_model=RunResponse)
def fail_run(run_id: str, body: Optional[dict] = None) -> RunResponse:
    """Human verdict on a STALLED run: mark it FAILED ('Mark as failed'). Only valid on a
    stalled run — running/paused/terminal runs are rejected. Optional {note} lands in the
    run.failed reason."""
    current = service.status(run_id)
    if current.run_state != RunState.STALLED:
        raise HTTPException(status.HTTP_409_CONFLICT, f"run is {current.run_state.value}, not stalled")
    return service.give_up(run_id, (body or {}).get("note"))


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
