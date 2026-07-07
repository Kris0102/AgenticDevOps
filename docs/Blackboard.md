# Blackboard (Run State) Overview — Agentic DevOps Pipeline

This document specifies the **Blackboard**, the pipeline’s **single source of truth** for state management.

## 1) Purpose

The Blackboard is a **deterministic state object** owned and mutated only by the **Orchestrator**.  
It enables:

- **Reproducibility** (restart a run from the latest persisted state)
- **Auditability** (every decision/action is traceable via events + state snapshots)
- **Safe iteration** (bounded loops with explicit stop conditions)
- **Microtask execution** (sequential queue with per-task acceptance criteria and attempt counts)

**Design rule:** Agents never “own state.” Agents propose outputs; the Orchestrator writes state.

---

## 2) Lifecycle

Each run has:

- A stable `run_id`
- A `status` (finite state machine)
- A `microtasks[]` queue
- A pointer to the `current_microtask_id`
- A structured record of **evidence** (test logs, diffs, scan results) used for reflection & gating decisions

The Orchestrator persists:

- `blackboard.state_json` (snapshot)
- `events[]` (append-only log)

---

## 3) Blackboard: Top-level Attributes (Recommended)

Below is a precise attribute list. You can implement it as a **Pydantic model** or TypedDict.

### A) Identity & Source

- `schema_version: str`  
  Version of this blackboard schema (e.g., `"1.0.0"`).
- `run_id: str`  
  Unique id for the run.
- `created_at: str` (ISO 8601)
- `updated_at: str` (ISO 8601)

- `trigger: object`
  - `type: str` (`"github_issue" | "pr_comment" | "alert" | "manual"`)
  - `source_url: str` (e.g., issue URL / PR URL / alert URL)
  - `actor: str` (who triggered it; GitHub username or system)
  - `raw_payload_ref: str` (optional pointer to stored raw webhook JSON in artifacts)

### B) Repository & Execution Context

- `repo: object`
  - `repo_url: str`
  - `default_branch: str` (if known)
  - `base_ref: str` (e.g., `"main"` or PR branch)
  - `base_sha: str` (exact commit SHA used for baseline)
  - `workspace_branch: str` (e.g., `"agent/run-<run_id>"`)
  - `pr_url: str | null` (filled once PR is created)

- `sandbox: object`
  - `sandbox_id: str` (container id or name)
  - `workspace_path: str` (path inside container)
  - `status: str` (`"not_created" | "ready" | "failed" | "destroyed"`)
  - `last_command: object | null`
    - `cmd: str`
    - `started_at: str`
    - `finished_at: str`
    - `exit_code: int`
    - `artifact_ref: str` (link to logs)

### C) Goal, Constraints, Policy

- `objective: object`
  - `title: str` (short)
  - `description: str` (full task)
  - `definition_of_done: list[str]` (human-readable)
  - `non_goals: list[str]` (explicit exclusions)

- `constraints: object`
  - `time_budget_minutes: int | null`
  - `cost_budget_usd: float | null`
  - `max_total_iterations: int` (global loop cap)
  - `max_microtask_attempts: int` (per microtask loop cap)
  - `max_total_tokens: int | null` (global per-run token budget; `null` = no cap. Enforced in
    every agent node — the run FAILs cleanly once `iteration.tokens_used_total` reaches this.
    See ADR 0025)
  - `allowed_languages: list[str]` (optional)
  - `forbidden_paths: list[str]` (e.g., `[".github/workflows/"]` if you want to block changes)
  - `allowed_network: str` (`"deny_all" | "allow_github_only" | "custom"`)
  - `risk_level: str` (`"low" | "medium" | "high" | "unknown"`)

- `policy: object`
  - `require_hitl_for_pr: bool`
  - `require_hitl_for_merge: bool`
  - `require_hitl_for_prod_deploy: bool`
  - `hitl_checkpoints: list[str]` — phase names that pause the run at
    `NEEDS_HITL_APPROVAL` for human approval before proceeding, e.g.
    `["plan", "code", "test"]`. One gate mechanism (`status=NEEDS_HITL_APPROVAL` +
    `hitl.pending_approvals[]`), configured per-run by risk level — see
    `ARCHITECTURE_preview.md` §6.2 "General HITL pattern". Empty list = no mid-run gates
    (only the `require_hitl_for_*` gates at PR/merge/deploy apply).
  - `security_scans_required: list[str]` (e.g., `["gitleaks", "semgrep", "osv"]`)
  - `ci_required: bool`
  - `protected_branches: list[str]`
  - `security_audit_enabled: bool` — gates the optional Security/Policy Auditor agent
    (see `ARCHITECTURE_preview.md` §5.6). Default `false`: Phase D routes
    `READY_FOR_PR` directly, skipping `NEEDS_SECURITY_AUDIT`. `security_scans_required`
    still applies via the Tester's deterministic checks regardless of this flag — this
    only controls whether a dedicated LLM-narrated audit *agent* runs.

### D) State Machine

- `status: str`  
  One of (example):  
  `"NEEDS_BOOTSTRAP" | "NEEDS_PLAN" | "NEEDS_CODE" | "NEEDS_TEST" | "REFLECT" | "NEEDS_FINAL_TEST" | "NEEDS_SECURITY_AUDIT" | "READY_FOR_PR" | "WAITING_FOR_CI" | "NEEDS_HITL_APPROVAL" | "DONE" | "FAILED"`

- `phase: str`  
  Higher-level grouping, optional: `"bootstrap" | "planning" | "execution" | "finalization" | "pr" | "deploy"`

- `iteration: object`
  - `global_step: int` (monotonic step counter)
  - `global_iterations_used: int`
  - `tokens_used_total: int` (running total of tokens consumed by every agent's LLM calls this
    run, accumulated after each node invocation; checked against `constraints.max_total_tokens`.
    See ADR 0025)
  - `current_loop_reason: str | null` (e.g., `"lint_failed"`, `"test_failed"`, `"security_failed"`)

### E) Planning Outputs

- `plan: object | null`
  - `summary: str`
  - `architecture_notes: str`
  - `assumptions: list[str]`
  - `risks: list[str]`
  - `open_questions: list[str]`
  - `review_feedback: list[object]`
    - `reviewer: str`
    - `timestamp: str`
    - `decision: str` (`"approve" | "reject"`)
    - `notes: str`

### F) Microtask Queue (Core)

- `microtasks: list[object]`  
  Each microtask MUST be self-contained and testable.
  - `microtask_id: str`
  - `title: str`
  - `description: str`
  - `acceptance_criteria: list[str]`
  - `status: str` (`"PENDING" | "IN_PROGRESS" | "DONE" | "BLOCKED" | "FAILED"`)
  - `priority: int` (lower = sooner) OR `idx: int` (execution order)
  - `dependencies: list[str]` (microtask_ids)
  - `attempts: int`
  - `max_attempts: int` (can override default)
  - `files_touched: list[str]` (filled as execution happens)
  - `test_strategy: str` (e.g., “unit tests for X; integration for Y”)
  - `notes: list[str]` (running log of decisions)

- `current_microtask_id: str | null`  
  Pointer to which microtask is currently being executed.

### G) Evidence & Artifacts (Grounding)

This section is _critical_ for grounded reflection and reliable debugging.

- `evidence: object`
  - `latest_diff: object | null`
    - `base_sha: str`
    - `diff_artifact_ref: str` (stored patch/diff)
    - `summary: str` (files changed summary)
  - `checks: object`
    - `lint: object | null` (`status`, `exit_code`, `artifact_ref`)
    - `typecheck: object | null`
    - `unit_tests: object | null`
    - `integration_tests: object | null`
    - `build: object | null`
  - `ci: object | null`
    - `status: str` (`"unknown" | "pending" | "failed" | "passed"`)
    - `provider: str` (`"github_actions"` etc.)
    - `run_url: str | null`
    - `failed_jobs: list[object]`
      - `job_name: str`
      - `logs_artifact_ref: str`

- `artifacts_index: list[object]`  
  Optional quick index; canonical storage still in DB.
  - `artifact_ref: str`
  - `kind: str` (`"log" | "diff" | "junit" | "coverage" | "scan_report" | "plan"`)
  - `created_at: str`
  - `summary: str`

### H) Reflection & Lessons Learned

- `reflections: list[object]`
  - `timestamp: str`
  - `microtask_id: str | null`
  - `failure_type: str` (`"lint" | "unit_test" | "integration_test" | "ci" | "security" | "runtime"`)
  - `root_cause: str`
  - `fix_instructions: str`
  - `linked_artifacts: list[str]` (artifact refs)
  - `should_store_in_semantic_memory: bool` (flag; Context Engine/Orchestrator decides storage)

### I) Security & Policy Gate Results

- `security: object`
  - `scan_results: list[object]`
    - `scanner: str` (gitleaks/semgrep/osv/...)
    - `status: str` (`"pass" | "fail" | "warning"`)
    - `severity: str` (`"low" | "medium" | "high" | "critical" | "unknown"`)
    - `report_artifact_ref: str`
  - `final_decision: str | null` (`"pass" | "fail"`)
  - `notes: str | null`

### J) Human-in-the-Loop (Approvals)

- `hitl: object`
  - `required: bool`
  - `pending_approvals: list[object]`
    - `type: str` (`"plan" | "code" | "test" | "pr" | "merge" | "prod_deploy" | "security_exception"`)
      — `"plan"`/`"code"`/`"test"` are mid-run checkpoints driven by
      `policy.hitl_checkpoints`; the rest are the fixed end-of-pipeline gates driven by
      `policy.require_hitl_for_*`.
    - `requested_at: str`
    - `status: str` (`"pending" | "approved" | "rejected"`)
    - `reviewer: str | null`
    - `notes: str | null`

### K) Termination

- `result: object | null`
  - `outcome: str` (`"success" | "failed" | "cancelled"`)
  - `reason: str`
  - `summary: str`
  - `final_artifacts: list[str]`
  - `ended_at: str`

---

## 4) Minimal Blackboard (MVP) vs Full Blackboard

### MVP (start building first)

If you want the smallest viable state machine, start with:

- `run_id`, `repo.repo_url`, `repo.base_sha`, `status`
- `objective.title`, `objective.description`
- `microtasks[]`, `current_microtask_id`
- `constraints.max_microtask_attempts`, `iteration.global_step`
- `evidence.checks.unit_tests`, `evidence.latest_diff`
- `reflections[]`
- `hitl.required`, `hitl.pending_approvals[]`

### Full (scale/production)

Add:

- CI job granularity (`evidence.ci.failed_jobs`)
- Security scan reports (`security.scan_results`)
- Artifact indexing
- Stronger sandbox runtime metadata and tool provenance

---

## 5) Hard Rules (to keep it robust)

1. **Only Orchestrator writes the Blackboard.**
2. Every tool execution result must be captured as:
   - event in `events`
   - optional summarized snapshot under `evidence`
3. Every loop must have a stop condition:
   - `max_plan_iterations`
   - `max_microtask_attempts`
   - `max_total_iterations`
4. Every microtask must contain **acceptance criteria**.
5. Retrieval must be namespaced:
   - baseline (`repo@sha`)
   - workspace (`run_id`)

---

## 6) Events (Append-only Audit Log) — What it needs & where it is stored

### 6.1 Why Events exist (and why they are not the Blackboard)

The Blackboard is the **current snapshot**: “what the system believes right now.”

Events are the **append-only history**: “what happened, in what order, with what evidence.”

You need both:

- Blackboard supports fast decisions and resumption.
- Events support auditing, debugging, replay, and analytics.

### 6.2 Where Events are stored (recommended)

**Canonical storage location:** an append-only `events` table in **Episodic Memory (Postgres)**.

**Why not store all events inside `blackboard.state_json`?**

- The Blackboard JSON would grow without bound and become slow to read/write.
- You want fast state transitions; events can be queried separately by time/seq.

### 6.3 How Events connect to the Blackboard

The Blackboard should include:

1. a **pointer/cursor** into the canonical event stream, and
2. optionally a small **tail buffer** of the most recent events for quick debugging.

**Recommended Blackboard field:**

- `events: object`
  - `store: str` (e.g., `"postgres"`)
  - `stream_id: str` (usually equals `run_id`)
  - `last_event_id: str | null` (cursor for resume)
  - `last_seq: int` (cursor for strict ordering; redundant but useful)
  - `tail: list[event]` (optional, last N events only; e.g., 20–100)

### 6.4 Event schema (required fields)

Each Event must be machine-readable and ordered.

- `event_id: str` (unique, e.g., `"evt_000042"`)
- `run_id: str`
- `seq: int` (monotonic per run; strict ordering)
- `timestamp: str` (ISO 8601)
- `actor: str` (e.g., `"orchestrator"`, `"coder_agent"`, `"sandbox"`, `"gitops_service"`, `"human"`)
- `type: str` (event type enum; see Section 6.7)
- `severity: str` (`"debug" | "info" | "warning" | "error" | "critical"`)
- `message: str` (human readable)
- `payload: object` (structured details; required fields depend on `type`)
- `artifact_refs: list[str]` (links to logs/diffs/reports used as evidence)

### 6.5 Minimal Postgres table (conceptual)

- `events(event_id TEXT PRIMARY KEY, run_id TEXT, seq INT, timestamp TIMESTAMPTZ, actor TEXT, type TEXT, severity TEXT, message TEXT, payload JSONB, artifact_refs JSONB)`

**Indexes (recommended):**

- `(run_id, seq)` unique
- `(run_id, timestamp)`
- `(type)`

### 6.6 Event categories

To keep analytics simple and to avoid “one-off” types, event types are grouped:

- **run.\***: lifecycle of the run
- **sandbox.\***: sandbox provisioning and command execution
- **repo.\*** / **gitops.\***: cloning, branching, commits, PRs
- **context.\***: baseline ingestion + incremental sync
- **plan.\***: planning loop outputs
- **microtask.\***: queue execution and status
- **tool.\***: tool invocations (including allowlist denial)
- **check.\*** / **test.\***: lint/typecheck/tests/build results
- **reflection.\***: grounded root-cause + fix instructions
- **security.\***: scans + decisions
- **hitl.\***: approvals requested/decided
- **ci.\***: CI status webhooks
- **deploy.\***: staging/prod deploy actions

---

### 6.7 Event Type Enum + Required Payload Fields (No ambiguity)

Below is a recommended event type registry.
**Rule:** if an event type is emitted, its payload MUST contain the listed fields.

> Tip: implement payload validation by mapping `type -> required payload keys` in code.

### 6.7.1 run.\*

- **`run.created`**
  - payload required: `repo_url`, `base_ref`, `base_sha`, `trigger_type`, `trigger_url`
- **`run.status_changed`**
  - payload required: `from_status`, `to_status`, `reason`
- **`run.failed`**
  - payload required: `reason`, `failed_phase`, `last_known_status`
- **`run.completed`**
  - payload required: `outcome`, `summary`, `pr_url`

### 6.7.2 sandbox.\*

- **`sandbox.created`**
  - payload required: `sandbox_id`, `workspace_path`
- **`sandbox.destroyed`**
  - payload required: `sandbox_id`
- **`sandbox.command_started`**
  - payload required: `sandbox_id`, `cmd`, `cwd`, `timeout_seconds`
- **`sandbox.command_finished`**
  - payload required: `sandbox_id`, `cmd`, `exit_code`, `duration_ms`, `stdout_ref`, `stderr_ref`

### 6.7.3 repo._ / gitops._

- **`repo.cloned`**
  - payload required: `repo_url`, `base_sha`, `sandbox_id`
- **`gitops.branch_created`**
  - payload required: `branch`, `base_sha`
- **`gitops.commit_created`**
  - payload required: `branch`, `commit_sha`, `message`, `files_changed`
- **`gitops.pr_created`**
  - payload required: `pr_url`, `branch`, `base_sha`, `title`
- **`gitops.pr_updated`**
  - payload required: `pr_url`, `branch`, `commit_sha`
- **`gitops.merge_requested`**
  - payload required: `pr_url`, `target_branch`
- **`gitops.merged`**
  - payload required: `pr_url`, `merge_sha`, `target_branch`

### 6.7.4 context.\*

- **`context.baseline_ingest_started`**
  - payload required: `repo_url`, `base_sha`, `namespace`
- **`context.baseline_ingested`**
  - payload required: `repo_url`, `base_sha`, `namespace`, `files_indexed`, `chunks_indexed`, `duration_ms`
- **`context.workspace_sync_started`**
  - payload required: `run_id`, `namespace`, `changed_files`
- **`context.workspace_synced`**
  - payload required: `run_id`, `namespace`, `changed_files`, `chunks_upserted`, `duration_ms`

### 6.7.5 plan.\*

- **`plan.drafted`**
  - payload required: `plan_version`, `microtasks_count`
- **`plan.reviewed`**
  - payload required: `plan_version`, `decision`, `notes`
- **`plan.approved`**
  - payload required: `plan_version`, `microtasks_count`

### 6.7.6 microtask.\*

- **`microtask.started`**
  - payload required: `microtask_id`, `title`, `attempt`
- **`microtask.updated`**
  - payload required: `microtask_id`, `status`, `notes`
- **`microtask.completed`**
  - payload required: `microtask_id`, `attempts_used`, `files_touched`
- **`microtask.failed`**
  - payload required: `microtask_id`, `attempts_used`, `reason`

### 6.7.7 tool.\*

- **`tool.executed`**
  - payload required: `tool_name`, `args`, `duration_ms`, `success`
- **`tool.denied`**
  - payload required: `tool_name`, `args`, `policy_rule`, `reason`

### 6.7.8 check._ / test._

- **`check.lint_completed`**
  - payload required: `status`, `exit_code`, `command`, `log_ref`
- **`check.typecheck_completed`**
  - payload required: `status`, `exit_code`, `command`, `log_ref`
- **`test.unit_completed`**
  - payload required: `status`, `exit_code`, `command`, `log_ref`
- **`test.integration_completed`**
  - payload required: `status`, `exit_code`, `command`, `log_ref`
- **`check.build_completed`**
  - payload required: `status`, `exit_code`, `command`, `log_ref`

### 6.7.9 reflection.\*

- **`reflection.created`**
  - payload required: `microtask_id`, `failure_type`, `root_cause_summary`, `instructions_ref`

### 6.7.10 security.\*

- **`security.scan_completed`**
  - payload required: `scanner`, `status`, `severity`, `report_ref`
- **`security.decision`**
  - payload required: `decision`, `notes`

### 6.7.11 hitl.\*

- **`hitl.requested`**
  - payload required: `approval_id`, `approval_type`, `reason`, `risk_level`, `evidence_refs`
- **`hitl.decided`**
  - payload required: `approval_id`, `decision`, `reviewer`, `notes`

### 6.7.12 ci.\*

- **`ci.status_changed`**
  - payload required: `provider`, `status`, `run_url`, `commit_sha`
- **`ci.failed_jobs`**
  - payload required: `run_url`, `jobs` _(jobs is an array of `{job_name, logs_ref}`)_

### 6.7.13 deploy.\*

- **`deploy.started`**
  - payload required: `environment`, `version_sha`, `strategy`
- **`deploy.finished`**
  - payload required: `environment`, `version_sha`, `status`, `duration_ms`, `logs_ref`

### 6.7.14 llm.\*

- **`llm.usage`** (ADR 0025) — emitted once per agent-node invocation (Planner, Reviewer,
  Coder, Tester, Reflector), right after that node's agent returns.
  - payload required: `agent`, `model`, `input_tokens`, `output_tokens`, `total_tokens`

---

## 7) Full updated Blackboard JSON example (includes events pointer + tail)

```json
{
  "schema_version": "1.0.0",
  "run_id": "run_2026_05_21_001",
  "created_at": "2026-05-21T14:12:03Z",
  "updated_at": "2026-05-21T14:25:41Z",
  "trigger": {
    "type": "github_issue",
    "source_url": "https://github.com/acme/payments/issues/123",
    "actor": "Kris0102",
    "raw_payload_ref": "artifact://events/raw_webhook/run_2026_05_21_001.json"
  },
  "repo": {
    "repo_url": "https://github.com/acme/payments",
    "default_branch": "main",
    "base_ref": "main",
    "base_sha": "4dd2a9c2f6c1b0d8d2f0ad4f1a9ce3b987654321",
    "workspace_branch": "agent/run-run_2026_05_21_001",
    "pr_url": null
  },
  "sandbox": {
    "sandbox_id": "sandbox_run_2026_05_21_001",
    "workspace_path": "/workspace/repo",
    "status": "ready",
    "last_command": {
      "cmd": "pytest -q",
      "started_at": "2026-05-21T14:24:10Z",
      "finished_at": "2026-05-21T14:24:55Z",
      "exit_code": 1,
      "artifact_ref": "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt"
    }
  },
  "objective": {
    "title": "Add password reset endpoint",
    "description": "Implement password reset in the existing auth service using token-based flow and add unit tests.",
    "definition_of_done": [
      "New endpoint exists and is documented",
      "Unit tests cover success + failure cases",
      "CI passes",
      "Security scan passes"
    ],
    "non_goals": [
      "Do not redesign the entire auth subsystem",
      "Do not change existing login endpoints unless required"
    ]
  },
  "constraints": {
    "time_budget_minutes": 90,
    "cost_budget_usd": 10.0,
    "max_total_iterations": 200,
    "max_microtask_attempts": 5,
    "max_total_tokens": null,
    "allowed_languages": ["python"],
    "forbidden_paths": [".github/workflows/"],
    "allowed_network": "deny_all",
    "risk_level": "medium"
  },
  "policy": {
    "require_hitl_for_pr": true,
    "require_hitl_for_merge": true,
    "require_hitl_for_prod_deploy": true,
    "security_scans_required": ["gitleaks", "semgrep", "osv"],
    "ci_required": true,
    "protected_branches": ["main"]
  },
  "status": "REFLECT",
  "phase": "execution",
  "iteration": {
    "global_step": 42,
    "global_iterations_used": 42,
    "tokens_used_total": 184320,
    "current_loop_reason": "unit_test_failed"
  },
  "plan": {
    "summary": "Implement token-based reset flow with DB-backed tokens and email stub.",
    "architecture_notes": "Reuse existing token utilities; keep changes inside auth module; add migration if needed.",
    "assumptions": ["Existing email provider is mocked in tests."],
    "risks": ["DB migration may be required", "Token expiry edge cases"],
    "open_questions": [
      "Where should reset tokens be stored: users table vs separate table?"
    ],
    "review_feedback": [
      {
        "reviewer": "plan_reviewer_agent",
        "timestamp": "2026-05-21T14:15:10Z",
        "decision": "approve",
        "notes": "Plan is complete; ensure tests include expired token case."
      }
    ]
  },
  "microtasks": [
    {
      "microtask_id": "mt1",
      "title": "Add reset token persistence",
      "description": "Introduce table/model for reset tokens with expiry and user binding.",
      "acceptance_criteria": [
        "Schema or model exists",
        "Tokens are hashed at rest",
        "Unit tests cover create + verify + expiry"
      ],
      "status": "DONE",
      "idx": 1,
      "dependencies": [],
      "attempts": 2,
      "max_attempts": 5,
      "files_touched": [
        "auth/models.py",
        "auth/migrations/2026_05_21_add_reset_tokens.sql"
      ],
      "test_strategy": "Unit tests for token creation and expiry checks",
      "notes": ["Chose separate table to avoid user table bloat."]
    },
    {
      "microtask_id": "mt2",
      "title": "Implement password reset endpoint",
      "description": "Add POST /auth/reset-password with token validation and password update.",
      "acceptance_criteria": [
        "Endpoint validates token",
        "Password is updated using existing hashing utilities",
        "Returns correct status codes for invalid/expired tokens"
      ],
      "status": "IN_PROGRESS",
      "idx": 2,
      "dependencies": ["mt1"],
      "attempts": 3,
      "max_attempts": 5,
      "files_touched": ["auth/routes.py", "auth/service.py"],
      "test_strategy": "Unit tests for route handler and service logic",
      "notes": ["Need to align error messages with existing API conventions."]
    }
  ],
  "current_microtask_id": "mt2",
  "evidence": {
    "latest_diff": {
      "base_sha": "4dd2a9c2f6c1b0d8d2f0ad4f1a9ce3b987654321",
      "diff_artifact_ref": "artifact://diffs/run_2026_05_21_001.patch",
      "summary": "Modified auth/routes.py and auth/service.py; added tests."
    },
    "checks": {
      "lint": {
        "status": "pass",
        "exit_code": 0,
        "artifact_ref": "artifact://logs/flake8/run_2026_05_21_001.txt"
      },
      "typecheck": null,
      "unit_tests": {
        "status": "fail",
        "exit_code": 1,
        "artifact_ref": "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt"
      },
      "integration_tests": null,
      "build": null
    },
    "ci": {
      "status": "unknown",
      "provider": "github_actions",
      "run_url": null,
      "failed_jobs": []
    }
  },
  "artifacts_index": [
    {
      "artifact_ref": "artifact://diffs/run_2026_05_21_001.patch",
      "kind": "diff",
      "created_at": "2026-05-21T14:23:10Z",
      "summary": "Patch of workspace changes vs base_sha."
    },
    {
      "artifact_ref": "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt",
      "kind": "log",
      "created_at": "2026-05-21T14:24:55Z",
      "summary": "Pytest output showing failing test: expired token case."
    }
  ],
  "reflections": [
    {
      "timestamp": "2026-05-21T14:25:20Z",
      "microtask_id": "mt2",
      "failure_type": "unit_test",
      "root_cause": "Token expiry comparison uses naive datetime; timezone-aware timestamps cause mismatch.",
      "fix_instructions": "Use timezone-aware UTC timestamps and normalize before comparing. Update tests accordingly.",
      "linked_artifacts": [
        "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt"
      ],
      "should_store_in_semantic_memory": true
    }
  ],
  "security": {
    "scan_results": [
      {
        "scanner": "gitleaks",
        "status": "pass",
        "severity": "low",
        "report_artifact_ref": "artifact://scans/gitleaks/run_2026_05_21_001.json"
      },
      {
        "scanner": "semgrep",
        "status": "warning",
        "severity": "medium",
        "report_artifact_ref": "artifact://scans/semgrep/run_2026_05_21_001.json"
      }
    ],
    "final_decision": null,
    "notes": "Semgrep warns about missing rate limiting on reset endpoint; consider follow-up task."
  },
  "hitl": {
    "required": true,
    "pending_approvals": [
      {
        "type": "pr",
        "requested_at": "2026-05-21T15:10:00Z",
        "status": "pending",
        "reviewer": null,
        "notes": "PR will be created after final tests pass."
      }
    ]
  },
  "events": {
    "store": "postgres",
    "stream_id": "run_2026_05_21_001",
    "last_event_id": "evt_000042",
    "last_seq": 42,
    "tail": [
      {
        "event_id": "evt_000041",
        "run_id": "run_2026_05_21_001",
        "seq": 41,
        "timestamp": "2026-05-21T14:24:55Z",
        "actor": "tester_agent",
        "type": "test.unit_completed",
        "severity": "error",
        "message": "Unit tests failed for microtask mt2",
        "payload": {
          "status": "fail",
          "exit_code": 1,
          "command": "pytest -q",
          "log_ref": "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt"
        },
        "artifact_refs": [
          "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt"
        ]
      },
      {
        "event_id": "evt_000042",
        "run_id": "run_2026_05_21_001",
        "seq": 42,
        "timestamp": "2026-05-21T14:25:20Z",
        "actor": "reflector_agent",
        "type": "reflection.created",
        "severity": "info",
        "message": "Root cause identified; fix instructions generated",
        "payload": {
          "microtask_id": "mt2",
          "failure_type": "unit_test",
          "root_cause_summary": "Timezone-aware timestamp mismatch",
          "instructions_ref": "artifact://reflections/run_2026_05_21_001_mt2.txt"
        },
        "artifact_refs": [
          "artifact://logs/pytest/run_2026_05_21_001_attempt3.txt",
          "artifact://reflections/run_2026_05_21_001_mt2.txt"
        ]
      }
    ]
  },
  "result": null
}
````
