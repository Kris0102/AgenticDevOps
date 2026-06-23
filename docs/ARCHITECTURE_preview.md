# Agentic DevOps Pipeline Architecture (Version 2 — Precise Spec)

This document is an **implementation-grade specification** for an **Agentic DevOps Pipeline**: a multi-agent system that can plan, implement, test, and propose changes to an **existing** software repository using an auditable, safe, and reproducible workflow.

The goal is **zero ambiguity**. This document defines:

- Every service, its responsibilities, and its boundaries
- What data is stored where (and who is allowed to write it)
- The exact end-to-end workflow, including loops and stop conditions
- The state machine that controls execution and guarantees reproducibility

---

## 1) Glossary

- **Run**: A single end-to-end execution instance for one request (feature/bugfix/security task). Identified by `run_id`.
- **Microtask**: The smallest unit of work executed sequentially. Each microtask has explicit **acceptance criteria**.
- **Blackboard / Run State**: The authoritative JSON state machine for a run. Stored in Episodic Memory and **owned by the Orchestrator**.
- **Sandbox**: A containerized workspace where the repository is cloned, edited, built, and tested.
- **Baseline**: The repository state at the initial `base_sha` for a run.
- **Workspace (Dirty State)**: The current modified repository state inside the Sandbox.
- **Semantic Memory**: A vector index used for retrieval of relevant code and reflections. **Not** a filesystem and **not** a code source of truth.

---

## 2) Architectural Principles (Non-negotiables)

### 2.1 Control Plane vs Data Plane

**Control Plane (decision-making)**

- Orchestrator
- Agents (Planner, Plan Reviewer, Coder, Tester, Reflector, Auditor, Deployer)

**Data Plane (execution + storage)**

- Sandbox (Docker)
- Episodic DB (Postgres)
- Semantic DB (vector store, e.g., pgvector)
- Observability (metrics/logs/traces)

**Rationale**

- If an agent fails → the run is recoverable from Episodic Memory + Sandbox.
- If the Sandbox fails → it can be recreated and rehydrated using GitOps + state.

### 2.2 Deterministic vs Probabilistic Responsibilities

**Deterministic operations (owned by infrastructure/services)**

- Clone repository, create branches, commit, open PR
- Execute lint/typecheck/tests/build commands
- Parse code, chunk, embed, and upsert to vector DB
- Enforce timeouts/resource limits/policy guardrails

**Probabilistic reasoning (owned by LLM agents)**

- Planning and decomposition into microtasks
- Code authoring decisions
- Log interpretation and repair proposals
- Security reasoning (but final enforcement is policy + scanners)

### 2.3 Sandbox is the Source of Truth for Code

- The **only authoritative** repository contents during a run live in the **Sandbox filesystem**.
- Semantic Memory is for **search and retrieval only**, not for reconstruction.
- The Orchestrator must never treat Semantic Memory as “the repo”.

---

## 3) Memory Model (Precise)

### 3.1 Working Memory (Per Agent Call)

- **Location:** the LLM context window.
- **Persistence:** none.
- **Contains:** the agent prompt, retrieved snippets, current microtask, and relevant run state.

### 3.2 Episodic Memory (System of Record)

- **Storage:** Postgres (or equivalent relational DB).
- **Purpose:**
  - run state machine persistence (resume/replay)
  - audit logs of agent decisions and tool results
  - microtask queue and progress tracking
  - artifact registry (logs, test reports, diffs)

**Minimum required data model (conceptual tables)**

- `runs(run_id, repo_url, base_sha, status, created_at, updated_at, risk_level, pr_url, ...)`
- `run_state(run_id, state_json, version, updated_at)`
- `events(event_id, run_id, ts, actor, event_type, payload_json)` (append-only)
- `microtasks(microtask_id, run_id, idx, title, description, acceptance_criteria_json, status, attempts, ...)`
- `artifacts(artifact_id, run_id, kind, path_or_url, sha256, metadata_json, created_at)`

**Write rules**

- Only the **Orchestrator** may mutate the `run_state`.
- Agents write data only via Orchestrator events (no direct DB writes).

### 3.3 Semantic Memory (Vector Retrieval)

- **Storage:** pgvector in Postgres (recommended early) or dedicated vector DB (scale).
- **Purpose:** retrieval of code chunks, docs, and reflections by similarity + metadata filters.

**Namespaces (required)**

- `baseline`: embeddings for repository at `base_sha`.
- `workspace`: embeddings reflecting current Sandbox edits for this specific `run_id`.

**Minimum required fields per record**

- `namespace` (`baseline` | `workspace`)
- `run_id` (nullable; required for `workspace`)
- `repo_url`
- `git_sha` (required for `baseline`)
- `filepath`
- `symbol` (function/class/module identifier when possible)
- `chunk_type` (`function` | `class` | `file` | `doc` | `reflection`)
- `chunk_text`
- `embedding`
- `metadata_json` (language, imports, dependencies, test references, etc.)

**Write rules**

- Context Engine is the **only writer** to Semantic Memory.
- Agents query Semantic Memory only through tools controlled by the Orchestrator/tool gateway.

---

## 4) Services (Responsibilities & Boundaries)

### 4.1 Orchestrator Service (Control Plane)

**Recommended tech:** Python + LangGraph

**Responsibilities**

- Own and mutate the **Run State machine** (Blackboard).
- Decide which agent/tool runs next.
- Enforce stop conditions (max attempts, max loops, escalation).
- Persist:
  - every state transition
  - every tool invocation and result
  - every agent output
- Emit observability events for tracing and dashboarding.

**Inputs**

- Task requests (GitOps webhooks: issue opened, PR comment, etc.)
- CI status updates (GitOps relays CI events)
- HITL approvals/rejections
- Optional monitoring alerts (future)

**Outputs**

- Agent invocation requests
- Sandbox execution requests (run `pytest`, `flake8`, `npm test`, etc.)
- GitOps requests (clone, branch, commit, PR create/update)

---

### 4.2 Context Engine Service (Data Plane — Ingestion & Sync)

**Recommended tech:** Python + Tree-sitter/LlamaIndex + filesystem watcher + embeddings + pgvector

**Responsibilities**

1. **Baseline ingestion** (run start):
   - parse repo (from Sandbox) into AST-aligned chunks
   - embed + upsert into `namespace=baseline` at `git_sha=base_sha`

2. **Workspace sync** (during run):
   - detect changes inside the Sandbox workspace (watchdog/inotify or `git diff` polling)
   - re-chunk only changed files/functions/classes
   - embed + upsert into `namespace=workspace, run_id=<run_id>`

**Hard requirements**

- Must be restart-safe and idempotent.
- Must apply secret/file exclusion policies (`.env`, keys, tokens, etc.)
- Must attach metadata sufficient for reliable retrieval filtering (namespace/run_id/file).

---

### 4.3 Sandbox Service (Data Plane — Execution)

**Recommended tech:** Docker (container per run)

**Responsibilities**

- Host repo working directory.
- Run deterministic commands with:
  - timeouts
  - CPU/memory limits
  - limited network egress
- Produce artifacts (logs, junit xml, coverage reports).

**Hard requirements**

- No production credentials stored in the container.
- Commands run through a tool gateway that enforces allowlists.

---

### 4.4 GitOps Service (Bridge to GitHub)

**Recommended tech:** GitHub App (preferred) or PAT + `git` CLI + GitHub API

**Responsibilities**

- Receive GitHub webhooks and forward normalized events to Orchestrator.
- Clone repo at specific SHA into Sandbox.
- Create branch: `agent/run-<run_id>`.
- Commit changes from Sandbox.
- Open/update PR with:
  - summary of microtasks
  - test results and artifacts
  - audit results
  - risk notes
- Relay CI status back to Orchestrator.

**Hard requirements**

- Least privilege scopes.
- Never merge protected branches without policy + HITL (unless explicitly configured).

---

### 4.5 Telemetry & Dashboard Service (Observability)

**Recommended tech**

- Traces: LangSmith (agent/tool call traces)
- Metrics: Prometheus
- Dashboards: Grafana
- Optional logs: Loki/ELK

**Responsibilities**

- Display per-run:
  - current state
  - microtask progress
  - tool execution durations
  - test pass/fail trends
  - indexing latency and error counts
  - token usage/cost

---

### 4.6 Human-in-the-Loop (HITL)

**Form:** GitHub PR review, UI, or chatops

**Responsibilities**

- Approve or reject:
  - merge to protected branches
  - production deployment
  - policy/security exceptions

---

## 5) Agents (Inputs & Outputs)

### 5.1 Planner (Actor)

**Input:** task, constraints, retrieved repo context  
**Output:** draft plan + ordered microtasks (each with acceptance criteria)

### 5.2 Plan Reviewer (Critic)

**Input:** draft plan + microtasks

**Output:** approve OR critique focused on **plan *quality and detail*** — the
judgment calls a deterministic check cannot make:
- Is each microtask **detailed enough** for the Coder to act without guessing
  (concrete file paths, approach, types/contracts spelled out — not vague intent)?
- Is the **ordering correct** (no microtask depends on something a later one creates)?
- Are there **missing steps or edge cases** the plan doesn't cover?
- Are the **acceptance criteria observable** (testable behaviour, not "file exists")?

**Explicitly NOT this agent's job:** structural/schema validation (every microtask has
`acceptance_criteria`, `target_files` non-empty and ≤3, `description` length, etc.).
That is **deterministic** and owned by the Planner's own output validator — re-checking
it here would be redundant LLM judgment over something already grounded (§2.2). The
Reviewer should be able to *assume* structural soundness and spend its judgment on the
genuinely-probabilistic question: "would a competent engineer find this plan clear and
complete enough to start coding from?"

### 5.3 Coder (Actor)

**Input:** one microtask + retrieved context + constraints  
**Output:** edits files in Sandbox + short implementation notes (what/why)

### 5.4 Tester (Executor/Designer)

**Input:** current workspace + microtask acceptance criteria  
**Output:**

- deterministic checks: format/lint/typecheck
- tests written/updated
- tests executed
- artifacts + logs

### 5.5 Reflector / Grounded Code Reviewer

**Input:** failing logs + code diff + relevant snippets  
**Output:** root-cause analysis + minimal fix instructions

### 5.6 Security/Policy Auditor (OPTIONAL — gated by `policy.security_audit_enabled`)

**Status:** not part of the v1 required pipeline. Enabled per-run via policy, not
hardwired into the state machine — a run with `security_audit_enabled=false` routes
`READY_FOR_PR` directly from Phase D step 15 (final test), skipping `NEEDS_SECURITY_AUDIT`
entirely. Build this when there's a concrete need for it (a target repo/org that
requires it), not speculatively — same reasoning as deferring the Context Engine (§9).

**Input:** final diff + scanner outputs

**Output:** pass/fail + mitigation plan + risk summary. The pass/fail decision itself
must remain **deterministic** (scanner exit codes vs policy thresholds — §2.2): the LLM
narrates *why*, it does not *decide*. An LLM-only security gate would be exactly the
"LLM-only review" the grounded-verification principle forbids — and getting a security
gate wrong is costlier than getting a test-pass wrong.

### 5.7 Deployer (Optional)

**Input:** approved merge/deploy request  
**Output:** deployment + smoke test results + rollback plan (if failure)

---

## 6) End-to-End Workflow (Deterministic State Machine)

### 6.1 Required Status Values (example set)

- `NEEDS_BOOTSTRAP`
- `NEEDS_PLAN`
- `NEEDS_CODE`
- `NEEDS_TEST`
- `REFLECT`
- `NEEDS_FINAL_TEST`
- `NEEDS_SECURITY_AUDIT`
- `READY_FOR_PR`
- `WAITING_FOR_CI`
- `NEEDS_HITL_APPROVAL`
- `DONE`
- `FAILED`

### 6.2 Workflow Steps

#### Phase A — Trigger & Bootstrap

1. GitOps receives webhook; sends `task_request(repo_url, base_ref/base_sha, task_text)` to Orchestrator.
2. Orchestrator creates `run_id`, stores metadata, sets `status=NEEDS_BOOTSTRAP`.
3. GitOps clones repo at `base_sha` into the Sandbox.
4. Context Engine ingests baseline into Semantic Memory (`namespace=baseline`).
5. Orchestrator sets `status=NEEDS_PLAN`.

#### Phase B — Planning (Actor–Critic loop)

6. Orchestrator invokes Planner → writes draft microtasks to Episodic Memory.
7. Orchestrator invokes Plan Reviewer (judges **detail and completeness**, not
   structure — see §5.2):
   - If rejected: record critique event; go back to step 6.
   - If approved: freeze microtask list.
8. **HITL checkpoint** (if `"plan" in policy.hitl_checkpoints`): set
   `status=NEEDS_HITL_APPROVAL`, pause for human approval of the *frozen plan* before
   any code is written. This is the cheapest possible veto point — rejecting here costs
   one planning pass; rejecting after Phase C has run costs the entire execution loop.
   If approved (or not configured): set `status=NEEDS_CODE`.

**Stop condition (required):**

- Max plan iterations (e.g., 3). If exceeded → require HITL or fail the run.

#### Phase C — Microtask Execution loop (strictly sequential)

Microtasks execute **one at a time, in `idx` order** — never in parallel, never
reordered at runtime. `Microtask.dependencies` is **reserved for a possible future
parallel scheduler and is not consulted by this loop**; until such a scheduler is
designed (a substantially larger architectural commitment — separate sandboxes per
parallel branch, workspace-merge strategy, parallel verification), treat the field as
informational only. This keeps the audit trail linear and trivial to reason about: one
microtask, one diff, one verification cycle, in order.

For each microtask in order:

8. Orchestrator sets `current_microtask` and sets `status=NEEDS_CODE`.
9. Invoke Coder to implement only that microtask in Sandbox.
10. **HITL checkpoint** (if `"code" in policy.hitl_checkpoints`): pause at
    `NEEDS_HITL_APPROVAL` for human review of the diff before testing burns more compute
    on a change a human would reject outright.
11. Context Engine syncs changed files into Semantic Memory (`namespace=workspace, run_id=<run_id>`).
12. Orchestrator sets `status=NEEDS_TEST`.
13. Invoke Tester:
    - run format/lint/typecheck
    - run microtask-focused tests
    - record artifacts/logs
14. If fail → Orchestrator sets `status=REFLECT` and invokes Reflector:
    - persist fix instructions
    - increment `attempts`
    - go back to step 9 (Coder)
15. If pass → **HITL checkpoint** (if `"test" in policy.hitl_checkpoints`): pause for
    human confirmation the verified change is acceptable before moving on — then mark
    microtask `DONE` and continue with next microtask.

**Stop condition (required):**

- Max attempts per microtask (e.g., 5). If exceeded → require HITL, re-plan, or fail.

> **General HITL pattern:** every phase boundary in this workflow *may* be gated by
> adding its phase to `hitl.hitl_checkpoints` (a per-run, per-risk-level choice —
> see §HITL below). All gates reuse the same `status=NEEDS_HITL_APPROVAL` /
> `hitl.pending_approvals[]` mechanism; there is exactly one HITL code path, configured
> differently per run. A `risk_level=high` run might gate every phase; a trusted
> low-risk run might gate only `merge`.

#### Phase D — Final Validation + Security

16. Tester runs full regression suite / build on the entire workspace.
17. **If `policy.security_audit_enabled`:** Orchestrator sets
    `status=NEEDS_SECURITY_AUDIT` and invokes Auditor (optional agent — see §5.6).
    - If security fails: create remediation microtask(s), return to Phase C.
    - If security passes: set `status=READY_FOR_PR`.

    **Else (default — Auditor disabled):** set `status=READY_FOR_PR` directly; security
    scanning is whatever `policy.security_scans_required` wires into the Tester's
    deterministic checks (no dedicated agent/LLM step).

#### Phase E — PR + CI loop

19. GitOps creates/updates branch `agent/run-<run_id>`, commits, and opens/updates PR.
20. Orchestrator sets `status=WAITING_FOR_CI`.
21. CI status arrives:
    - If CI fails: create microtask “Fix CI failure”, return to Phase C.
    - If CI passes: set `status=NEEDS_HITL_APPROVAL` (recommended).

#### Phase F — Merge/Deploy + Teardown

22. HITL approves merge/deploy (policy-dependent).
23. GitOps merges PR (if permitted).
24. Optional: Deployer deploys to staging/prod and runs smoke tests.
25. Teardown:
    - destroy Sandbox
    - purge `workspace` semantic namespace for `run_id` (optional but recommended)
26. Orchestrator sets `status=DONE`.

---

## 7) Data Flow Summary

- Repository files: **Sandbox filesystem**
- Run state + audit: **Episodic DB (Postgres)**
- Retrieval index: **Semantic DB (Vector store)**
- CI feedback: **GitHub Actions → GitOps webhook → Orchestrator**
- Artifacts: stored as references in Episodic DB (and optionally in external storage)

---

## 8) Minimum Security Requirements

- Sandbox is isolated, resource-limited, and network-restricted.
- GitHub credentials are least-privilege and never leaked to agent prompts.
- Secret scanning + policy checks are required before merge.
- HITL is recommended for production merge/deploy.

---

## 9) Next Implementation Deliverables

1. Define the `run_state` JSON schema (TypedDict/Pydantic) including microtask queue + stop conditions.
2. Implement LangGraph nodes + transitions for the state machine.
3. Implement GitOps webhook handler + PR creation/update.
4. Implement Context Engine baseline ingestion + incremental sync (workspace namespace).
