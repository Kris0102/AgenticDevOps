from typing import Optional

from pydantic import BaseModel, model_validator

from orchestrator.state import (
    HITL,
    Constraints,
    DeployMode,
    LLMConfig,
    Objective,
    Policy,
    Repo,
    SandboxSpec,
    Trigger,
)


class RunRequest(BaseModel):
    run_name: Optional[str] = None
    
    objective: Objective
    trigger: Trigger
    # Exactly one of `repo` / `repo_id`: the dashboard sends `repo_id` (registry pick,
    # GH #41); the raw `repo` object is the legacy path (CLI/scripts) and still works.
    repo: Optional[Repo] = None
    repo_id: Optional[str] = None
    # Per-run base-branch override for the repo_id path (None = the repo's default
    # branch). Legacy `repo` payloads carry base_ref inside the Repo object instead.
    base_ref: Optional[str] = None
    # Optional commit pin on top of base_ref (repo_id path).
    base_sha: Optional[str] = None
    constraints: Constraints
    policy: Policy
    hitl: HITL
    # Per-run LLM override (settings hub). None → the global selected model. Caps/gates are
    # already carried by `constraints`/`hitl`, pre-filled from global defaults by the UI.
    llm: Optional[LLMConfig] = None
    # Optional commit message for the eventual deploy push. Overridable at the deploy
    # HITL gate; blank/None → a message built from the run (objective + microtasks +
    # files)..
    commit_message: Optional[str] = None
    # Optional per-run push branch. Wins over the repo registry's `push_branch_override`;
    # blank/None → `agent/run-<run_id>`. Never the repo's default branch — refused at run
    # creation (422) and again at deploy. .
    push_branch: Optional[str] = None
    # What THIS run's deploy step does (ADR 0036): dry_run | push | pr | local_only.
    # None → the global default (Settings.defaults.auto_create_pr: True → pr, False →
    # push). Resolved once at run creation; the mode decides which rights the run-start
    # preflight requires (pr → PR rights, push → push rights, dry_run/local_only →
    # clone/read only). Every mode still passes the deploy HITL gate when configured.
    deploy_mode: Optional[DeployMode] = None
    # LOCAL_ONLY target: absolute path of a git clone on the host this API runs on —
    # the run branch is fetched into it via a git bundle, no remote touched. Required
    # exactly when deploy_mode is local_only; filesystem-validated at run creation (422).
    local_export_path: Optional[str] = None
    # Container shape for this run. None → the generic agent sandbox (agentic-sandbox:0.1 at
    # /workspace). Set to a PRE-PROVISIONED image — one shipping the repo plus its dependency
    # env — by the SWE-bench adapter, which is how the Tester gets a runnable test
    # environment instead of skipping for missing dependencies. Not exposed in the dashboard;
    # scripted/benchmark callers only.
    sandbox_spec: Optional[SandboxSpec] = None

    @model_validator(mode="after")
    def _exactly_one_repo_source(self) -> "RunRequest":
        if (self.repo is None) == (self.repo_id is None):
            raise ValueError("provide exactly one of 'repo' or 'repo_id'")
        return self

    @model_validator(mode="after")
    def _local_export_path_matches_mode(self) -> "RunRequest":
        if self.deploy_mode == DeployMode.LOCAL_ONLY and not (self.local_export_path or "").strip():
            raise ValueError("deploy_mode 'local_only' requires local_export_path")
        if self.local_export_path and self.deploy_mode != DeployMode.LOCAL_ONLY:
            raise ValueError("local_export_path is only valid with deploy_mode 'local_only'")
        return self


class ContinueRunRequest(BaseModel):
    """Optional body for POST /runs/{id}/continue. When `additional_tokens` is set, it ADDS
    that many tokens to the checkpointed Constraints.max_total_tokens before re-driving a
    stalled run (continuing a token-budget stall unchanged just immediately re-trips the
    same check). This is an INCREMENT, not a new absolute cap — a run whose cap was 200_000
    and that entered `additional_tokens=200_000` ends up with a 400_000 cap (ADR 0025
    amendment). Omitted body (or a null field) is today's plain re-drive, unaffected."""
    additional_tokens: Optional[int] = None

    @model_validator(mode="after")
    def _positive_if_set(self) -> "ContinueRunRequest":
        if self.additional_tokens is not None and self.additional_tokens <= 0:
            raise ValueError("additional_tokens must be a positive integer")
        return self


class AgentModelPatch(BaseModel):
    """Body for PATCH /runs/{id}/model — change one agent's model mid-run (GH #64). Takes
    effect on that agent's next node."""
    agent: str          # planner | reviewer | coder | tester | reflector
    provider_id: str
    model: str


class RunConfigPatch(BaseModel):
    """Body for PATCH /runs/{id}/config — edit ONE knob or loop-cap on a STALLED or paused
    run (the recovery console, GH #64 follow-up). `field` is 'knobs.<name>' (any Knobs field)
    or 'constraints.max_planner_reviewer_iterations' | 'constraints.max_microtask_attempts'. Every
    editable field is a positive int — the change is checkpointed and read by the relevant
    agent on its next node. `max_total_tokens` is NOT edited here: it has its own additive
    path on /continue (ADR 0025)."""
    field: str
    value: int

    @model_validator(mode="after")
    def _positive(self) -> "RunConfigPatch":
        if self.value <= 0:
            raise ValueError("value must be a positive integer")
        return self


class RepoCreateRequest(BaseModel):
    """Register a repo. `repo_url` is canonicalized server-side; label defaults to
    owner/repo. `token` is an optional per-repo PAT (falls back to the global
    Settings token when unset)."""
    repo_url: str
    label: Optional[str] = None
    token: Optional[str] = None
    push_branch_override: Optional[str] = None


class RepoUpdateRequest(BaseModel):
    """Edit a registered repo. `token` left as the mask (or omitted) keeps the stored
    value, '' clears it. `push_branch_override` '' clears, omitted keeps."""
    label: Optional[str] = None
    token: Optional[str] = None
    push_branch_override: Optional[str] = None


class LessonCreateRequest(BaseModel):
    """Write an operator rule into a repo's lesson store (ADR 0034) — a standing
    instruction the agents follow, as opposed to the advisory lessons they learn
    themselves. `scope` picks who sees it: "plan" → the planner + plan reviewer, "code"
    → the coder and reflector."""
    scope: str = "code"
    summary: str
    guidance: Optional[str] = None
    failure_type: Optional[str] = None


class LessonUpdateRequest(BaseModel):
    """Edit a lesson — human-authored or agent-learned. Omitted fields keep their stored
    value; '' clears the nullable ones. Any edit stamps `updated_at`, which makes the
    lesson immune to the failed-run retraction (ADR 0034)."""
    scope: Optional[str] = None
    summary: Optional[str] = None
    guidance: Optional[str] = None
    failure_type: Optional[str] = None
