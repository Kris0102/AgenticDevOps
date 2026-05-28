# PoC Instructions — Agentic DevOps Pipeline (Docker-per-run, Pluggable Models)

**Date:** 2026-05-27  
**Goal:** Build a Proof-of-Concept that can:
1) create a **run** (with a persisted run_id)  
2) spin up a **Docker sandbox container per run**  
3) clone a repo at a specific SHA into the sandbox  
4) apply a code change (initially scripted, then LLM-generated)  
5) run deterministic checks (`pytest -q` or equivalent)  
6) persist a full **audit trail** (events + tool calls + artifacts) to Postgres

This PoC is intentionally minimal. It proves your *grounded verification loop* + *reproducibility* before adding MCP servers, vector retrieval, or GitHub PR automation.

---

## 0) Prerequisites (install once)

### Required software
- Docker Desktop / Docker Engine (must be running)
- Docker Compose v2 (`docker compose version`)
- Python 3.12 (local)
- `git`

### Verify Docker can run containers
```bash
docker run --rm hello-world
```

---

## 1) Create a new repo for implementation (do not use the papers repo)

Create a new repository, e.g.:
- `Agentic_DevOps_Pipeline` (recommended)

Local folder structure (create exactly this):
```text
Agentic_DevOps_Pipeline/
  deploy/
    docker-compose.yml
  apps/
    orchestrator/
      pyproject.toml
      src/orchestrator/
        __init__.py
        main.py
        db.py
        models.py
        events.py
        sandbox.py
        tool_gateway.py
        poc_patch.py
  sandbox/
    Dockerfile
```

---

## 2) Write the Docker sandbox image (Docker-per-run)

### File: `sandbox/Dockerfile`
A simple base image that supports cloning + running tests. Start with Python PoC (easiest).

Minimum contents idea (you’ll implement it):
- python
- git
- build essentials (optional)
- create `/workspace`

**Why:** Every run container uses this image; it’s your reproducible sandbox environment.

Build it:
```bash
docker build -t agentic-sandbox:0.1 ./sandbox
```

Sanity test:
```bash
docker run --rm agentic-sandbox:0.1 python --version
docker run --rm agentic-sandbox:0.1 git --version
```

---

## 3) Stand up Postgres (episodic memory) via docker-compose

### File: `deploy/docker-compose.yml`
Run Postgres (and optionally pgAdmin). Use a named volume for persistence.

Start services:
```bash
docker compose -f deploy/docker-compose.yml up -d
```

Confirm Postgres is up:
```bash
docker ps
```

---

## 4) Implement the minimal DB schema (runs + run_state + events + artifacts)

### What to create
Implement these tables first (minimum viable):
- `runs(run_id, repo_url, base_sha, status, created_at, updated_at)`
- `run_state(run_id, version, state_json, updated_at)`
- `events(event_id, run_id, ts, actor, event_type, payload_json)`
- `artifacts(artifact_id, run_id, kind, content_text, sha256, created_at)`

**How to do it (PoC approach)**
- For PoC speed: create tables on orchestrator startup if they don’t exist.
- For “real” project: use Alembic migrations (you can add right after PoC works).

**Success check**
- You can connect and insert a row into `runs`
- You can append an `events` row

---

## 5) Implement the Sandbox Runner (Docker API) — the PoC “engine”

### File: `apps/orchestrator/src/orchestrator/sandbox.py`
Implement these functions:

#### 5.1 `create_run_container(run_id) -> container_id`
- uses image `agentic-sandbox:0.1`
- sets working dir `/workspace`
- sets CPU/mem limits (best effort)
- returns container id

#### 5.2 `exec_in_container(container_id, cmd: list[str], timeout_s: int, cwd="/workspace")`
- **deny** if cmd[0] not in allowlist (see section 6)
- run command as array (no shell)
- capture stdout+stderr
- return `{exit_code, stdout, stderr, duration_ms}`

#### 5.3 `clone_repo(container_id, repo_url, base_sha)`
- run:
  - `git clone <repo_url> repo`
  - `git checkout <base_sha>` inside `/workspace/repo`

**Success check**
- From orchestrator you can:
  - create container
  - clone repo
  - run `pytest -q` (or `python -m pytest -q`)
  - fetch logs

---

## 6) Implement the Tool Gateway (deny-by-default + audit)

### File: `apps/orchestrator/src/orchestrator/tool_gateway.py`

The Tool Gateway must do two things:
1) enforce policy (allowlists)  
2) log every tool call to `events`

#### 6.1 Agent roles (PoC)
Use these roles:
- `PLANNER` (no tools yet)
- `CODER` (`fs.write_file`, `repo.diff`)
- `TESTER` (`sandbox.run`)
- `ORCHESTRATOR` (bootstrap-only)

#### 6.2 Allowlisted sandbox commands (PoC)
Start strict:
- `git`
- `python`
- `pytest`

Everything else denied and logged as `tool.denied`.

#### 6.3 Tool call events
Every tool invocation writes:
- `tool.executed` with:
  - `tool_name`
  - `args_summary` (redacted)
  - `duration_ms`
  - `success`
  - `result_refs` (artifact ids)
- OR `tool.denied` with reason

**Success check**
- Running one `pytest` produces one `tool.executed` event in Postgres.

---

## 7) Implement the Orchestrator CLI (the PoC user interface)

### File: `apps/orchestrator/src/orchestrator/main.py`

Add a CLI command:
```bash
python -m orchestrator.main run --repo <repo_url> --base-sha <sha> --task "<text>"
```

#### 7.1 What it does (exact sequence)
1) `run_id = uuid4()`
2) insert into `runs` with status `NEEDS_BOOTSTRAP`
3) emit event `run.created`
4) create sandbox container (record event)
5) clone repo at SHA (record event)
6) set status `NEEDS_CODE`
7) apply a patch (see section 8)
8) set status `NEEDS_TEST`
9) run tests (`pytest -q`) via tool gateway
10) store logs artifact + event
11) if pass:
    - set status `DONE`
    - emit event `run.done`
12) if fail:
    - set status `FAILED`
    - emit event `run.failed` with artifact refs

**Success check**
- You can run the command and see:
  - `run_id` printed
  - container created
  - tests executed
  - events persisted

---

## 8) Apply a PoC patch (start scripted, then replace with LLM)

### Step 8A — Scripted patch (recommended first)
Create a small deterministic code edit to prove the pipeline loop.

### File: `apps/orchestrator/src/orchestrator/poc_patch.py`
Provide one function:
- `apply_patch(container_id)`

Ways to implement:
- simplest: use `fs.write_file` to modify a known file in the repo
- or run `git apply` with an embedded patch (later)

**Success check**
- `repo.diff()` shows expected change
- tests still run

### Step 8B — LLM patch (after scripted works)
Only after the scripted loop is stable:
- add `providers/base.py` and one provider backend
- have the “Coder” agent produce a patch/diff
- apply it, then run tests

---

## 9) Artifact handling (PoC version)

For PoC simplicity:
- store logs in Postgres `artifacts.content_text`
- compute sha256 of content (dedupe later)

Later (production):
- store artifacts in S3/MinIO and keep refs in Postgres

---

## 10) End-to-end PoC run procedure (what you do every time)

1) Start Postgres:
```bash
docker compose -f deploy/docker-compose.yml up -d
```

2) Build sandbox image (only when Dockerfile changes):
```bash
docker build -t agentic-sandbox:0.1 ./sandbox
```

3) Run orchestrator:
```bash
cd apps/orchestrator
python -m orchestrator.main run --repo <repo_url> --base-sha <sha> --task "PoC task"
```

4) Inspect results:
- printed `run_id`
- query Postgres tables for:
  - `runs.status`
  - `events` timeline
  - artifacts (test logs)

---

## 11) “Definition of Done” for the PoC
PoC is done when:
- A run can be started from CLI
- A new Docker container is created per run
- Repo is cloned at a specific SHA
- A patch is applied
- Tests run deterministically
- All tool calls are logged to Postgres
- The system stops cleanly on pass/fail with artifact refs

---

## Knowledge check (choose now so we don’t build the wrong PoC)
1) PoC target repo language: **Python/pytest** or **Node/npm**?  
2) Do you want sandbox networking in PoC:
   - **A:** no network at all
   - **B:** limited network to fetch dependencies (with strict allowlist later)

Reply with your choices and I’ll tailor the exact allowlists + sandbox Dockerfile base packages accordingly.