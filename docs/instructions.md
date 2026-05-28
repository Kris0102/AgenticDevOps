# instructions.md — MVP Start (Docker-per-run, Pluggable Models)

**Date:** 2026-05-25  
**Goal:** Bootstrap an Agentic DevOps Pipeline MVP that is **auditable**, **safe-by-construction**, and **reproducible**.

This doc assumes:
- **Docker-per-run** sandboxing from day one
- **Pluggable model providers** (run “from your chair” against a cluster endpoint)
- **CLI-triggered runs** first (GitHub integration is a later milestone)

Aligned with the attached specs:
- `ARCHITECTURE_preview.md` (state machine + responsibilities)
- `MCP_AND_TOOLS.md` (Tool Gateway + MCP contracts)

---

## 0) Non‑negotiables (MVP invariants)

1. **Sandbox is the source of truth for code**
   - Agents never treat semantic memory as “the repo”.
2. **Episodic DB is the system of record**
   - Run state + events persisted so runs can be resumed/replayed.
3. **All environment actions go through a Tool Gateway**
   - deny-by-default
   - allowlists/denylists
   - every tool call logged: `tool.executed` or `tool.denied`
4. **Grounded verification loop**
   - Code is “accepted” only if deterministic checks pass (tests/lint/build logs).

---

## 1) MVP architecture (what we implement first)

### 1.1 Components (local dev via docker-compose)
- **postgres**: episodic memory (runs/run_state/events/microtasks/artifacts)
- **orchestrator**: LangGraph control-plane + CLI entrypoint
- **sandbox runner**: component that creates & manages a **Docker container per run**

> MCP servers/tools can be implemented **in-process** initially (Python modules) and later split into real MCP stdio/HTTP servers without changing the tool contracts.

### 1.2 Minimal tool surface area (MVP)
To prove end-to-end behavior, keep tools tiny:

**Filesystem / repo**
- `fs.read_file(path)`
- `fs.write_file(path, content)`
- `repo.diff()`

**Execution**
- `sandbox.run(cmd: string[], timeout_s, cwd)`
  - `cmd` must be **array-like** (no `sh -c`, no pipes, no redirects)
  - allowlisted commands only
  - always capture stdout/stderr and store as an artifact

**Episodic memory**
- `runs.create`
- `runs.get_state`
- `events.append`
- `artifacts.put/get`

---

## 2) Recommended new implementation repo layout

Do **not** implement inside the papers repo long-term (mixes research + runtime).
Create a separate implementation repository.

Suggested layout:

- `apps/orchestrator/`
  - `main.py` (CLI entrypoint)
  - `graph.py` (LangGraph nodes + transitions)
  - `state.py` (Pydantic models for run_state)
  - `agents/`
    - `planner.py`
    - `reviewer.py`
    - `coder.py`
    - `tester.py`
    - `reflector.py`
    - `prompts/` (prompt templates)
  - `tool_gateway/`
    - `registry.py`
    - `policy.py`
    - `audit.py`
    - `dispatch.py`
  - `providers/` (pluggable LLM backends)
    - `base.py`
    - `openai.py` (optional)
    - `local_endpoint.py` (optional)
  - `db/`
    - `models.py`
    - `migrations/`
- `apps/sandbox/`
  - `Dockerfile` (base image for run sandboxes)
  - `runner.py` (create container, exec, stream logs)
- `deploy/`
  - `docker-compose.yml`
- `docs/`
  - `ARCHITECTURE_preview.md`
  - `MCP_AND_TOOLS.md`
  - `instructions.md`

---

## 3) Step-by-step implementation plan (do in this order)

### Step 1 — Define Run State + Events schema (Pydantic first)
**Deliverable**
- `apps/orchestrator/state.py`
  - `RunStatus` enum:
    - `NEEDS_BOOTSTRAP`, `NEEDS_PLAN`, `NEEDS_CODE`, `NEEDS_TEST`,
      `REFLECT`, `NEEDS_FINAL_TEST`, `READY_FOR_PR`, `DONE`, `FAILED`,
      `NEEDS_HITL_APPROVAL` (reserved for later)
  - `Microtask` model:
    - `idx`, `title`, `description`
    - `acceptance_criteria` (JSON-serializable)
    - `status` (TODO/DOING/DONE/FAILED)
    - `attempts` (int)
  - `RunState` model (authoritative blackboard):
    - repo metadata (`repo_url`, `base_sha`)
    - current status + loop counters
    - microtask list + current index
    - risk flags (e.g., `touched_ci`, `touched_iac`)
    - artifact refs (latest logs, diffs)

**Why first**
- Prevents every node/agent from inventing fields.
- Enables strict validation + replay.

---

### Step 2 — Postgres schema + migrations (episodic memory)
**Minimum tables**
- `runs(run_id, repo_url, base_sha, status, created_at, updated_at, risk_level, …)`
- `run_state(run_id, version, state_json, updated_at)`
- `events(event_id, run_id, ts, actor, event_type, payload_json)` (append-only)
- `microtasks(microtask_id, run_id, idx, title, acceptance_criteria_json, status, attempts, …)`
- `artifacts(artifact_id, run_id, kind, content_address, sha256, metadata_json, created_at)`

**Rules**
- Only Orchestrator mutates `run_state`.
- Everything else is events + artifacts.

**Why**
- Crash recovery + auditability are core requirements, not optional polish.

---

### Step 3 — Docker-per-run Sandbox Runner
**Responsibilities**
- create sandbox container per `run_id`
- clone repo at `base_sha` into the container (prefer: clone inside container)
- run allowlisted commands with timeouts
- capture stdout/stderr and store as artifact (`artifact_ref`)
- return structured results to the Orchestrator

**MVP isolation controls**
- CPU/memory limits
- per-run working directory under a fixed sandbox root
- optional network restriction (see Knowledge Check)

**Why**
- This enables “grounded verification loops” immediately.

---

### Step 4 — Tool Gateway (policy perimeter + auditing)
**Implement**
- tool registry (deny-by-default)
- allowlist rules based on:
  - `agent_role` (Planner vs Tester vs Coder)
  - `run.status` / phase
- every tool call emits an event:
  - `tool.executed`: includes `tool_name`, `args_summary` (redacted), `duration_ms`, `success`, `result_refs`
  - `tool.denied`: includes denial reason + attempted args summary

**Why this works**
- Safety-by-construction: the system is safe even if a model is not.

---

### Step 5 — Orchestrator LangGraph state machine (minimal but correct)
Implement nodes:

1. `bootstrap_node`
   - create run in DB
   - create sandbox container + clone repo
   - set `status=NEEDS_PLAN`

2. `plan_node` (Planner)
   - generate microtasks + acceptance criteria
   - persist microtasks + event
   - set `status=NEEDS_CODE`

3. `review_plan_node` (Plan Reviewer)
   - approve/reject plan
   - max iterations = 3 (then require HITL or fail run)

4. microtask loop (sequential)
   - `code_node` (Coder edits files via fs tools)
   - `test_node` (Tester runs deterministic checks via `sandbox.run`)
   - if fail → `reflect_node` (Reflector reads logs + diff, proposes minimal fix)
   - attempts max per microtask = 5

5. `final_test_node`
   - run full regression suite (allowlisted)
   - set `status=READY_FOR_PR` (PR creation is next milestone)

6. `done_node`
   - set `status=DONE`, persist final summary event

**Why**
- Validates loop mechanics, stop conditions, and evidence logging early.

---

### Step 6 — CLI entrypoint (first user interface)
Command:

- `orchestrator run --repo <url> --base-sha <sha> --task "..."`

Outputs:
- `run_id`
- state transitions (status changes)
- artifact refs (test logs, diffs)
- final summary

**Why**
- Fast iteration without GitHub plumbing complexity.

---

## 4) Pluggable model provider (do early, keep interface tiny)

### 4.1 Provider interface
Define e.g.:

- `ModelProvider.generate(messages, *, model, temperature, max_tokens, tools=None) -> ModelResult`

### 4.2 Backends (later)
- OpenAI
- Local model server (cluster endpoint)
- Any future provider

**Why this works**
- Agents stay stable while you swap inference backends.
- Supports “from our chair” execution against a remote cluster.

---

## 5) Initial command allowlist (start strict)
Choose a minimal set and expand only with evidence:

**Python repos**
- `python -m pytest`
- `pytest`
- `ruff`
- `mypy`

**Node repos** (only if you target Node early)
- `npm test`
- `npm run lint`

**Explicitly do NOT allow**
- `sh`, `bash`, `zsh`
- `curl`, `wget`
- arbitrary `apt-get`, `pip install` at runtime (handle deps via sandbox image or repo lockfiles)

---

## 6) First end-to-end demo scenario (recommended)
Use a tiny fixture repo with 1–3 tests.

1. Planner generates 2–3 microtasks.
2. Coder implements microtask #1.
3. Tester runs tests → logs stored as artifact.
4. If fail: Reflector reads logs + diff → proposes minimal patch.
5. Loop until pass or attempts exceeded.
6. Persist full run history in Postgres.

Success criteria:
- You can show a run timeline from `events` and reproduce what happened.
- Every “fix” is backed by deterministic evidence (test logs).

---

## 7) Next milestones (after MVP loop works)
1. Split tools into real MCP servers (stdio first).
2. Add semantic memory (pgvector) baseline/workspace namespaces + incremental sync.
3. Add GitOps (webhooks + PR creation/update + CI signal relay).
4. Add HITL gating (PR review / ChatOps).

---

## Knowledge check (answer before we overbuild)
1) Sandbox networking for MVP:  
   - **Option A:** No network at all (hard fail outbound)  
   - **Option B:** Allow limited outbound for fetching deps (safer long-term if you pin + cache)  

2) Artifacts storage:  
   - **Option A:** Store artifacts in Postgres (simple, can get heavy)  
   - **Option B:** Store in S3/MinIO and keep only refs in Postgres (more scalable)

Which options do you want?