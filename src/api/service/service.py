import asyncio
import threading
import time
import uuid
from typing import AsyncIterator, List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from Exceptions.RetriableError import RetriableError, is_transient_llm_error
from Exceptions.RunStoppedError import RunStoppedError
from Exceptions.TokenBudgetExceededError import TokenBudgetExceededError

from api.requests.requests import RepoCreateRequest, RepoUpdateRequest, RunRequest
from datetime import datetime, timezone

from api.responses.responses import (
    AgentModelView,
    ArtifactContent,
    ArtifactView,
    DiffView,
    EventView,
    HitlReviewContext,
    LessonView,
    LogView,
    MicrotaskProgress,
    MicrotaskRow,
    MicrotaskView,
    PipelineProgress,
    PlanView,
    ProviderOption,
    RepoView,
    CostView,
    PhaseCostView,
    RunConfigView,
    RunCostView,
    RunExportBundle,
    RunResponse,
    RunResponseWithDetails,
    RunState,
)
from db.db import db
from db.event_log import EventLogger
from db.lessons import LessonStore
from db.semanticMemory.semantic_store import SemanticStore
from db.models import Artifact as ArtifactRow, MicrotaskRecord, Run, Sandbox
from db.models import Event as EventRow
from db.repo_store import MASK, RepoEntry, RepoStore, normalize_repo_url
from db.settings_store import SettingsStore, resolve_run_config
from gitops.github_api import GitHubRepoClient, RepoValidationKind, RepoValidationResult
from providers import cost_for, health_check, probe_cache
from Exceptions.RunExecutionError import RunExecutionError
from observability import Category, bind_run, clear_context, configure_logging, get_logger
from observability.log_bus import log_bus
from orchestrator.bootstrap_node import BootStrapper
from sandbox.sandbox_tools import DockerSandboxTools
from orchestrator.checkpointer import checkpointer, setup_checkpointer
from orchestrator.hitl import HitlResume, Phase
from orchestrator.orchestrator import graph
from orchestrator.run_control import clear_stop, request_stop
from orchestrator.run_failure import RunFailureHandler
from orchestrator.state import (
    AppSettings,
    BlackboardState,
    EventType,
    Iteration,
    LLMConfig,
    MicrotaskStatus,
    RunConfig,
    RunStatus,
    Repo,
    create_initial_blackboard_state,
)
from tool_gateway.Roles import Roles

# How often the live-log SSE drains the in-memory bus. Localhost + in-memory, so this
# can be tight; 0.5s is imperceptible and cheap.
_LOG_STREAM_POLL_SECONDS = 0.5
# Keys promoted to LogView columns; everything else on a log line goes into `fields`.
_LOG_PROMOTED_KEYS = {"timestamp", "level", "category", "event", "run_id"}
_PRIMITIVE = (str, int, float, bool, type(None))

# Raw Run.status string for a manually-stopped run — deliberately NOT a RunStatus enum
# member (that enum is deeply threaded through node routing; a stopped run never
# re-enters the graph, so it doesn't need a routable value). See ADR 0033.
STOPPED_STATUS = "STOPPED"

_log = get_logger(Category.ORCHESTRATOR)


class HitlNotPausedError(RuntimeError):
    """resume() was called on a run that isn't suspended at a HITL gate."""


class RunAlreadyExistsError(RuntimeError):
    """import_run() targeted a run_id that is already in this database."""


class RunNotFoundError(RuntimeError):
    """stop_run()/delete_run() targeted a run_id with no matching `runs` row."""


class RunAlreadyTerminalError(RuntimeError):
    """stop_run() was called on a run that's already completed/failed/stopped."""


class RunStopTimeoutError(RuntimeError):
    """stop_run() waited past `_STOP_WAIT_TIMEOUT_SECONDS` for the driver thread to exit.
    Safe to retry — the thread is guaranteed to clear on its own shortly via its own
    configured LLM timeout (see ADR 0033)."""
    def __init__(self, run_id: str):
        super().__init__(f"timed out waiting for run {run_id}'s driver thread to exit")


class RunNotDeletableError(RuntimeError):
    """delete_run() was called on a run that's still RUNNING/PAUSED_HITL — must be
    stopped first (ADR 0033)."""


class InvalidModelChangeError(ValueError):
    """A PATCH /runs/{id}/model request is invalid — unknown agent, provider not in settings,
    empty model, terminal run, or no checkpointed config to patch (GH #64)."""


class InvalidTokenBudgetError(ValueError):
    """A /continue request's `additional_tokens` doesn't make sense against the run's
    checkpointed state (ADR 0025) — no checkpointed Constraints to patch, a non-positive
    amount, or a resulting cap at/below tokens already spent (would just immediately re-trip
    `token_budget_exceeded` and re-stall the instant the node re-enters)."""


class ProviderUnreachableError(RuntimeError):
    """LLM preflight failed — the run must not start. `kind` = auth | unreachable | timeout."""
    def __init__(self, kind: str, reason: str):
        self.kind = kind
        self.reason = reason
        super().__init__(f"[{kind}] {reason}")


class RepoValidationFailedError(RuntimeError):
    """Repo validation failed (add / run preflight) — carries the GitHub-derived kind
    so the route maps it to an HTTP status like the LLM preflight does."""
    def __init__(self, kind: RepoValidationKind, reason: str):
        self.kind = kind
        self.reason = reason
        super().__init__(f"[{kind.value}] {reason}")


class Service:
    """Headless run engine behind the HTTP control API. Mirrors RunLauncher (main.py)
    minus the interactive CLI: a single `graph.invoke()` drives the graph to the next
    `interrupt()` or END. So `run` advances to the first HITL gate (or completion), and
    each `resume` advances exactly one gate further — one HTTP round-trip per gate.

    All durable state lives in the LangGraph checkpoint, keyed by thread_id=run_id;
    this class holds no per-run state and is safe to construct once at app startup.

    CONCURRENCY (ADR 0032): multiple runs can execute at once. Each run drives the graph in
    its own BackgroundTask thread; the checkpointer is backed by a psycopg CONNECTION POOL
    (orchestrator/checkpointer.py) so concurrent `graph.invoke`s — and a `get_state` poll
    racing a run's superstep write — each borrow their own connection instead of colliding on
    one. SQLAlchemy already pools; `log_bus` is per-run + locked; structlog run_id binding is a
    per-task contextvar; `_active` is lock-guarded; each run owns its own sandbox container.
    A run's work is single-threaded (one worker per run_id, enforced by `_active`) — we
    parallelize ACROSS runs, never within one thread_id.
    """

    def __init__(self) -> None:
        configure_logging()      # reads LOG_LEVEL / LOG_JSON from env
        setup_checkpointer()     # idempotent — ensures checkpoint tables exist
        # run_ids currently being driven by a background worker in THIS process. Lets us
        # tell a genuinely-RUNNING run from a STALLED one (checkpoint non-terminal but no
        # worker driving it — the process died before the _drive guard could FAIL it). The
        # set is empty after a restart, so every previously-running run reads as STALLED and
        # becomes continuable (ADR 0019). Guarded by a lock: with concurrent runs it's
        # mutated from several background-worker threads and read from the API event loop.
        self._active: set[str] = set()
        self._active_lock = threading.Lock()
        self._github = GitHubRepoClient()

    def mark_active(self, run_id: str) -> bool:
        """Atomically CLAIM the run for driving. Returns True if the caller now owns it, False if
        a worker is ALREADY driving it — the caller must then refuse (a second concurrent driver
        on the same thread_id would run two `graph.invoke`s over one checkpoint and corrupt it).
        Test-and-set under the lock. Called synchronously from the route before the BackgroundTask
        starts, so the 202 response never briefly reports STALLED."""
        with self._active_lock:
            if run_id in self._active:
                return False
            self._active.add(run_id)
            return True

    def mark_inactive(self, run_id: str) -> None:
        """Release a run's active claim. Called from each drive's `finally`, and by a route
        that claimed the run but then bailed before scheduling the worker (so it doesn't stay
        stuck reading RUNNING with no driver)."""
        with self._active_lock:
            self._active.discard(run_id)

    def is_active(self, run_id: str) -> bool:
        with self._active_lock:
            return run_id in self._active

    def resolve_and_preflight(self, runRequest: RunRequest | None) -> RunConfig:
        """Resolve the run's effective config (per-run override › global › env) and PROBE the
        provider before anything starts. Raises ProviderUnreachableError on a dead/mis-keyed
        provider so the route can reject with the reason — no run is created. Fail fast."""
        config = resolve_run_config(override=runRequest.llm if runRequest else None)
        # Per-agent models (GH #71): probe every DISTINCT (provider, model) pair actually
        # used, deduped — a mis-keyed provider on ANY agent must fail fast before a run
        # is created, not mid-run on the first agent that touches it.
        seen: set = set()
        for agent in config.llm.agents():
            key = (agent.provider_id, agent.model)
            if key in seen:
                continue
            seen.add(key)
            provider = config.provider_for(agent)
            result = health_check(provider, agent)
            if not result.ok:
                raise ProviderUnreachableError(
                    result.kind or "unreachable",
                    f"{agent.name} provider '{agent.provider_id}' ({agent.model}): "
                    f"{result.reason or 'provider unreachable'}")
            _log.info("run.preflight_ok", agent=agent.name, provider=agent.provider_id,
                      model=agent.model, latency_ms=result.latency_ms)
        return config

    # --------------------------------------------------------------- public API

    def run(self, request: RunRequest, run_id: str, config: Optional[RunConfig] = None) -> RunResponse:
        """Start a fresh run: bootstrap the environment, then drive the graph to the
        first HITL pause or to completion. `run_id` is minted by the caller so the
        HTTP layer can return it before this (background) work finishes. `config` is the
        effective RunConfig resolved+preflighted by the route; falls back to global if absent."""
        bind_run(run_id)
        try:
            # The route resolved `repo_id`/legacy input into a concrete Repo via
            # resolve_repo() before dispatching here — a bare repo_id must never
            # reach the blackboard.
            repo = request.repo
            if repo is None:
                raise RunExecutionError(phase="run.start",
                                        reason="RunRequest.repo not resolved — route must call resolve_repo() first")
            _log.info("run.start", repo_url=repo.repo_url, base_sha=repo.base_sha)
            state = create_initial_blackboard_state(
                run_id=run_id,
                trigger=request.trigger,
                repo=repo,
                objective=request.objective,
                constraints=request.constraints,
                policy=request.policy,
                hitl=request.hitl,
                config=config or resolve_run_config(override=request.llm),
                status=RunStatus.NEEDS_BOOTSTRAP,
                deploy_commit_message=request.commit_message,
                skip_deploy=request.skip_deploy,
            )
            bootstrap = self._bootstrap(run_id, state)
            if bootstrap.get("status") == RunStatus.FAILED:
                _log.error("run.bootstrap_failed", detail="not invoking graph")
                return self._response(run_id)
            self._drive(run_id, {**state, **bootstrap})
            self._finalize(run_id)
            return self._response(run_id)
        finally:
            self.mark_inactive(run_id)
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
            self.mark_inactive(run_id)
            clear_context()

    def raise_token_budget(self, run_id: str, additional_tokens: int) -> None:
        """ADD `additional_tokens` to the checkpointed `Constraints.max_total_tokens` (ADR
        0025 amendment — this is an INCREMENT, not a new absolute cap): continuing a
        token-budget stall unchanged just immediately re-trips `token_budget_exceeded` and
        re-stalls, so the human needs a way to actually raise the cap before `/continue`
        re-drives. Called synchronously from the route BEFORE `continue_run` is scheduled in
        the background, so a bad value 422s the HTTP request instead of silently re-stalling
        inside the background task.

        Also mirrors the new cap onto the denormalized `runs.max_total_tokens` column (same
        precedent as `bootstrap_node._upsert_run_record` / `token_budget.record_usage`) so
        `RunResponseWithDetails`/the dashboard tile reflect it without waiting on another
        checkpoint write, and emits a durable audit event."""
        if additional_tokens <= 0:
            raise InvalidTokenBudgetError("additional_tokens must be a positive integer")
        snapshot = graph.get_state(self._config(run_id))
        values = snapshot.values or {}
        constraints = values.get("constraints")
        if constraints is None:
            raise InvalidTokenBudgetError(f"run {run_id} has no checkpointed constraints to patch")
        iteration = values.get("iteration")
        used = iteration.tokens_used_total if iteration else 0
        old_max_total_tokens = constraints.max_total_tokens
        new_max_total_tokens = (old_max_total_tokens or 0) + additional_tokens
        if new_max_total_tokens <= used:
            raise InvalidTokenBudgetError(
                f"adding {additional_tokens} tokens brings the cap to {new_max_total_tokens}, "
                f"still at/below tokens already used ({used}) — enter a larger amount"
            )
        patched = constraints.model_copy(update={"max_total_tokens": new_max_total_tokens})
        graph.update_state(self._config(run_id), {"constraints": patched})
        with db.session_scope() as session:
            run_record = session.get(Run, run_id)
            if run_record:
                run_record.max_total_tokens = new_max_total_tokens
            EventLogger.log(session, run_id, Roles.HUMAN.value, EventType.RUN_TOKEN_BUDGET_RAISED, {
                "old_max_total_tokens": old_max_total_tokens,
                "new_max_total_tokens": new_max_total_tokens,
                "additional_tokens": additional_tokens,
                "tokens_used_total": used,
            })
        _log.info("run.token_budget_raised", run_id=run_id, new_cap=new_max_total_tokens,
                  additional_tokens=additional_tokens, used=used)

    # ------------------------------------------------------- run settings (GH #64)
    _LLM_AGENTS = ("planner", "reviewer", "coder", "tester", "reflector")

    def _run_config(self, run_id: str) -> Optional[RunConfig]:
        """The run's effective RunConfig from the checkpoint, coerced back to the model (the
        LangGraph serde usually re-hydrates it, but a legacy/stale blob can come back as a
        dict — validate to be safe)."""
        values = graph.get_state(self._config(run_id)).values or {}
        raw = values.get("config")
        if raw is None:
            return None
        return raw if isinstance(raw, RunConfig) else RunConfig.model_validate(raw)
    
    def _run_iteration(self, run_id: str) -> Optional[Iteration]:
        """The run's checkpointed Iteration (loop counters), coerced back to the model. None
        before the graph has produced one (mid-bootstrap)."""
        it = (graph.get_state(self._config(run_id)).values or {}).get("iteration")
        if it is None:
            return None
        return it if isinstance(it, Iteration) else Iteration.model_validate(it)

    def run_config_view(self, run_id: str) -> RunConfigView:
        """The run-settings view: per-agent models (editable), providers for the pickers, and
        the read-only knobs / constraints / gates the run is executing under."""
        config = self._run_config(run_id)
        if config is None:
            raise InvalidModelChangeError(f"run {run_id} has no checkpointed config")
        values = graph.get_state(self._config(run_id)).values or {}
        constraints = values.get("constraints")
        hitl = values.get("hitl")
        settings = SettingsStore.get()  # backend-internal; providers listed WITHOUT secrets below
        run_state = self.status(run_id).run_state
        # Models: changeable any time the run isn't terminal (incl. while running — GH #64).
        editable = run_state not in (RunState.COMPLETED, RunState.FAILED)
        # Knobs + loop-caps: only when there's no live driver to race — a stalled or gated run.
        expert_editable = run_state in (RunState.STALLED, RunState.PAUSED_HITL)
        return RunConfigView(
            models=[
                AgentModelView(agent=a, provider_id=getattr(config.llm, a).provider_id,
                               model=getattr(config.llm, a).model)
                for a in self._LLM_AGENTS
            ],
            providers=[ProviderOption(id=p.id, label=p.label, models=list(p.models))
                       for p in settings.providers],
            knobs=config.knobs.model_dump(),
            constraints={
                "max_planner_reviewer_iterations": constraints.max_planner_reviewer_iterations if constraints else None,
                "max_microtask_attempts": constraints.max_microtask_attempts if constraints else None,
                "max_total_tokens": constraints.max_total_tokens if constraints else None,
            },
            hitl_checkpoints=[str(c) for c in (hitl.hitl_checkpoints if hitl else [])],
            editable=editable,
            expert_editable=expert_editable,
        )

    # Loop-caps editable via the generic config patch (max_total_tokens has its own additive
    # /continue path, ADR 0025 — deliberately excluded to keep one owner).
    _EDITABLE_CONSTRAINTS = ("max_planner_reviewer_iterations", "max_microtask_attempts")
    # Hard ceiling per editable cap so a fat-fingered value can't effectively disable the guard.
    _CONSTRAINT_CEILINGS = {
        "max_planner_reviewer_iterations": 100,
        "max_microtask_attempts": 50,
    }

    def patch_run_config(self, run_id: str, field: str, value: int) -> None:
        """Edit ONE knob or loop-cap on a STALLED/paused run (recovery console, GH #64
        follow-up). Patches the checkpointed RunConfig.knobs or Constraints via
        graph.update_state — read by the relevant agent on its next node, same mechanism as
        set_agent_model. Refused while the run is actively RUNNING (the driver would race the
        patch) or terminal (frozen). `field` = 'knobs.<name>' | 'constraints.<cap>'."""
        if value <= 0:
            raise InvalidModelChangeError("value must be a positive integer")
        if self.status(run_id).run_state not in (RunState.STALLED, RunState.PAUSED_HITL):
            raise InvalidModelChangeError(
                "config can only be edited while the run is stalled or paused at a gate")

        config = self._run_config(run_id)
        if config is None:
            raise InvalidModelChangeError(f"run {run_id} has no checkpointed config to patch")

        group, _, name = field.partition(".")
        if group == "knobs":
            if name not in config.knobs.model_fields:
                raise InvalidModelChangeError(f"unknown knob '{name}'")
            old = getattr(config.knobs, name)
            patched = config.model_copy(update={"knobs": config.knobs.model_copy(update={name: value})})
            graph.update_state(self._config(run_id), {"config": patched})
        elif group == "constraints":
            if name not in self._EDITABLE_CONSTRAINTS:
                raise InvalidModelChangeError(f"constraint '{name}' is not editable here")
            constraints = graph.get_state(self._config(run_id)).values.get("constraints")
            if constraints is None:
                raise InvalidModelChangeError(f"run {run_id} has no checkpointed constraints to patch")
            old = getattr(constraints, name)
            self._verify_constraint_value(
                name, value, self._run_iteration(run_id), self._current_microtask_attempts(run_id))
            graph.update_state(self._config(run_id),
                               {"constraints": constraints.model_copy(update={name: value})})
        else:
            raise InvalidModelChangeError(f"field '{field}' is not editable")

        with db.session_scope() as session:
            EventLogger.log(session, run_id, Roles.HUMAN.value, EventType.RUN_CONFIG_CHANGED, {
                "field": field, "old_value": old, "new_value": value,
            })
        _log.info("run.config_changed", run_id=run_id, field=field, old=old, new=value)

    def set_agent_model(self, run_id: str, agent: str, provider_id: str, model: str) -> None:
        """Change ONE agent's model mid-run (GH #64) by patching the checkpointed RunConfig —
        takes effect on that agent's next node (agents build their LLM from `config.llm.<agent>`
        at the start of every invoke). Synchronous so a bad request 422s the HTTP call; no
        re-drive (unlike /continue) — a running/next node just picks up the new selection."""
        agent = agent.strip().lower()
        if agent not in self._LLM_AGENTS:
            raise InvalidModelChangeError(f"unknown agent '{agent}'")
        if not model.strip():
            raise InvalidModelChangeError("model must not be empty")
        settings = SettingsStore.get()
        provider = SettingsStore.provider(settings, provider_id)
        if provider is None:
            raise InvalidModelChangeError(f"provider '{provider_id}' not found in settings")
        if self.status(run_id).run_state in (RunState.COMPLETED, RunState.FAILED):
            raise InvalidModelChangeError("run is terminal — its model can't be changed")

        config = self._run_config(run_id)
        if config is None:
            raise InvalidModelChangeError(f"run {run_id} has no checkpointed config to patch")

        old = getattr(config.llm, agent)
        new_sel = old.model_copy(update={"provider_id": provider_id, "model": model})
        new_llm = config.llm.model_copy(update={agent: new_sel})
        new_providers = {**config.providers, provider_id: provider}  # ensure the picked provider is resolvable
        patched = config.model_copy(update={"llm": new_llm, "providers": new_providers})
        graph.update_state(self._config(run_id), {"config": patched})

        with db.session_scope() as session:
            # The Coder is the headline model mirrored onto the runs row (bootstrap precedent).
            if agent == "coder":
                run_record = session.get(Run, run_id)
                if run_record:
                    run_record.llm_provider = provider_id
                    run_record.llm_model = model
            EventLogger.log(session, run_id, Roles.HUMAN.value, EventType.RUN_MODEL_CHANGED, {
                "agent": agent,
                "old_provider_id": old.provider_id, "old_model": old.model,
                "new_provider_id": provider_id, "new_model": model,
            })
        _log.info("run.model_changed", run_id=run_id, agent=agent, provider=provider_id, model=model)
    
    
    def _verify_constraint_value(
        self, name: str, value: int, current_iteration: Optional[Iteration], current_microtask_attempts: int,
    ) -> None:
        """Guard a loop-cap patch on a stalled/paused run. A new cap must leave the run room to
        continue — setting it at/below the count the run has ALREADY reached would just re-trip
        the same guard on the next node (an instant re-stall/re-fail). Also enforce a per-cap
        hard ceiling so a mistyped value can't effectively disable the guard."""
        # The planner↔reviewer cap can't be dropped below where the planning loop already is.
        if name == "max_planner_reviewer_iterations":
            cur = current_iteration.planner_reviewer_iteration_current if current_iteration else 0
            if value <= cur:
                raise InvalidModelChangeError(
                    f"{name} ({value}) must exceed the planner↔reviewer iterations already used ({cur})")
        # The per-microtask cap can't be dropped at/below the attempts already spent on the
        # in-flight microtask — the next coder cycle's `_check_stop_limit` (attempts >= limit)
        # would fail the run instantly.
        elif name == "max_microtask_attempts":
            if value <= current_microtask_attempts:
                raise InvalidModelChangeError(
                    f"{name} ({value}) must exceed the attempts already spent on the current "
                    f"microtask ({current_microtask_attempts})")
        ceiling = self._CONSTRAINT_CEILINGS.get(name)
        if ceiling is not None and value > ceiling:
            raise InvalidModelChangeError(f"{name} cannot exceed {ceiling}")

    def _current_microtask_attempts(self, run_id: str) -> int:
        """Attempts already spent on the microtask the execution loop is working now (the one
        `coder_node` would re-enter on /continue) — the in-flight microtask, else 0. Mirrors the
        selection used by `_progress` / the coder-tester-reflector nodes."""
        values = graph.get_state(self._config(run_id)).values or {}
        microtasks = values.get("microtasks") or []
        by_id = {mt.microtask_id: mt for mt in microtasks}
        current = by_id.get(values.get("current_microtask_id"))
        if current is None:
            current = next(
                (mt for mt in sorted(microtasks, key=lambda m: m.idx)
                 if mt.status in self._IN_FLIGHT_MICROTASK_STATUSES),
                None,
            )
        return current.attempts if current is not None else 0
    
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
            # A stalled run's work lives in its sandbox container. If the container is gone
            # (host restart, `docker rm`, prune), the in-progress checkout/edits can't be
            # recovered and continuing would re-enter a node that assumes the sandbox is there.
            # Fail the run cleanly rather than crash mid-continue (GH #56). Skip the check for a
            # run still at NEEDS_BOOTSTRAP — it never had a sandbox yet, so "missing" is expected.
            snapshot: BlackboardState = graph.get_state(self._config(run_id)).values or {}  # type: ignore[assignment]
            sandbox = DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace")
            if snapshot.get("status") is not RunStatus.NEEDS_BOOTSTRAP and not sandbox.sandbox_exists(
                    sandbox_id=sandbox.get_sandbox_id(run_id=run_id, verify=False),
                    start_if_created_but_not_running=True):
                _log.error("run.continue_sandbox_missing", run_id=run_id)
                with db.session_scope() as session:
                    RunFailureHandler.handle(
                        session, run_id, "system", snapshot,
                        RunExecutionError(
                            phase="run.continue",
                            reason="sandbox container missing after crash/restart — the run's "
                                   "in-progress work is unrecoverable"))
                log_bus.mark_done(run_id)          # terminal now — close the live stream
                return self._response(run_id)      # the DB FAILED row wins → RunState.FAILED
            _log.info("run.continue", run_id=run_id)
            with db.session_scope() as session:
                EventLogger.log(session, run_id, Roles.HUMAN.value, EventType.RUN_RESUMED, {})
            self._drive(run_id, None)
            self._finalize(run_id)
            return self._response(run_id)
        finally:
            self.mark_inactive(run_id)
            clear_context()

    def status(self, run_id: str) -> RunResponse:
        """Current lifecycle snapshot for the run — the dashboard's poll target."""
        return self._response(run_id)

    def give_up(self, run_id: str, note: Optional[str] = None) -> RunResponse:
        """Human verdict on a STALLED run: mark it FAILED ('Mark as failed' button).
        Crashes leave runs stalled-but-continuable by design (ADR 0021); this is the
        deliberate close-the-case action when the human judges the run hopeless. Goes
        through RunFailureHandler so status/events/lesson-retraction match every other
        failure path, then honors teardown_on_terminal (the run is terminal now)."""
        reason = f"marked failed by user{f': {note}' if note else ''}"
        try:
            snapshot = graph.get_state(self._config(run_id))
            state: BlackboardState = snapshot.values or {}  # type: ignore[assignment]
        except Exception:
            state = {}  # type: ignore[assignment]
        with db.session_scope() as session:
            RunFailureHandler.handle(
                session, run_id, "human", state,
                RunExecutionError(phase="human", reason=reason),
            )
        _log.info("run.given_up", run_id=run_id, note=note or "")
        log_bus.mark_done(run_id)
        self._teardown_sandbox(run_id)
        return self._response(run_id)

    # Last-resort ceiling for stop_run's wait — comfortably above any single configured
    # agent LLM timeout, so hitting it means something is genuinely stuck, not just slow.
    _STOP_WAIT_TIMEOUT_SECONDS = 180

    def stop_run(self, run_id: str, note: Optional[str] = None) -> RunResponse:
        """Hard-stop a live run ('Stop' button, ADR 0033). Sets the cooperative stop flag
        FIRST (orchestrator/run_control.py) — every graph node wrapper checks it before
        doing any work, which is the actual stop mechanism: a killed sandbox alone does
        NOT reliably interrupt a live node, since tool-call exceptions are caught deep in
        ToolGateway and fed back to the LLM as a normal observation instead of propagating,
        and much of a ReAct agent's investigation may never touch the sandbox at all (e.g.
        semantic_search over pgvector). The sandbox kill still happens too — real signal
        for whatever sandbox call IS in flight, and needed for the actual container
        cleanup. Then BLOCKS until `is_active` genuinely goes False before writing the
        terminal verdict — never forces the claim open early, since that would let a
        second driver start on this run_id while the old thread might still be mutating
        the same checkpoint/DB row. Raises RunStopTimeoutError (mapped to 503, safe to
        retry) if the wait exceeds `_STOP_WAIT_TIMEOUT_SECONDS`."""
        current = self._response(run_id)
        if current.run_state in (RunState.COMPLETED, RunState.FAILED, RunState.STOPPED):
            raise RunAlreadyTerminalError(f"run {run_id} is already {current.run_state.value}")
        request_stop(run_id)
        self._teardown_sandbox(run_id, force=True)
        if current.run_state == RunState.RUNNING:
            deadline = time.monotonic() + self._STOP_WAIT_TIMEOUT_SECONDS
            while self.is_active(run_id):
                if time.monotonic() >= deadline:
                    raise RunStopTimeoutError(run_id)
                time.sleep(0.2)
        reason = f"manually stopped by user{f': {note}' if note else ''}"
        with db.session_scope() as session:
            run_record = session.get(Run, run_id)
            if run_record is None:
                raise RunNotFoundError(run_id)
            run_record.status = STOPPED_STATUS
            run_record.updated_at = datetime.now(timezone.utc)
            try:
                LessonStore.retract_run(session, run_id)
            except Exception:
                pass
            EventLogger.log(session, run_id, Roles.HUMAN.value, EventType.RUN_STOPPED, {"reason": reason})
        _log.info("run.stopped", run_id=run_id, note=note or "")
        log_bus.mark_done(run_id)
        self._purge_semantic(run_id)
        clear_stop(run_id)   # run is terminal now — drop the flag, don't leak it forever
        return self._response(run_id)

    def delete_run(self, run_id: str) -> None:
        """Permanently remove a run and its per-run data (ADR 0033). Refuses a live run
        (409 in the route) — delete is a data-removal operation and must never race a
        live driver thread; the run must be stopped first. Lessons are deliberately KEPT
        (cross-run memory, ADR 0018) — deleting a run's audit history is orthogonal to
        whether its lessons were already judged trustworthy at write time."""
        current = self._response(run_id)
        if current.run_state in (RunState.RUNNING, RunState.PAUSED_HITL):
            raise RunNotDeletableError(f"run {run_id} is {current.run_state.value} — stop it first")
        self._teardown_sandbox(run_id, force=True)
        SemanticStore().delete_workspace(run_id)
        checkpointer.delete_thread(run_id)
        with db.session_scope() as session:
            run_record = session.get(Run, run_id)
            if run_record is None:
                raise RunNotFoundError(run_id)
            for model in (EventRow, ArtifactRow, MicrotaskRecord, Sandbox):
                session.query(model).filter(model.run_id == run_id).delete()
            session.delete(run_record)
        clear_stop(run_id)   # defensive — e.g. a stop_run that hit RunStopTimeoutError
        _log.info("run.deleted", run_id=run_id)

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
            # Deploy gate: the message the push WOULD use (per-run value or default),
            # so the UI can prefill its editable commit-message input (ADR 0027).
            deploy_commit_message=(
                (state.get("deploy_commit_message") or "").strip()
                or f"AgenticDevOps run {run_id}"
            ) if phase == Phase.DEPLOY.value else None,
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

    def run_cost(self, run_id: str) -> RunCostView:
        """Detailed USD cost for a run, broken down per phase and per model (GH #62).

        Derived from the durable `llm.usage` events (written on every path including a
        crash, ADR 0025) — so it's accurate live, after a clean finish, AND after a
        stall/crash. NOT computed in the deploy node, which a failed run never reaches.
        Groups the events by (agent, model), sums the token split, and prices each bucket
        via `providers.pricing.cost_for`; a model with no list price (cluster) contributes
        its tokens but no cost and flips `has_unpriced`."""
        buckets: dict[tuple[str, str], dict[str, int]] = {}
        with db.session_scope() as session:
            rows = (
                session.query(EventRow.payload_json)
                .filter(
                    EventRow.run_id == run_id,
                    EventRow.event_type == EventType.LLM_USAGE_RECORDED.value,
                )
                .all()
            )
        for (payload,) in rows:
            payload = payload or {}
            phase = str(payload.get("agent") or "")
            model = str(payload.get("model") or "")
            if not phase or not model:
                continue
            acc = buckets.setdefault((phase, model), {
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "total": 0,
            })
            acc["input"] += int(payload.get("input_tokens") or 0)
            acc["output"] += int(payload.get("output_tokens") or 0)
            acc["cache_read"] += int(payload.get("cache_read_tokens") or 0)
            acc["cache_creation"] += int(payload.get("cache_creation_tokens") or 0)
            acc["total"] += int(payload.get("total_tokens") or 0)

        lines: List[PhaseCostView] = []
        total_cost = 0.0
        total_tokens = 0
        has_unpriced = False
        for (phase, model), acc in buckets.items():
            cost = cost_for(
                model, acc["input"], acc["output"], acc["cache_read"], acc["cache_creation"],
            )
            if cost is None:
                has_unpriced = True
            else:
                total_cost += cost.total
            total_tokens += acc["total"]
            lines.append(PhaseCostView(
                phase=phase, model=model,
                input_tokens=acc["input"], output_tokens=acc["output"],
                cache_read_tokens=acc["cache_read"],
                cache_creation_tokens=acc["cache_creation"],
                total_tokens=acc["total"],
                priced=cost is not None,
                cost=None if cost is None else CostView(**cost.model_dump()),
            ))
        # Priced lines first, then by spend — the biggest cost driver reads at the top.
        lines.sort(key=lambda ln: (ln.cost is None, -(ln.cost.total if ln.cost else 0.0)))
        return RunCostView(
            lines=lines, total_cost=total_cost, total_tokens=total_tokens,
            has_unpriced=has_unpriced,
        )

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
        state = self._list_state(run.status, self.is_active(run.run_id))
        return RunResponseWithDetails(
            run_id=run.run_id,
            run_state=state,
            status=run.status,
            failure_reason=(self._last_event_payload(run.run_id, EventType.RUN_FAILED.value) or {}).get("reason")
            if state == RunState.FAILED else None,
            repo_url=run.repo_url,
            repo_name=self._repo_name(run.repo_url),
            base_sha=run.base_sha,
            task_description=self._ensure_task(session, run),
            pushed_files=pushed_files,
            llm_provider=run.llm_provider,
            llm_model=run.llm_model,
            tokens_used_total=run.tokens_used_total,
            max_total_tokens=run.max_total_tokens,
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

    # ----------------------------------------------------------------- settings
    def get_settings(self) -> AppSettings:
        """UI settings with keys masked."""
        return SettingsStore.get_public()

    def update_settings(self, new: AppSettings) -> AppSettings:
        """Persist settings; a provider key left as the mask keeps its stored value."""
        return SettingsStore.update(new)

    def test_provider(self, provider_id: str) -> dict:
        """Health-probe a stored provider (settings 'Test connection'). Uses the real key.
        On a healthy provider it ALSO probes prompt caching (GH #65) — two identical
        padded calls, cache_read on the second — so an invisible server property
        (vLLM prefix caching on / off, OpenAI automatic caching) becomes checkable
        from the dashboard. Probe failure degrades to cache=None, never fails the test."""
        settings = SettingsStore.get()
        provider = SettingsStore.provider(settings, provider_id)
        if provider is None:
            return {"ok": False, "reason": f"provider '{provider_id}' not found", "kind": "config"}
        model = provider.models[0] if provider.models else settings.selected.planner.model
        from orchestrator.state import ProbeLLMConfig
        llm = ProbeLLMConfig(provider_id=provider_id, model=model)
        result = health_check(provider, llm)
        out = {"ok": result.ok, "latency_ms": result.latency_ms, "reason": result.reason, "kind": result.kind}
        if result.ok:
            out["cache"] = probe_cache(provider, llm)   # {"active", "read_tokens"} | None
        return out

    def test_github(self) -> dict:
        """Validate the stored GitHub token (settings 'Test connection'): GET /user with
        the real token. No token = anonymous mode — fine for public repos, called out as
        such rather than failing. Never logs or returns the token itself."""
        import time

        import requests

        token = SettingsStore.github_token()
        if not token:
            return {"ok": False, "kind": "no_token",
                    "reason": "no token set — public repos only; private clone/push will fail"}
        try:
            t0 = time.monotonic()
            resp = requests.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {token}",
                         "Accept": "application/vnd.github+json"},
                timeout=5,
            )
            latency = int((time.monotonic() - t0) * 1000)
        except requests.RequestException as exc:
            return {"ok": False, "kind": "unreachable", "reason": str(exc)}
        if resp.status_code == 200:
            login = resp.json().get("login", "?")
            return {"ok": True, "latency_ms": latency, "reason": f"authenticated as {login}"}
        if resp.status_code in (401, 403):
            return {"ok": False, "kind": "auth", "reason": f"GitHub rejected the token (HTTP {resp.status_code})"}
        return {"ok": False, "kind": "unexpected", "reason": f"HTTP {resp.status_code}"}

    # ----------------------------------------------------------------- repo registry
    # GH #41 — repos are first-class: validated on add, re-validated as a run preflight.
    # Raw per-repo tokens never cross this layer (masked views; resolve_token at gitops).

    @staticmethod
    def _repo_view(entry: RepoEntry) -> RepoView:
        return RepoView.model_validate(entry.model_dump())

    def list_repos(self) -> List[RepoView]:
        return [self._repo_view(e) for e in RepoStore.list()]

    def add_repo(self, req: RepoCreateRequest) -> RepoView:
        """Normalize → validate against GitHub → create when the repo is at least
        READABLE. Registration only needs read/clone access: a repo with no push
        access (clone-only — usable for dry-runs, or pending a token) is registrable
        and shown as such in the UI. Push/PR access is NOT required to add — it's
        gated per-run at run-start (`resolve_repo`/`_require_run_rights`), where a
        dry-run is the documented escape. Not-found / no-read / unreachable still block."""
        canonical = normalize_repo_url(req.repo_url)          # ValueError → 422
        token = req.token or SettingsStore.github_token()
        result = self._github.validate_repo(canonical, token)
        self._reject_if_unreadable(result)
        self._reject_default_branch_push(req.push_branch_override, result.default_branch)
        entry = RepoStore.create(repo_url=canonical, label=req.label, token=req.token,
                                 push_branch_override=req.push_branch_override,
                                 validation=result)
        return self._repo_view(entry)

    def update_repo(self, repo_id: str, req: RepoUpdateRequest) -> RepoView:
        """Mask-preserving edit; a re-typed token is re-validated immediately so a bad
        renewal surfaces on save, not on the next run."""
        current = RepoStore.get(repo_id)                      # RepoNotFoundError → 404
        self._reject_default_branch_push(req.push_branch_override, current.default_branch)
        entry = RepoStore.update(repo_id, label=req.label, token=req.token,
                                 push_branch_override=req.push_branch_override)
        token_changed = req.token is not None and req.token != MASK
        if token_changed:
            self.validate_repo(repo_id)
            entry = RepoStore.get(repo_id)
        return self._repo_view(entry)

    def delete_repo(self, repo_id: str) -> None:
        RepoStore.delete(repo_id)

    def validate_repo(self, repo_id: str) -> RepoValidationResult:
        """'Test connection' for a repo entry — validates with the resolved token and
        persists the snapshot (status dot + refreshed default_branch)."""
        entry = RepoStore.get(repo_id)
        result = self._github.validate_repo(entry.repo_url, RepoStore.resolve_token(repo_id))
        RepoStore.record_validation(repo_id, result)
        return result

    @staticmethod
    def _reject_if_unreadable(result: RepoValidationResult) -> None:
        """Raise unless the repo is at least READABLE/cloneable. A NO_PUSH_ACCESS result
        came from a 200 on the repo GET (exists + read access) — cloneable even though it
        can't be pushed to. Every other not-ok kind (not found, no read access,
        unreachable) blocks — you can't clone what you can't read."""
        if result.ok or result.kind == RepoValidationKind.NO_PUSH_ACCESS:
            return
        raise RepoValidationFailedError(result.kind, result.reason)

    @staticmethod
    def _reject_if_unusable(result: RepoValidationResult, *, skip_deploy: bool) -> None:
        """Run-start clone gate. A dry-run only needs read/clone, so a clone-only
        (NO_PUSH_ACCESS) repo is fine for it; a deploying run additionally needs push
        (enforced by `_require_run_rights`), so it must be fully `ok` here."""
        if skip_deploy:
            Service._reject_if_unreadable(result)
            return
        if not result.ok:
            raise RepoValidationFailedError(result.kind, result.reason)

    @staticmethod
    def _require_run_rights(result: RepoValidationResult, *, auto_create_pr: bool,
                            skip_deploy: bool) -> None:
        """Run-start preflight (NOT the add-repo path). A completing run auto-pushes
        (tester DONE → deploy → deploy_push), so push access is REQUIRED — and
        `result.ok` alone is not enough: an anonymous (no-token) request returns
        `ok=True, can_push=None` (never probed), which would otherwise start a run
        guaranteed to fail at the final push. So we require `can_push is True`
        explicitly (catches both the anonymous None and a token that can't push).
        PR access is required only when the deploy flow will actually open one
        (auto_create_pr on).

        A dry-run (`skip_deploy`) never reaches the deploy node, so it needs neither
        push nor PR rights — clone/read is enough (ADR 0029)."""
        if skip_deploy:
            return
        if result.can_push is not True:
            raise RepoValidationFailedError(
                RepoValidationKind.NO_PUSH_ACCESS,
                "a completing run pushes its result branch, but this repository has no "
                "verified push access — add a token with push permission, or start the "
                "run as a dry-run (skip deploy)",
            )
        if auto_create_pr and not result.can_create_pr:
            raise RepoValidationFailedError(
                RepoValidationKind.NO_PR_ACCESS,
                "auto-create-PR is enabled in Settings but the token has no "
                "pull-request access to this repository — disable auto-create-PR "
                "or use a token with pull-request permission",
            )

    def repo_branches(self, repo_id: str) -> List[str]:
        """Live GitHub branch names for the picker — raises BranchListError with a
        validation-style kind on failure. Never cached in the DB."""
        entry = RepoStore.get(repo_id)
        return self._github.list_branches(entry.repo_url, RepoStore.resolve_token(repo_id))

    @staticmethod
    def _reject_default_branch_push(override: Optional[str], default_branch: Optional[str]) -> None:
        """Fail early at add/update — the deploy-time GitOps guard is the backstop."""
        if override and default_branch and override == default_branch:
            raise ValueError(
                f"push_branch_override '{override}' is the repo's default branch — "
                "agents propose on a separate branch, never land directly"
            )

    def resolve_repo(self, request: RunRequest) -> "Repo":
        """Run-preflight (mirror of resolve_and_preflight): turn the request's repo
        reference into a validated blackboard Repo, or raise so NO run is created.

        repo_id path: registry row + per-repo token, re-validated now (the snapshot is
        recorded either way so the repo list shows the red dot). Legacy raw-repo path:
        URL normalized, registry entry attached by canonical URL when one exists (so
        its token applies), then validated the same way. Either path also checks PR
        rights via `_require_run_rights` when auto_create_pr is on — unlike add_repo,
        a run must not start without the rights it will actually need."""
        auto_create_pr = SettingsStore.get().defaults.auto_create_pr
        if request.repo_id:
            entry = RepoStore.get(request.repo_id)            # RepoNotFoundError → 404
            result = self._github.validate_repo(entry.repo_url, RepoStore.resolve_token(entry.repo_id))
            RepoStore.record_validation(entry.repo_id, result)
            self._reject_if_unusable(result, skip_deploy=request.skip_deploy)
            self._require_run_rights(result, auto_create_pr=auto_create_pr, skip_deploy=request.skip_deploy)
            return Repo(
                repo_url=entry.repo_url,
                repo_id=entry.repo_id,
                default_branch=result.default_branch or entry.default_branch,
                base_ref=request.base_ref,
                base_sha=request.base_sha,
                workspace_branch="agentic-devops-workspace",
            )

        legacy = request.repo
        if legacy is None:
            # unreachable behind the RunRequest exactly-one-of validator; narrows the
            # Optional for the type checker and guards direct Service callers
            raise ValueError("provide exactly one of 'repo' or 'repo_id'")
        canonical = normalize_repo_url(legacy.repo_url)       # ValueError → 422
        entry = RepoStore.find_by_url(canonical)
        repo_id = entry.repo_id if entry else None
        result = self._github.validate_repo(canonical, RepoStore.resolve_token(repo_id))
        if repo_id:
            RepoStore.record_validation(repo_id, result)
        self._reject_if_unusable(result, skip_deploy=request.skip_deploy)
        self._require_run_rights(result, auto_create_pr=auto_create_pr, skip_deploy=request.skip_deploy)
        return legacy.model_copy(update={
            "repo_url": canonical,
            "repo_id": repo_id,
            "default_branch": result.default_branch or legacy.default_branch,
        })

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
                    title=m.title, description=m.description, approach=m.approach,
                    status=m.status, attempts=m.attempts,
                    acceptance_criteria=[c.model_dump() for c in m.acceptance_criteria],
                    target_files=m.target_files, context_files=m.context_files,
                    dependencies=m.dependencies, files_touched=m.files_touched,
                    test_strategy=m.test_strategy,
                    created_at=m.created_at, updated_at=m.updated_at,
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
        if status == STOPPED_STATUS:
            return RunState.STOPPED
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
                    description=r.description,
                    approach=r.approach,
                    status=r.status,
                    attempts=r.attempts,
                    acceptance_criteria=list(r.acceptance_criteria or []),
                    target_files=list(r.target_files or []),
                    context_files=list(r.context_files or []),
                    dependencies=list(r.dependencies or []),
                    files_touched=list(r.files_touched or []),
                    test_strategy=r.test_strategy,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
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
            microtask_id=row.microtask_id,
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
        except TokenBudgetExceededError as exc:
            # Constraints.max_total_tokens reached (ADR 0025/0027). Deliberately NOT a
            # terminal FAILED like the other deterministic caps (max_planner_reviewer_iterations,
            # microtask attempts): a token cap is a policy knob the human can raise
            # out-of-band, so leave the run non-terminal — STALLED, checkpoint intact —
            # and recoverable via /continue (retries the same node) or /fail to close it.
            _log.warning("graph.token_budget_exceeded", run_id=run_id, error=exc.reason)
            self._mark_stalled(run_id, exc.reason, kind="token_budget")
        except RetriableError as exc:
            # Explicit transient signal from an agent — an LLM API hiccup (timeout / 429
            # "overloaded" / 5xx), not a run defect. Do NOT fail: leave the run non-terminal
            # so it reads STALLED and can be re-driven via /continue, re-running the node.
            _log.warning("graph.transient", run_id=run_id, error=exc.reason)
            self._mark_stalled(run_id, exc.reason)
        except RunStoppedError:
            # A node aborted because stop_run() flagged this run_id (ADR 0033 amendment,
            # orchestrator/run_control.py) — a clean, intentional unwind, not a crash. No
            # stall event: stop_run's own wait loop is watching is_active and will write
            # the terminal STOPPED status + its own run.stopped event once this thread
            # exits (imminently, via the caller's `finally: mark_inactive`).
            _log.info("graph.stop_requested", run_id=run_id)
        except Exception as exc:
            # A raw transient that escaped the agents (defensive — the nodes classify these
            # now, but a call site outside an agent could still leak one) → also STALL.
            if is_transient_llm_error(exc):
                _log.warning("graph.transient", run_id=run_id, error=str(exc))
                self._mark_stalled(run_id, f"Transient LLM error: {exc}")
                return
            # Uncaught crash — a bug, not a domain failure. Do NOT force-fail: the
            # checkpoint is intact and one fixed bug away from finishing, so leave the
            # run non-terminal (reads STALLED, sandbox alive) and continuable via
            # /continue. Previously this wrote Run.status=FAILED while the checkpoint
            # stayed non-terminal — the runs LIST said FAILED, the run DETAIL said
            # STALLED. Domain failures (iteration caps, bad microtasks) still go
            # terminal through the nodes' own RunFailureHandler paths.
            _log.error("graph.uncaught", run_id=run_id, error=str(exc), exc_info=True)
            self._mark_stalled(run_id, f"Uncaught {type(exc).__name__}: {exc}")

    def _mark_stalled(self, run_id: str, reason: str, kind: str = "transient") -> None:
        """Record a stall WITHOUT touching Run.status — the run stays at whatever node it
        reached, so `_derive_state` reports STALLED and /continue re-drives it. Only emits
        an audit event carrying the reason so the dashboard can say why (api timeout vs
        worker died vs token budget). `kind` distinguishes a genuinely transient error from
        a token-budget stop (ADR 0025) — same recovery mechanics, different cause."""
        with db.session_scope() as session:
            EventLogger.log(session, run_id, Roles.ORCHESTRATOR.value, EventType.RUN_STALLED, {
                "reason": reason, "kind": kind,
            })

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
        values = snapshot.values or {}
        status = values.get("status")
        pending = self._pending_payload(run_id)
        state = self._derive_state(status, pending, self.is_active(run_id))
        db_status = self._db_status(run_id)
        if state == RunState.STALLED and db_status == RunStatus.FAILED.value:
            # human closed the case (give_up) or sandbox doesnt exists anymore: checkpoint stays non-terminal, but the
            # runs-table verdict is FAILED — the row wins over the orphaned checkpoint.
            state = RunState.FAILED
        if state in (RunState.STALLED, RunState.PAUSED_HITL) and db_status == STOPPED_STATUS:
            # A run stopped while PAUSED_HITL still has its checkpoint's pending interrupt,
            # so _derive_state returns PAUSED_HITL first — the row still wins (ADR 0033).
            state = RunState.STOPPED
        stall_payload = self._last_event_payload(run_id, EventType.RUN_STALLED.value) if state == RunState.STALLED else None
        fail_payload = self._last_event_payload(run_id, EventType.RUN_FAILED.value) if state == RunState.FAILED else None
        stop_payload = self._last_event_payload(run_id, EventType.RUN_STOPPED.value) if state == RunState.STOPPED else None
        return RunResponse(
            run_id=run_id,
            run_state=state,
            status=status.value if isinstance(status, RunStatus) else status,
            hitl_phase=pending.get("phase") if pending else None,
            hitl_approval_id=pending.get("approval_id") if pending else None,
            # Only for a stalled run: the reason/kind if the stall carried one (worker-died
            # stalls have no run.stalled event → None → the UI shows the generic message).
            stalled_reason=stall_payload.get("reason") if stall_payload else None,
            stalled_kind=stall_payload.get("kind") if stall_payload else None,
            # Only for a failed run: the run.failed reason — the dashboard's error banner.
            failure_reason=fail_payload.get("reason") if fail_payload else None,
            # Only for a manually-stopped run: the run.stopped reason — the gray banner.
            stopped_reason=stop_payload.get("reason") if stop_payload else None,
            progress=self._progress(values),
        )

    # `Microtask.status` values that mean "the coder/tester/reflector loop is actively
    # working this one right now" — mirrors the fallback pick used by the reflector/
    # tester nodes themselves (reflector_node.py:_select_microtask, tester_node.py).
    _IN_FLIGHT_MICROTASK_STATUSES = (MicrotaskStatus.IN_PROGRESS, MicrotaskStatus.EXECUTING, MicrotaskStatus.VERIFYING)

    @classmethod
    def _progress(cls, state: dict) -> Optional[PipelineProgress]:
        """Planner-loop / microtask-loop iteration counts read straight off the
        checkpoint (ADR 0028) — no DB read, no new columns. None before the graph
        has produced Iteration/Constraints state (e.g. mid-bootstrap)."""
        iteration = state.get("iteration")
        constraints = state.get("constraints")
        if iteration is None or constraints is None:
            return None
        microtasks = state.get("microtasks") or []
        current_id = state.get("current_microtask_id")
        by_id = {mt.microtask_id: mt for mt in microtasks}
        current = by_id.get(current_id) if current_id else None
        if current is None:
            current = next(
                (mt for mt in sorted(microtasks, key=lambda m: m.idx)
                 if mt.status in cls._IN_FLIGHT_MICROTASK_STATUSES),
                None,
            )
        microtask_progress = None
        if current is not None:
            microtask_progress = MicrotaskProgress(
                idx=current.idx,
                total=len(microtasks),
                title=current.title,
                status=current.status.value,
                attempts=current.attempts,
                max_attempts=current.max_attempts or constraints.max_microtask_attempts,
            )
        return PipelineProgress(
            planner_reviewer_iteration=iteration.planner_reviewer_iteration_current,
            planner_reviewer_max=constraints.max_planner_reviewer_iterations,
            microtask=microtask_progress,
        )

    @staticmethod
    def _db_status(run_id: str) -> Optional[str]:
        with db.session_scope() as session:
            run = session.get(Run, run_id)
            return run.status if run else None

    @staticmethod
    def _last_event_payload(run_id: str, event_type: str) -> Optional[dict]:
        """Full `payload_json` of the run's most recent event of the given type (run.stalled /
        run.failed) — powers the stalled-bar (reason + kind, ADR 0025) and the failure banner
        (reason)."""
        with db.session_scope() as session:
            row = (
                session.query(EventRow)
                .filter(EventRow.run_id == run_id, EventRow.event_type == event_type)
                .order_by(EventRow.seq.desc())
                .first()
            )
            return (row.payload_json or {}) if row else None

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
        # Optional cleanup, GUARDED on a truly terminal status: a stalled run (crash /
        # transient error → status still mid-pipeline) keeps its container so /continue
        # can pick the work back up. Settings-driven (defaults.teardown_on_terminal).
        if status in (RunStatus.DONE, RunStatus.FAILED):
            self._purge_semantic(run_id)
            self._teardown_sandbox(run_id)

    def _purge_semantic(self, run_id: str) -> None:
        """Drop this terminal run's workspace index (dead weight once the run is done —
        decoupled from teardown_on_terminal, which governs the Docker container, not the
        index) and opportunistically sweep orphaned chunks left by earlier stalled/crashed
        runs or de-registered repos (context-engine #6). Best-effort — a housekeeping
        failure must never mask the run's outcome."""
        try:
            store = SemanticStore()
            store.delete_workspace(run_id)
            pruned = store.prune_orphans()
            _log.info("semantic.pruned", run_id=run_id, **pruned)
        except Exception as exc:
            _log.warning("semantic.prune_failed", run_id=run_id, error=str(exc))

    def _teardown_sandbox(self, run_id: str, force: bool = False) -> None:
        """Destroy the run's Docker sandbox if settings ask for it, or unconditionally
        when `force` (an explicit user Stop/Delete, ADR 0033) — those must tear the
        container down regardless of the `teardown_on_terminal` setting, which only
        governs routine end-of-run housekeeping. Best-effort — a teardown failure must
        never mask the run's actual outcome."""
        try:
            if not force and not SettingsStore.get().defaults.teardown_on_terminal:
                return
            DockerSandboxTools(image="agentic-sandbox:0.1", workdir="/workspace").sandbox_destroy(
                sandbox_id=f"sbox_{run_id}"
            )
            _log.info("sandbox.teardown_on_terminal", run_id=run_id)
        except Exception as exc:
            _log.warning("sandbox.teardown_failed", run_id=run_id, error=str(exc))

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
