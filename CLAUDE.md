
///# AgenticDevOps — Project Guide for Claude

## What this is
Multi-agent system that autonomously plans, implements, tests, and proposes code changes to an existing repo. Triggered by CLI (MVP) or GitHub webhook (future). Uses Docker-per-run sandboxing and Postgres for full audit trail.

## Key architecture decisions
- **Blackboard (run_state)**: single JSON state owned exclusively by Orchestrator. Agents never write state directly.
- **Grounded verification**: code is only accepted when deterministic checks (lint/tests/build) pass. No LLM-only review.
- **Tool Gateway**: all environment actions (file reads, shell commands, git) go through `ToolGateway` — deny-by-default, every call logged.
- **Star topology**: all agent-to-agent communication is mediated by Orchestrator via blackboard, not direct calls.
- **Docker-per-run**: each run gets its own container. Sandbox is the source of truth for code during a run.
- **Bootstrap always first**: `bootstrapper_node` runs unconditionally on every invocation (upserts Run, ensures sandbox running, clones on first run). No side effects in routers.
- **DB-backed sandbox registry**: sandbox container IDs stored in `sandboxes` table — survives process restarts, no in-memory dict.

## Project layout
```
src/
  main.py                        # CLI entrypoint
  orchestrator/
    orchestrator.py              # LangGraph graph + routing
    state.py                     # Pydantic BlackboardState + all models
    bootstrap_node.py            # BootStrapper class — always runs first
  agents/
    Planning/
      Planner/
        planner.py                 # Orchestration only: investigate → synthesize → validate (with retry)
        planner_tools.py           # Tool wrappers for planner
        quality.py                 # _quality_report / _normalize_output — microtask quality gate
        plan_output.py             # create_plan_file_as_output — debug plan dump to disk
        investigation_support.py   # _default_max_tool_steps, _investigation_recursion_limit, _last_ai_text, _log_investigation_steps
        prompts/                   # Prompt templates (investigator.py, synthesis.py)
      Reviewer/
        reviewer.py                # Plan Reviewer — two-phase (investigate + review), structured approve/reject
        reviewer_tools.py          # ReviewerTools: gateway-routed read/exec/semantic-search (read-only)
        review_output.py           # create_review_file_as_output — debug review dump to disk
        investigation_support.py   # shared investigation helpers (mirrors planner)
        prompts/reviewer.py        # REVIEWER_SYSTEM_PROMPT + build_investigation/review_prompt
    Implementing/
      Coder/
        coder.py                 # Coder — single ReAct loop, deterministic repo.diff verification
        coder_tools.py           # CoderTools: gateway-routed read/write/diff/exec + path normalization
        prompt/coder_prompt.py   # CODER_SYSTEM_PROMPT + build_coder_prompt (full grounding context)
      tester.py                  # Tester (stub)
  Exceptions/
    BadMicroTaskExceptions.py    # BadMicroTaskException — raised by quality.py, caught+retried in Planner
  tool_gateway/
    gateway.py                   # ToolGateway: allowlist enforcement (via permissions.get_permissions) + audit logging
    permissions.py               # RolePermissions subclasses — per-role tool + sandbox-cmd allowlists
    Roles.py                     # AgentRole enum
  sandbox/
    sandbox_tools.py             # DockerSandboxTools: create/exec/destroy + repo_diff (DB-backed). Pure sandbox exec — no remote git
    Tools.py                     # Tools enum — sandbox tool identifiers, dispatched via match/case in gateway
    Dockerfile                   # Base sandbox image (agentic-sandbox:0.1)
  gitops/
    gitops.py                    # GitOps — GitHub boundary (ADR 0012): clone_and_checkout (honors base_ref), push_branch (agent/run-<run_id> or per-repo override; default-branch push hard-refused), create_pr (real GitHub PR, ADR 0027). Token centralized here
    pr_body.py                   # build_pr_body — deterministic PR description from run data (objective + microtasks + fileset), no LLM (ADR 0027)
    commit_message.py            # build_commit_message — deterministic default commit message from run data, no LLM. SINGLE source for both the deploy node (what it pushes) and the deploy gate's prefill (what the human approves) — they diverged once, see ADR 0035
    github_api.py                # GitHubRepoClient — repo validation (GET /repos/{owner}/{repo}) + live branch list for the repo registry (ADR 0024) + create_pull_request (POST /pulls, the deploy auto-PR, ADR 0027)
  db/
    db.py                        # SQLAlchemy engine + session_scope
    event_log.py                 # EventLogger — centralized Event-row construction (session.add only, never commit)
    models.py                    # ORM models: Run, Event, Artifact, Sandbox, MicrotaskRecord, CodeChunk, Lesson, RepoRecord
    repo_store.py                # RepoStore — repo registry (ADR 0024): canonical URL normalization, per-repo token (Fernet+masked), resolve_token chain (repo → Settings → anonymous)
    lessons.py                   # LessonStore — per-repo cross-run failure memory (ADR 0018): write on confirmation (record_plan_lessons/record_code_lessons), read via find_lessons (pgvector top-k) + render_block. Human curation (ADR 0034): create_manual/update_lesson/delete_lesson + find_operator_rules (pinned, unranked)
    embedder.py                  # Embedder — lazy sentence-transformers (all-MiniLM-L6-v2, 384-dim)
    semantic_store.py            # SemanticStore — pgvector upsert/search/delete over code_chunks
    migrations/                  # Alembic migrations
  context_engine/                # Code semantic memory (ADR 0010)
    chunker.py                   # tree-sitter AST chunking (function/class) + whole-file fallback
    engine.py                    # ContextEngine: ingest_baseline / sync_workspace / search
  providers/                     # Pluggable LLM provider interface (TODO: implement)
  api/                           # FastAPI control plane + dashboard backend (ADR 0013) — NOT an LLM agent, bypasses ToolGateway
    app.py                       # Routes: POST /runs, /runs/{id}/resume; GET /runs/{id}{,/hitl,/events,/logs,/logs/stream}
    service/service.py           # Service — headless run engine: run/resume (one graph.invoke per gate), HITL review context, log+event feeds
    requests/requests.py         # RunRequest (objective/repo/constraints/policy/hitl/trigger)
    responses/responses.py       # RunResponse, HitlReviewContext (PlanView/DiffView), EventView (durable history), LogView (live)
  observability/
    log_bus.py                   # RunLogBus — in-memory per-run ring buffer; structlog tee → dashboard SSE (live logs, ephemeral). ADR 0013
    logging_config.py            # structlog setup + _bus_tee processor (feeds log_bus)
deploy/
  docker-compose.yml             # Postgres + pgAdmin + sandbox image builder
scripts/
  deleting_db_data_episodic_memory.sh  # Wipe all run data (keeps schema)
  snapshot_db.sh                       # Dump DB to scripts/snapshots/<label>.sql
  load_snapshot.sh                     # Restore DB from snapshot
docs/
  ARCHITECTURE.md                # AS-BUILT architecture (implemented state, demo/reference doc — start here)
  MAS_PATTERNS.md                # Catalogue of every MAS pattern + design choice used (what/where/why/cost)
  ARCHITECTURE_preview.md        # Full system spec (state machine, data flow)
  Design_preview.md              # Agent patterns and cognitive architecture
  MCP_AND_TOOLS.md               # Tool Gateway + MCP contracts
  Blackboard.md                  # Blackboard schema + event type registry
  POC_instructions.md            # Step-by-step PoC build guide
  instructions.md                # MVP implementation plan

  decisions/                     # ADRs — WHY decisions were made (read this for cross-session context, see below)
```

## Persistent memory across sessions: `docs/decisions/`
This file (`CLAUDE.md`) tells you **what's true now** — it gets rewritten and loses
history. `docs/decisions/` tells you **why** — append-only, dated ADRs (`NNNN-title.md`)
that are never deleted, only superseded. Read it when starting fresh on this project;
write to it (new `NNNN-title.md`, format in `docs/decisions/README.md`) whenever a
non-obvious architectural decision is made, an audit finding gets actioned/deferred, or
an alternative gets seriously considered and rejected — so the next session doesn't
re-litigate it cold. See `docs/decisions/0001-architecture-audit-2026-06-07.md` for the
first architecture audit and its remediation order.

**`docs/problems-log.md`** is the companion register: a scannable, newest-first table of
every non-trivial problem we hit + its root cause + fix, each linking the ADR/GH issue/commit
with the detail. `docs/decisions/` says *why we chose X*; the problems log says *what broke and
what we did*. Add a row when a real problem is diagnosed and resolved (failed run, data bug,
security gap); skip pure refactors.

## Graph topology (current)
```
START → planner → plan_reviewer → (planner | coder | hitl)        # planning loop
                  coder → tester → (reflector | hitl | coder | deploy)  # execution loop
                  tester → reflector → (coder | tester)           # FAIL: code-fix→coder | test-bug→tester
                  tester → deploy → (hitl) → deploy_push → END    # deploy (terminal)
                  hitl → (planner | coder | deploy | deploy_push | END)

bootstrapper runs OUTSIDE the graph (RunLauncher calls it directly) — see ADR 0005.
```
- **Deploy (terminal)**: on `DONE` the tester ALWAYS routes to `deploy` (gate producer →
  emits `READY_TO_DEPLOY`, funnels through the shared `hitl` gate — every mode, dry-runs
  included, so the human sees the final diff). Approve → `deploy_push` dispatches on the
  run's **DeployMode** (ADR 0036, replaces the old `skip_deploy`/`auto_create_pr` pair):
  `dry_run` → ship nothing (`gitops.deploy_skipped` event); `push` → `gitops.GitOps`
  stage/commit/push to `agent/run-<run_id>` (bypasses ToolGateway — not an LLM agent);
  `pr` → push + GitHub PR; `local_only` → git-bundle the sandbox commit and fetch it into
  the user's local clone (`RunRequest.local_export_path`, filesystem-422-validated at
  creation; `GitOps.export_local_branch` + `fs_read_file_bytes`; failure FAILS the run,
  no degrade; `gitops.exported_local` event). Then END. Reject → `DONE` → END (run
  finished, un-deployed). See ADR 0012/0036. `RunRequest.deploy_mode` (None → global:
  `defaults.auto_create_pr` True → `pr`, False → `push`) is resolved ONCE at creation by
  `Service.resolve_deploy_mode` onto `BlackboardState.deploy_mode` — the deploy node
  reads that, NOT Settings live, so the rights run-start preflighted (ADR 0029: push/pr
  need push access, pr needs PR access, dry_run/local_only need clone only) are the ones
  it deploys with (ADR 0035 invariant). Mirrored onto `runs.deploy_mode` = the run's
  label on the dashboard list. **Push branch** (ADR 0035): per-run
  `RunRequest.push_branch` (→ `Repo.branch_to_push_to`, validated at run creation → 422)
  › registry `push_branch_override` › `agent/run-<run_id>`; the repo's default branch is
  refused at four layers (for `local_only` the same name is the branch created in the
  local clone — must not already exist there, 422). Commit message: gate-approve input ›
  per-run `RunRequest.commit_message` › `gitops/commit_message.py::build_commit_message`
  (objective title + microtasks + fileset — the gate PREFILLS this exact string, ADR 0035).
  PR failure degrades, never fails the run (ADR 0027).
- **Planning loop**: `planner → plan_reviewer`. Reviewer rejects (`NEEDS_PLAN`) → back to
  planner to revise; approves (`NEEDS_CODE`) → coder.
- **Execution loop** (ADR 0016): `coder → tester`. The **Tester owns its verdict**
  (deterministic, from `evidence.checks.unit_tests`). On a **PASS** it marks the microtask
  COMPLETED/DONE and emits `NEEDS_CODE` (more microtasks → coder) or `DONE` (final →
  deploy); the **CODE** gate fires here. On a **FAIL** (`REFLECT`) it routes to the
  **failure-only Reflector**, which diagnoses, seeds a `Reflection`, and is the SOLE path
  back to coder after a failure (`NEEDS_CODE`) — no blind retries, and no LLM call on a
  green pass. The reflector also emits a `fix_target` (GH #33): `code` → coder retry;
  `test` → **tester** re-synthesizes the wrong test (`NEEDS_TEST`, note via
  `test_rejection_note`), bounded by `test_resynth_count < REFLECTOR_MAX_TEST_RESYNTH`
  (then it falls back to the code path; the counter resets when the coder runs).
- **HITL gate** (shared `hitl` node, see below + ADR 0006): any phase in
  `hitl.hitl_checkpoints` routes through `hitl` before advancing.

## When HITL is triggered
A phase emits `NEEDS_HITL_APPROVAL` (via `HitlGate.apply`, `orchestrator/hitl.py`) and the
graph suspends at the `hitl` node (`interrupt()`) ONLY when that phase is listed in
`hitl.hitl_checkpoints` (set with `--hitl-checkpoints plan,code,deploy`; empty = no gates).
The field lives on the `HITL` state model (was `Policy.hitl_checkpoints`; moved + retyped
`List[str]` → `List[Phase]` — see ADR 0014). There are three gates (`Phase`: PLAN, CODE,
DEPLOY — no TEST gate; the gate enum is single-sourced in `state.py`, ADR 0016).
The gate points and their resume routing:

| Gate   | fires after                | approve →                       | reject (note required) → |
|--------|----------------------------|---------------------------------|--------------------------|
| `plan` | plan_reviewer approves (`NEEDS_CODE`) | coder                | planner (note → `plan.review_feedback`) |
| `code` | **tester passes** (`NEEDS_CODE`/`DONE`) | coder (next microtask) / deploy (if final) | reset microtask → IN_PROGRESS + seed a `Reflection` → coder |
| `deploy` | tester passes final → `deploy` node (`READY_TO_DEPLOY`) — fires for EVERY DeployMode, dry-run included (ADR 0036) | `deploy_push` executes the run's mode: dry_run = nothing ships / push / pr / local_only = bundle → local clone. Optional `commit_message` in the approve payload overrides the run's message (ADR 0027) | END, un-deployed (`DONE`; no loop-back, no note channel) |

Reject ALWAYS requires a note (no note = re-`interrupt()`, stays paused). A `code` reject
does NOT re-run the reflector — it resets the tested microtask to IN_PROGRESS and seeds a
`Reflection` carrying the human note directly for the coder retry (`hitl._write_feedback`).
Resume via `python main.py --run_id <id> --approve | --reject --note "..."`.

## Run the pipeline
```bash
# 1. Start Postgres
docker compose -f deploy/docker-compose.yml up -d

# 2. Build sandbox image (only when Dockerfile changes)
docker compose -f deploy/docker-compose.yml --profile donotstart build sandbox-image-builder

# 3. Run migrations
cd /home/kristian/project/AgenticDevOps && venv/bin/alembic upgrade head

# 4a. Run via CLI (being deleted — see ADR 0013; prefer the API below)
cd src
../venv/bin/python main.py \
  --repo_url https://github.com/owner/repo \
  --base-sha <SHA> \          # omit for brand-new repos with no commits
  --task_description "Your task here"

# 4b. Run via Control API (dashboard backend — the going-forward surface)
cd src
../venv/bin/uvicorn api.app:app --port 8000     # serves REST + SSE; needs Postgres up
#   POST /runs                         trigger a run (RunRequest: repo_id from the registry OR legacy raw repo; repo+LLM preflights — 4xx = no run created)
#   GET/POST /repos, PUT/DELETE /repos/{id}   repo registry CRUD (ADR 0024) — POST validates against GitHub before storing
#   POST /repos/{id}/validate          re-validate a repo entry (status dot)
#   GET  /repos/{id}/branches          live GitHub branch list (base-branch picker, never DB-cached)
#   GET/POST /repos/{id}/lessons       lesson store: read the repo's memory / add an operator rule (ADR 0034)
#   PUT/DELETE /lessons/{lesson_id}    correct or forget a lesson (edit ⇒ re-embed + retraction immunity)
#   GET  /runs/{id}                    poll status / run_state / hitl_phase
#   GET  /runs/{id}/hitl               paused-gate review context (plan or green/red diff)
#   POST /runs/{id}/resume             {decision: approve|reject, note}
#   POST /runs/{id}/stop               hard-stop a live run (blocks until driver thread exits, ADR 0033)
#   DELETE /runs/{id}                  permanently delete a terminal run + its data (409 if live, ADR 0033)
#   GET  /runs/{id}/logs/stream        live logs over SSE (structlog bus)
#   GET  /runs/{id}/events?after=<seq> durable history (events audit table)
```

## Control API ↔ graph (ADR 0013)
The API in `src/api/` is the dashboard's only backend — it drives the **same checkpointed
graph**, it is not a second engine. Key invariants:
- **One `graph.invoke()` per gate.** `POST /runs` drives to the first HITL pause or END;
  each `POST /resume` (`Command(resume=HitlResume…)`) advances exactly one gate. Runs in a
  `BackgroundTasks` worker → endpoints return `202` immediately; the dashboard polls.
- **Resume does NOT re-bootstrap** (the CLI did, because each `--approve` was a cold process).
  A HITL pause is a mid-graph suspend in the same live process — sandbox + checkpoint intact.
- **Approve/reject routing is unchanged** — `HitlGate.resolve()` sets the next status and
  routes the note into the existing channel (plan→`review_feedback`, code/test→`Reflection`).
- **Two log feeds, by purpose:** *live* = structlog tee'd into `observability/log_bus.py` →
  SSE (ephemeral, granular: `node.enter`, tool steps); *durable history* = `events` table
  polled by `seq`. The logger is the live source; the table is the record. Right-sized to the
  self-hosted single-user deployment — no resumable-SSE / LISTEN-NOTIFY machinery.

## DB management scripts
```bash
# Wipe all data for a fresh run (keeps schema/migrations)
./scripts/deleting_db_data_episodic_memory.sh

# Save DB snapshot before a run
./scripts/snapshot_db.sh <label>

# Restore from snapshot
./scripts/load_snapshot.sh scripts/snapshots/snapshot_<label>.sql
```
Scripts use `docker exec` into `deploy-postgres-1` — no local Postgres client needed.
Override container name: `POSTGRES_CONTAINER=<name>` in `.env`.

## Environment variables (`.env` in project root)
**Settings hub (ADR 0020):** the LLM vars below (`CLUSTER_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`,
`LLM_RETRIES`) and every `*_MAX_TOOL_STEPS` / `*_TIMEOUT_SECONDS` / `CODER_MAX_*` /
`PLANNER_MAX_SYNTHESIS_ATTEMPTS` / `REFLECTOR_MAX_TEST_RESYNTH` knob are now **seeds/fallbacks
only** — they populate `app_settings` on first boot; after that the UI (DB) is the source of
truth and per-run overrides win. Edit them in the dashboard Settings page, not `.env`.

| Variable | Purpose |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_HOST` / `POSTGRES_PORT` | Single source of truth for DB creds — docker-compose inits the DB from them; app (`db.py`) + Alembic build the connection URL from them (`Database.sqlalchemy_url()`). Also used by DB scripts |
| `DATABASE_URL` | Optional override — set to point app + Alembic at an **external** Postgres (RDS/Supabase). When set, wins over `POSTGRES_*`. Leave unset for the local compose DB |
| `CLUSTER_API_KEY` | API key for LLM cluster endpoint |
| `LLM_MODEL` | Model name for LLM cluster endpoint (default `google/gemma-4-31B-it`) |
| `USE_TOOLS` | `true` to enable planner's ReAct investigation tools |
| `PLANNER_MAX_TOOL_STEPS` | Max tool call rounds for planner investigation |
| `TESTER_MAX_TOOL_STEPS` | Tool-call rounds for the test-writer ReAct loop (default 6 → recursion limit 17). Kept tight so a wandering synth fails fast; on GraphRecursionError the SynthesisPhase degrades (no crash). The test-writer is NOT given `get_repo_diff` (avoids a verify-loop) |
| `PLANNER_MAX_SYNTHESIS_ATTEMPTS` | Max synthesis retries when `_normalize_output` raises `BadMicroTaskException` (default 3) — Planner feeds the validation error back into the next prompt as context |
| `CODER_MAX_TOOL_STEPS` | Max tool call rounds for the coder's ReAct loop (default 16) |
| `CODER_MAX_EMPTY_DIFF_RETRIES` | Re-prompt attempts when the post-loop `repo.diff` comes back empty (default 2) |
| `CODER_MAX_COMPILE_RETRIES` | Warm in-loop compile-fix attempts when the target files don't compile (default 2). Coder needs `node` in its allowlist for the JS `node --check`; Python `py_compile` runs via `python` |
| `REFLECTOR_MAX_TEST_RESYNTH` | Bound on the reflector→tester test re-synthesis loop when the reflector judges the synthesized TEST (not the code) wrong (default 2). After the cap it forces the code path; reset when the coder runs (GH #33) |
| `CODER_TIMEOUT_SECONDS` | LLM request timeout for the Coder (default 120 — code gen is long) |
| `TEAR_DOWN_SANDBOX_AFTER_RUN` | Legacy dev flag (planner node). The real control is the UI setting `defaults.teardown_on_terminal` — destroys the sandbox in `Service._finalize` only when the run is truly terminal (DONE/FAILED, never STALLED). ADR 0021 |
| `GITHUB_TOKEN` | Seed/fallback only (ADR 0021) — the token now lives in Settings (`AppSettings.github_token`, masked in UI, Fernet-encrypted at rest), read via `SettingsStore.github_token()` by clone + push. Never logged |
| `SETTINGS_ENCRYPTION_KEY` | Optional Fernet key for secrets-at-rest (`db/secrets.py`). Unset → auto-generated `.settings_key` file (gitignored). Protects DB dumps/snapshots, not a compromised machine |
| `EMBEDDING_MODEL` | sentence-transformers model for the Context Engine (default `all-MiniLM-L6-v2`, 384-dim). Changing it requires matching `EMBEDDING_DIM` + a new `code_chunks` migration |
| `LOG_LEVEL` | Global structlog threshold (default `INFO`) |
| `LOG_LEVEL_<CATEGORY>` | Per-category override, e.g. `LOG_LEVEL_TOOL=DEBUG`, `LOG_LEVEL_SEMANTIC=DEBUG` (categories in `observability/categories.py`) |
| `LOG_JSON` | `true` → one JSON object per line (prod); `false` → pretty console (default) |
| `LOG_COLORS` | `auto` (color when stdout is a TTY, default) \| `true` \| `false`. `NO_COLOR` env also disables |

## LLM provider (settings hub — ADR 0020)
Provider/model/knobs are UI-managed (DB source of truth; `.env` = seed/fallback only). The
single construction site is `providers/llm.py::build_chat_llm(provider, llm, timeout)` which
dispatches by provider type — `ChatOpenAI` for `openai_compatible` (the cluster / any
OpenAI-shaped endpoint), `ChatAnthropic` for `anthropic` (Claude). Settings live in the
`app_settings` row via `db/settings_store.py` (`SettingsStore.get/get_public/update`,
`resolve_run_config`). A run resolves ONE `RunConfig` at creation (per-run override › global
DB › env seed), stored on the blackboard (`state["config"]`); **agents take `config` as a
mandatory arg — the fallback (`… or resolve_run_config()`) lives in the node**. `POST /runs`
**preflights** the provider (`providers.health_check`) and refuses to start on a dead/mis-keyed
one. Secrets (provider API keys + GitHub token) are masked on read AND Fernet-encrypted at
rest (`db/secrets.py`, key = `SETTINGS_ENCRYPTION_KEY` env or auto-generated `.settings_key`
file) — see ADR 0021. Uncaught graph crashes mark runs STALLED (continuable), never FAILED;
failed runs expose `failure_reason` (latest `run.failed` event) on the API responses.

## DB schema (all tables)
| Table | Purpose |
|---|---|
| `runs` | One row per run — repo_url, repo_id (nullable registry ref, no FK), base_sha (nullable), status, timestamps, `tokens_used_total` (denormalized mirror of the checkpointed `Iteration.tokens_used_total`, ADR 0025), `deploy_mode` (the run's label: dry_run/push/pr/local_only, nullable for pre-ADR-0036 rows, migration `e8f9a0b1c2d3`) |
| `repos` | Repo registry (ADR 0024) — canonical repo_url (unique; the lessons/code_chunks memory key), label, optional per-repo token (Fernet `enc:`, masked in API), default_branch, push_branch_override, last validation snapshot. Migration `f6a7b8c9d0e1` also backfilled canonical URLs into runs/lessons/code_chunks |
| `sandboxes` | Docker container ID per run — survives restarts |
| `microtasks` | Planner output persisted after each plan — survives crashes |
| `artifacts` | Plan summaries, test logs, diffs — kind field identifies type |
| `events` | Full audit trail — run.created, repo.cloned, plan.drafted, tool.executed, etc. Has a monotonic `seq` (BIGSERIAL identity) — the dashboard history cursor (`GET /runs/{id}/events?after=<seq>`). See ADR 0013 |
| `code_chunks` | Context Engine semantic index — AST chunks + `Vector(384)` embedding, `namespace` baseline/workspace (ADR 0010). Needs the `vector` extension (`pgvector/pgvector` image) |
| `lessons` | Per-repo cross-run failure memory (ADR 0018) — `Vector(384)` embedding, indexed by `repo_url`/`run_id`, `scope` plan/code. Written on CONFIRMATION (reviewer approve / tester PASS), retrieved semantically top-k (`find_lessons`, current-run excluded), injected advisory into **planner**/reviewer/coder/reflector prompts. Migration `b2c3d4e5f6a7`. **Human curation (ADR 0034, migration `d7e8f9a0b1c2`):** `agent="human"` + nullable `run_id` = an operator rule typed in the dashboard (pinned, ALWAYS injected, framed as a rule — never ranked); `microtask_id` + `updated_at` added (the only non-derivable provenance facts; `updated_at` = human edit → also grants immunity from `retract_run`) |
| `checkpoints*` | LangGraph PostgresSaver — blackboard snapshot per superstep (ADR 0005; replaced `run_state`) |

Models use **SQLAlchemy 2.0** style: `Mapped[T]` + `mapped_column()`. Never use 1.x `Column()` style — Pylance will report type errors.

## Current implementation status
| Component | Status |
|---|---|
| Blackboard / state models | Done |
| DB schema + Alembic | Done |
| DockerSandboxTools (DB-backed) | Done |
| ToolGateway | Done |
| Bootstrapper node | Done |
| Planner (ReAct + structured output) | Done |
| DB persistence (microtasks, events, artifacts, Run.status) | Done |
| Planner↔Reviewer + Coder→Tester→Reflector loops (wired) | Done |
| HITL gates (plan/code/test, native `interrupt()`) | Done — see ADR 0006 |
| Plan Reviewer | Done — two-phase (independent investigation via read-only tools, then structured approve/reject). Appends a `ReviewFeedback` to the plan, emits `PLAN_REVIEWED`, caps the planner↔reviewer loop at `Constraints.max_planner_reviewer_iterations` (→ FAILED + `RUN_FAILED`; renamed from the misleading `max_total_iterations` — it only ever bounded this planning loop, ADR 0031). See ADR 0011. **Convergence fix (ADR 0022, GH #43):** planner sees only the LATEST review verdict (not the stacked reject history); `ReviewerOutput.blockers` (structured, findings-only) is forwarded to the planner instead of the reasoning-prone `notes` |
| Coder | Done — hybrid grounding (runner pre-reads target/context files), single ReAct loop, deterministic diff verification, per-microtask attempt limit enforced. **Compile gate** (GH #27): after the diff, the runner compiles the microtask's target files (`py_compile` / `node --check`); on failure it appends the error to the SAME ReAct conversation and re-runs (warm, no cold re-prompt), up to `CODER_MAX_COMPILE_RETRIES`. On exhaustion it proceeds — the Tester's build check is the deterministic backstop |
| Tester | Done — phase-based pipeline (ADR 0016): `TestPipeline` runs ordered `TestPhase`s under `agents/Implementing/Tester/phases/` (detect → deterministic-checks → synthesis → trivial-guard → execute → verdict). Two-track grounding: **Track A** (`DeterministicChecksPhase`) runs the repo's existing suite + **build/typecheck/lint** (config-gated; build+typecheck+suite gate, lint advisory); **Track B** LLM-synthesizes tests from the Planner's `acceptance_criteria` (independent of the Coder), guarded by a deterministic AST/regex assertion check (`grounding.py`). Combined verdict emitted as the canonical `unit_tests` check. Tester owns its PASS/FAIL verdict; CODE gate fires on pass. **Value-grounding (ADR 0022, GH #44):** synthesis asserts STRUCTURE ONLY for any case the Planner gave no `IOExample` — never a self-computed value/number/distance/message (an LLM can't reliably compute these); the Planner is nudged to supply examples for precise-value/boundary cases. **Unverified verdict (ADR 0037):** "supported project, nothing decided" is now SKIPPED pass-through (loudly labelled UNVERIFIED), NOT FAILED — a fail nothing can flip looped reflector+coder to BLOCKED on correct code (run 27198e26); synthesis refuses harness-untestable targets up-front (.tsx under pytest → `synth_untestable_reason`); target-scoped subproject Track A (`_subproject_js_checks`: nearest package.json → `npm --prefix` build / tsc, gating) covers JS apps nested in non-JS repos |
| Reflector | Done — failure-only (ADR 0016): reached ONLY on a tester FAIL (`REFLECT`). Diagnoses, seeds a `Reflection`, and routes by `fix_target`: `code` → coder retry (`NEEDS_CODE`); `test` → tester re-synthesizes the wrong test (`NEEDS_TEST`, bounded by `REFLECTOR_MAX_TEST_RESYNTH`, GH #33). No approve path / no LLM call on green (the Tester owns the PASS verdict). **Spec-aware routing (ADR 0022, GH #44):** the Phase-2 diagnosis prompt now injects the microtask's `acceptance_criteria` + the failing synthesized test source, so `fix_target=test` correctly fires when a test asserts a self-computed value contradicting the spec (previously the diagnosis was blind to the criteria → always routed `code`). **Honest evidence (ADR 0037):** prompt sections use `_elide_middle` (head+tail, marker names itself a DISPLAY cut — the old `diff[:2000]+" [truncated]"` made the reflector diagnose "truncated implementation" on complete code, run 27198e26); diff budget 12k; SCOPE paragraph stops it demanding later-microtask integration |
| Context Engine (pgvector) | Done — code semantic memory: `baseline`+`workspace` namespaces, local MiniLM (384-dim) embeddings, tree-sitter chunking, `semantic.search` Planner tool. Baseline ingested in Bootstrap after clone; workspace synced by coder node after a diff; purged on sandbox teardown. See ADR 0010 |
| GitOps (webhooks + PR) | Partial — `gitops.GitOps` class (GitHub boundary, ARCHITECTURE §4.4) owns branch/commit/push/PR; terminal `deploy`/`deploy_push` nodes drive it to push the sandbox to `agent/run-<run_id>` (bypasses ToolGateway — not an agent), HITL-gateable (`--hitl-checkpoints deploy`), emits `gitops.pushed`. **Deploy modes (ADR 0036):** per-run `DeployMode` — dry_run / push / pr (push + real PR: `GitHubRepoClient.create_pull_request` POSTs `/pulls` with a deterministic body `gitops/pr_body.py`, emits `gitops.pr_created`, URL on `Repo.pr_url`; PR failure degrades via `pr_error`, 422 already-exists resolves to the existing PR) / local_only (`GitOps.export_local_branch`: shared `_stage_and_commit` → `git bundle` → `fs_read_file_bytes` (get_archive) → host `git fetch` into `RunRequest.local_export_path`; failure = `LocalExportError` → run FAILED, no degrade). Commit message user-configurable (deploy-gate approve payload › per-run `RunRequest.commit_message` › default, ADR 0027). Still no webhooks/CI relay/merge. See ADR 0012. **Deploy hygiene (ADR 0019):** bootstrap seeds a `.gitignore`; `push_branch` stages with pathspec excludes (deps/secrets/`.agentic_tests/` never shipped); `scan_secrets` runs **gitleaks** over the tree before the push and **hard-blocks** on any finding (emits `gitops.secret_blocked`, run→FAILED); Tester synth tests are ephemeral (`.agentic_tests/`, git-ignored) |
| Control API + dashboard backend | Done — FastAPI (`src/api/`, ADR 0013). Trigger runs + HITL approve/reject over HTTP (`POST /runs/{id}/resume` → `Command(resume=...)`); one `graph.invoke()` per gate (no re-bootstrap on resume — mid-graph pause, sandbox alive). HITL review context (`GET /runs/{id}/hitl`): plan + microtasks for a `plan` gate, raw green/red diff for a `code` gate. Notify by poll (`GET /runs/{id}` → `run_state`/`hitl_phase`). Live logs: structlog → in-memory `RunLogBus` → SSE (`/logs/stream`); durable history: `events` table poll (`/events?after=<seq>`). Runs in `BackgroundTasks` (move to a worker before prod). **Concurrent runs supported (ADR 0032):** the checkpointer is backed by a psycopg `ConnectionPool` (not a shared connection), so multiple runs' `graph.invoke`s — and a `get_state` poll racing a superstep write — each borrow their own connection; `Service._active` is lock-guarded and `mark_active` is an atomic test-and-set (a second driver on the same run 409s). We parallelize ACROSS runs; one run stays single-threaded. CLI (`main.py`) being deleted — don't mirror it |
| Provider abstraction + settings hub | Done (ADR 0020) — `providers/llm.py` factory (OpenAI-compat + Anthropic), UI-managed `app_settings` (providers/model/knobs), per-run override, preflight healthcheck. Closes provider-coupling TODO #6. **Token usage tracking + run-wide budget + dashboard surfacing (ADR 0025):** every agent's real token usage is captured (`providers/token_usage.py`) into per-microtask debug files, `llm.usage` logs/events (tagged with `microtask_id` for Coder/Tester/Reflector), and `Iteration.tokens_used_total` (mirrored onto `runs.tokens_used_total`); `Constraints.max_total_tokens` is enforced across all five nodes. Exceeding the cap stalls the run rather than failing it: a node raises `TokenBudgetExceededError` (not `RunExecutionError`), which `Service._drive` catches and turns into a non-terminal STALLED run (checkpoint intact) — recoverable via `POST /runs/{id}/continue` or closed via `POST /runs/{id}/fail`, reusing the same STALLED machinery as transient LLM errors/crashes. Dashboard: `RunOverview` shows a live "tokens used / cap" tile (summed client-side from `llm.usage` events) plus the cap's raise history ("token cap at start" + each `run.token_budget_raised`); `TokensByAgent` and the `MicrotasksPanel` break the same events down per-agent and per-microtask; `llm.usage` events are visible in the event timeline unconditionally (no longer folded into the tool-call toggle). Settings' nullable-numeric-field bug fixed so `max_total_tokens` can actually be cleared to "no cap"; New Run silently passes the global cap through, same pattern as the other constraints. `RunResponse.stalled_kind` ('token_budget' vs 'transient', read back from the `RUN_STALLED` event's `kind` payload) lets the dashboard show a "tokens to add" input for a token-budget stall — **mandatory** in that case (`POST /runs/{id}/continue` 422s without it; a plain/transient stall still accepts a blank body as a no-op retry). **Amendment 2026-07-08 (ADR 0025):** both the Settings `max_total_tokens` field and this continue-bar input are entered in thousands (UI convention only — the backend/DB stay in raw tokens throughout; carried by a `scale` property on the settings schema field descriptor). `POST /runs/{id}/continue` takes an optional `additional_tokens` body field (renamed from `max_total_tokens`) that ADDS to the checkpointed cap — not a new absolute cap — applied synchronously via `Service.raise_token_budget` (`graph.update_state` patches the checkpointed `Constraints`, mirrors the new cap onto `runs.max_total_tokens`, emits `run.token_budget_raised` with `old_max_total_tokens`/`new_max_total_tokens`/`additional_tokens`) before the background re-drive — a non-positive amount, or one that still leaves the new cap at/below tokens already used, 422s instead of silently re-stalling. `fmtTokens` (RunOverview/TokensByAgent/MicrotasksPanel) switches from `k` to `M` above 1,000,000 |
| State snapshots (LangGraph PostgresSaver checkpointer) | Done — see ADR 0005 (replaced `run_state` table) |
| `reflections` DB table | Not started (reflections live in the checkpointed blackboard; subsumed by the `lessons` table for cross-run reuse) |
| Per-repo lessons memory | Done (ADR 0018) — `lessons` table + `db/lessons.py`. **Write** (outcome-validated): `record_plan_lessons` on reviewer APPROVE, `record_code_lessons` on tester PASS (gated `fix_target=="code"` + `should_store_in_semantic_memory`); `record_test_lessons` is a deliberate no-op. **Read**: `find_lessons` (pgvector cosine top-k, repo+scope filtered, current run excluded) + `render_block` → advisory prompt block in **planner**/reviewer/coder/reflector, also dumped to each agent's debug output file. Open: per-repo prune/cap hygiene; retrieval-usefulness reinforcement |
| Lesson store (human curation) | Done (ADR 0034) — dashboard page (top bar → Lessons; `LessonStorePage.tsx`) over `GET/POST /repos/{id}/lessons` + `PUT/DELETE /lessons/{id}`. **Operator rules** (`agent="human"`, no run): typed by a human, ALWAYS injected via `find_operator_rules` (unranked, capped per scope at `OPERATOR_RULE_LIMIT`=15) in their own "follow these" block above the advisory ones — deliberately not a vector search, so a fresh rule can't lose the top-k race and silently do nothing. The cap keeps the NEWEST rules, so `LessonView.injected` (derived server-side by `Service._injected_rule_ids` from the same constant) marks displaced rules "not sent to the agents" in the UI — a stored-but-inert rule must never look active. **Provenance is DERIVED** (`dashboard/src/lessonInfo.ts`, shared by the store page + run panel): `agent`+`scope` ⇒ the "why" narrative; only `microtask_id`/`updated_at` are stored (not derivable). NB `originLabel` derives the author from `scope`, NOT from `agent` — `agent` is the node that *confirmed* the lesson (tester for code lessons), not the reflector that diagnosed it. **`retract_run` skips human-touched lessons** (`updated_at` set or `agent="human"`). **Planner joined the read path** here — closes the loop at prevention, not detection; accepted risk = actor-critic coupling |

## Known bugs / remaining TODOs (priority order)
See `docs/decisions/0001-architecture-audit-2026-06-07.md` for full evidence + fixes — fix 1-2 below *before* wiring real Coder/Tester/Reflector, the blast radius multiplies otherwise.
1. **Stop conditions PARTIALLY enforced** (remaining half: must-fix, safety): `CoderRunner._check_stop_limit` refuses a microtask when `attempts >= (Microtask.max_attempts or Constraints.max_microtask_attempts)` → FAILED, killing the runaway coder↔reflector loop. The planner↔reviewer loop is now also capped — `ReviewerRunner.invoke` fails the run when `Iteration.planner_reviewer_iteration_current >= Constraints.max_planner_reviewer_iterations` (enforced in the node, not the router; counter incremented via the node-return dict; field renamed from `max_total_iterations`, ADR 0031). A run-wide **token** budget is now also enforced across all five agent nodes — `Constraints.max_total_tokens` vs. `Iteration.tokens_used_total`, checked at the top of every node's `invoke()` (see ADR 0025, `orchestrator/token_budget.py`) — but unlike the two caps above it does NOT fail the run: it raises `TokenBudgetExceededError`, which stalls the run (recoverable via `/continue` or `/fail`, and via `/continue`'s cap-raising body, ADR 0025) instead. The coder↔tester↔reflector loop is bounded **per-microtask** by `Constraints.max_microtask_attempts` (`_check_stop_limit`); a separate run-wide cap on that loop was considered and **deliberately rejected** (ADR 0031) — with a fixed plan the per-microtask cap already bounds total cycles, so `Iteration.coder_tester_reflector_iteration_current` stays a dead field. STILL MISSING: a single whole-run `global_iterations_used` counter (currently dead — `global_iteration` + `global_iterations_used` are redundant, collapse to one).
2. ~~**`run_state` never written**~~ — **SUPERSEDED by ADR 0005**: hand-rolled `run_state` snapshots replaced by the LangGraph PostgresSaver checkpointer (snapshots every superstep, keyed by `thread_id=run_id`). `run_state` table dropped.
3. ~~**`ToolGateway` commits mid-transaction**~~ — **FIXED**: `_log_event` now delegates to `EventLogger.log()` (`db/event_log.py`), which only ever calls `session.add()`. Commit ownership correctly sits with the lending `session_scope()`.
4. ~~**`planner → END` shortcut**~~ — **FIXED**: full loops wired (`planner → plan_reviewer → (planner | coder)`, `coder → tester → reflector → coder`), plus the shared `hitl` gate (ADR 0006). Phase agents are still stubs (#8).
5. **`check_requested_path` dead code** (should-fix, security): defined in `gateway.py` but never called — `fs.read_file`/`fs.write_file` skip the sandbox-path-traversal check entirely.
6. **Provider coupling in planner**: `ChatOpenAI` instantiated directly. Should use `providers/` abstraction.
7. **`reflections` table missing**: reflections live in the checkpointed blackboard (so they survive a crash now, via ADR 0005), but there's no queryable `reflections` table for cross-run semantic memory. The `code`/`test` HITL-reject path already seeds `Reflection`s (ADR 0006) — a dedicated table makes them analyzable.
8. ~~**Global sandbox command allowlist**~~ — **FIXED**: `tool_gateway/permissions.py` gives each role its own `RolePermissions` (`tools` + `allowed_commands`); `gateway.py` calls `get_permissions(role)`. `CoderPermissions.allowed_commands` no longer includes `pytest` — Coder is now gateway-blocked from running tests, not just prompt-forbidden. Reflector is still a stub returning `{}`, but the stub Tester (`tester_node.py`) now marks microtasks COMPLETED and the coder<->tester loop cycles — see graph topology above.
9. ~~**Tester under-grounded**~~ — **DONE (ADR 0016)**: phase-based `TestPipeline`, two-track grounding. Track A (`DeterministicChecksPhase`) runs the existing suite + config-gated build/typecheck/lint (build+typecheck+suite gate, lint advisory); Track B synthesizes from structured `acceptance_criteria` (`List[AcceptanceCriterion]` + `IOExample`s, migration `b1c2d3e4f5a6`) gated by a deterministic assertion guard (`grounding.py`). Tester owns its verdict; Reflector failure-only. Node is baked into `sandbox/Dockerfile`. STILL OPEN: SynthesisPhase "only when uncovered" precision; typecheck/lint run only when the repo configured the tool; reflector-as-judge (GH #26) + coder compile self-check (GH #27) under discussion.

## Coding conventions
- All state mutations must go through the LangGraph node return dict — never mutate `state` in place.
- Every tool invocation must emit a `tool.executed` or `tool.denied` event via `ToolGateway`.
- Never construct `Event(...)` rows directly — use `EventLogger.log(session, run_id, actor, event_type, payload)` (`db/event_log.py`). It handles id generation + timestamp and only calls `session.add()` — keeps commit-boundary ownership with the caller's `session_scope()`.
- Agents return `Dict[str, Any]` matching BlackboardState fields — only include changed keys.
- Tool functions in `sandbox_tools.py` return `ExecResult` dataclasses — never raw strings.
- New agents go in `src/agents/<Phase>/<agent_name>.py` and must be wired into `orchestrator.py`.
- New tool types must be registered in `sandbox/Tools.py` (the `Tools` enum), and in `permissions.py` — add/extend the relevant `RolePermissions` subclass's `tools` / `allowed_commands`, plus `Roles.py` for new roles.
- Sandbox is always pre-created by `bootstrapper_node` — agent nodes must never call `sandbox_create()`. Get sandbox_id from `state.get("sandbox").sandbox_id`.
- `Run.status` must be updated in DB whenever LangGraph status changes — do it in the node, not in a router.
- Use OOP for new classes.
- Operational logging: never `print()`. Use `from observability import get_logger, Category; _log = get_logger(Category.X)` (module-level) then `_log.info("dotted.event.name", key=val, ...)`. This is ephemeral debug/ops output (console or JSON), SEPARATE from the durable `events` audit trail — keep emitting `EventLogger` events too. `run_id` is auto-attached (bound once in `RunLauncher`). Interactive CLI prompts (HITL `input()` banners) stay as `print()`. Never name a package `logging` (shadows stdlib) — the package is `observability/`.

## Adding a new agent
1. Create `src/agents/<Phase>/<name>.py` with a class that has `invoke(state: BlackboardState) -> Dict[str, Any]`
2. Add a node wrapper in `orchestrator.py` following the `planner_node` pattern — call
   `self._check_stop(state)` as its first line (ADR 0033 amendment) so a human Stop
   request bounds the run at this node too; there's no automatic enforcement of this,
   it's easy to forget
3. Register the node: `builder.add_node("<name>", <name>_node)`
4. Wire edges and add routing logic if needed
5. Define which tools the agent can access in `Roles.py`
6. After `invoke()` returns: update `Run.status` in DB, emit lifecycle event, persist any new data

## Event types (must use these for DB logging)
See `docs/Blackboard.md` §6.7 for full registry. Key ones:
- `run.created`, `run.status_changed`, `run.completed`, `run.failed`
- `sandbox.created`, `sandbox.command_finished`
- `repo.cloned`, `gitops.pushed`, `gitops.pr_created`, `gitops.exported_local`, `gitops.deploy_skipped` (ADR 0036)
- `plan.drafted`, `plan.reviewed`, `plan.approved`
- `microtask.started`, `microtask.completed`, `microtask.failed`
- `tool.executed`, `tool.denied`
- `test.unit_completed`, `check.lint_completed`
- `reflection.created`
- `semantic.baseline_ingested`, `semantic.workspace_synced`
- `llm.usage` (per agent-node invocation — tokens in/out/total; see ADR 0025)
- `run.token_budget_raised` (human raises `Constraints.max_total_tokens` on a stalled run via `/continue`; see ADR 0025)
- `run.stopped` (human hard-stops a live run via `/runs/{id}/stop`; a raw `Run.status = "STOPPED"` string, not a `RunStatus` enum member; see ADR 0033)
