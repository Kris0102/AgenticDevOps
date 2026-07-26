# System Design — Complete Memorization Guide

The whole architecture in 7 chunks. Learn one chunk per sitting; each has a hook
word. Master line:

> **"Objective → film of 8 steps → 5 specialists → blackboard-star-status →
> 5 safety rings → 4 failure words → 4 memories."**

---

## Chunk 1 — WHAT (hook: *one sentence*)

AgenticDevOps is a multi-agent system that takes a plain-language objective
("add input validation to the /users endpoint") and autonomously **plans,
implements, tests, and ships** a code change to a real GitHub repository, with a
human approving at defined gates.

Deployment shape (deliberate, documented): **self-hosted, single-user, localhost.**
One Postgres (data + vectors + checkpoints + metrics), Docker for sandboxes,
LangGraph for orchestration, FastAPI + React dashboard as the control surface.
Every "why not Kafka/microservices/queue?" answer starts here: right-sized to the
deployment we actually have.

Tech stack one-liner: Python, LangGraph + LangChain, Pydantic state, SQLAlchemy
2.0, Postgres + pgvector, Docker, FastAPI, structlog, Grafana, sentence-transformers
(local MiniLM), tree-sitter, gitleaks.

---

## Chunk 2 — THE RUN LIFECYCLE (hook: *film of 8 steps*)

Memorize as a story you can narrate forwards. Bold words are the recall anchors:
**Bootstrap, Plan, Review, Code, Test, Reflect, Deploy, Learn.**

### 1. Bootstrap (before the graph, always first)
`RunLauncher` calls the `BootStrapper` directly — deliberately OUTSIDE the graph so
routers stay side-effect-free and setup runs exactly once, idempotently:
- Upsert the `Run` row (status, repo, base SHA).
- Create the run's Docker sandbox — or **reattach** to an existing container (IDs
  live in the `sandboxes` DB table, so a process restart doesn't orphan it).
- Clone the repo into the sandbox (honors `base_ref`; token resolved via
  repo-registry → settings → anonymous chain).
- Ingest the codebase into the semantic index (`baseline` namespace) and seed a
  `.gitignore` for later deploy hygiene.

### 2. Plan
The Planner runs **two phases**:
- **Investigate** — a ReAct tool loop (file reads, shell, semantic search) bounded
  by `PLANNER_MAX_TOOL_STEPS`. Nothing is answered from the model's memory of
  "typical repos"; it must look.
- **Synthesize** — structured output: a `Plan` of `Microtask`s, each a contract:
  target files, approach, `acceptance_criteria` (structured, with `IOExample`s for
  any precise value).
A deterministic **quality gate** (`quality.py`) rejects bad microtasks — including
*tautological* criteria like "all tests pass" — raising `BadMicroTaskException`;
the validation error is fed back into the next synthesis attempt (max
`PLANNER_MAX_SYNTHESIS_ATTEMPTS`). The planner also reads the repo's **lessons**
(prevention beats detection).

### 3. Review (actor–critic)
The Plan Reviewer **independently investigates** with read-only tools — it does not
grade the Planner's homework by reading the Planner's summary. Output is a
structured approve/reject; on reject only **the latest verdict + structured
blockers** go back (stacked prose reviews made the loop oscillate — convergence
fix). The loop is capped by `max_planner_reviewer_iterations`, enforced *in the
node*. On approve → optional **plan gate** (human) → Coder.

### 4. Code
The Coder gets ONE microtask, with **hybrid grounding**: the runner pre-reads
target/context files into the prompt, then a single ReAct loop edits files through
the gateway. Verification is deterministic, in the runner:
- **Diff check**: post-loop `repo.diff` — empty diff → re-prompt (up to
  `CODER_MAX_EMPTY_DIFF_RETRIES`). "I edited the file" without a diff is nothing.
- **Compile gate**: `py_compile` / `node --check` on target files; errors appended
  to the SAME warm conversation and re-run (up to `CODER_MAX_COMPILE_RETRIES`).
- Per-microtask **attempt cap** checked before starting (`_check_stop_limit`).
After a diff, the changed files are re-embedded (`workspace` namespace) so semantic
search stays current.

### 5. Test
The Tester is a **phase pipeline** (detect → deterministic checks → synthesis →
trivial-guard → execute → verdict), two tracks:
- **Track A (deterministic)**: the repo's existing suite + config-gated
  build/typecheck/lint. Suite gating is **baseline-delta**: the pre-existing
  failure count was measured at the start, and only NEW failures gate — the agent
  isn't blamed for a repo that was already red.
- **Track B (synthesized)**: tests written from the *Planner's acceptance
  criteria* — independent of the Coder's code (don't grade your own homework). A
  deterministic AST/regex guard rejects vacuous assertions; values are asserted
  only when the plan supplied an `IOExample` (an LLM must never invent expected
  numbers). Synth tests live in ephemeral `.agentic_tests/`, never shipped.
- **Verdict**: computed from check results — no LLM on the accept path. Vocabulary
  includes **UNVERIFIED** (loud skip) when nothing could be decided, because a
  false FAIL that nothing can flip drives the loop to block correct code.
PASS → mark microtask done → **code gate** → next microtask, or → Deploy if final.
FAIL → Reflect.

### 6. Reflect (failure only)
Never runs on green (zero LLM cost on the happy path). Reads honest evidence
(head+tail elision, never misleading truncation), the microtask's criteria, and
the failing test source. Emits a `Reflection` + `fix_target`:
- `code` → Coder retries WITH the diagnosis injected.
- `test` → the synthesized test itself is wrong → Tester re-synthesizes (bounded
  by `REFLECTOR_MAX_TEST_RESYNTH`; counter resets when the coder runs).
- `blocked` → contradictory requirements: run ends BLOCKED with the conflict named.
**Deterministic backstop** (`_diff_cycle_kind`): compares the per-attempt diff
artifacts — `A→B→A` = revert cycle, `…B→B` = stagnation — and forces BLOCKED even
if the LLM diagnosis misses it. Needs ≥3 attempts; suppressed while a test-resynth
still has budget (a correct "no code change" isn't stagnation).

### 7. Deploy (terminal, always gated)
Every mode funnels through the **deploy gate** — the human sees the exact final
diff and a commit-message field **prefilled with the exact string that will be
committed** (one code path for prefill and push). On approve, dispatch by the
run's `DeployMode` (resolved ONCE at run creation, checkpointed — mid-run settings
changes can't alter a running run):
- `dry_run` — ship nothing (the diff was still human-reviewed).
- `push` — stage/commit/push to `agent/run-<id>` (or validated override branch).
- `pr` — push + real GitHub PR with a deterministic body; PR failure only
  *degrades* (code is already on the branch).
- `local_only` — `git bundle` the sandbox commit, fetch into the user's local
  clone; failure FAILS the run (the export IS the deliverable).
Rails: **gitleaks scans before any push and hard-blocks** (run FAILED on a
finding); the default branch is refused at four layers; staging uses pathspec
excludes (deps/secrets/synth tests never ship).

### 8. Learn
On **confirmation only**: reviewer approve → plan lesson; tester PASS → code
lesson. Reflector diagnoses are NOT stored (unconfirmed guesses). Lessons are
embedded and retrieved semantically for future runs on the same repo.

---

## Chunk 3 — THE FIVE AGENTS (hook: *5 specialists*)

One job + one distinguishing fact + one enforcement detail each:

| Agent | Job | Distinguishing fact | Enforced by |
|---|---|---|---|
| **Planner** | objective → microtask contracts | two-phase: investigate before synthesize; quality-gated retries | `quality.py` raises, node retries |
| **Reviewer** | approve/reject the plan | investigates independently; sees only the latest verdict | read-only tool allowlist |
| **Coder** | implement ONE microtask | cannot run tests at all | no `pytest` in `CoderPermissions.allowed_commands` |
| **Tester** | own the verdict | deterministic verdict; can say UNVERIFIED | verdict computed from `CheckResult`s, no LLM call |
| **Reflector** | diagnose failures | failure-only; can indict the test, not just the code | graph edge only fires on FAIL |

Common shape: each agent is a plain class, `invoke(state) -> dict of changed keys`,
wrapped by a node-runner in `orchestrator/` that handles config resolution, DB
persistence, events, and caps. All tool access goes through the gateway under the
agent's role. All get capped tool output (`truncate_for_llm` — one runaway file
read once cost ~245k tokens because ReAct resends history every step).

---

## Chunk 4 — COORDINATION (hook: *blackboard, star, status*)

- **Blackboard**: one shared `BlackboardState` (Pydantic/TypedDict, `state.py`) is
  the system's working memory. Agents never mutate it — they return only changed
  keys; LangGraph merges. Why: every state change is attributable, diffable, and
  checkpointable; no hidden coupling.
- **Star topology**: agents never call each other; the orchestrator mediates all
  interaction. Why: N agents chatting = N² unauditable conversations that don't
  reliably converge; one hub = one place for routing, caps, gates, and logging.
- **Status-driven routing**: routers read status enums (`NEEDS_PLAN`, `NEEDS_CODE`,
  `REFLECT`, `NEEDS_TEST`, `READY_TO_DEPLOY`, `DONE`) with per-edge expected-status
  allowlists. **Routers are pure** — every side effect (DB writes, events, status
  updates) lives in nodes. Why: the LLM decides *content*, the state machine
  decides *flow*; deciders with side effects are undebuggable.
- **Checkpointing**: LangGraph PostgresSaver snapshots the blackboard every
  superstep, `thread_id = run_id`, over a psycopg **connection pool** (concurrent
  runs + polls each borrow a connection). Crash-anywhere/resume-anywhere. A HITL
  pause is a suspended checkpoint (`interrupt()`), not a parked process: resume =
  `Command(resume=...)`, one `graph.invoke()` per gate, **no re-bootstrap**.
- **Config resolve-once**: a run resolves provider/model/knobs and DeployMode at
  creation onto the blackboard; agents take config as a mandatory argument
  (fallback only at the node boundary). What was preflighted is what executes.

---

## Chunk 5 — SAFETY (hook: *5 rings, inside → out*)

1. **Gateway** (`tool_gateway/`) — deny-by-default. Per-role `RolePermissions`:
   allowed tools AND allowed shell commands. Every call → `tool.executed` /
   `tool.denied` audit event. Security is code, not prompts — the Coder can't run
   the test suite even if its prompt is jailbroken.
2. **Sandbox** (`sandbox/`) — Docker container per run; sandbox is the source of
   truth for code during the run. Blast radius of any action = one disposable
   container. Independent layer from ring 1 → defense in depth.
3. **Caps** — no loop without a number: per-microtask attempts,
   planner↔reviewer iterations, per-agent tool steps, synthesis retries, test
   resynths, compile retries, and a **run-wide token budget**. Caps enforced in
   nodes (auditable), not routers.
4. **Human gates** (`hitl` node) — plan / code / deploy, configured per run; the
   graph physically suspends. Reject REQUIRES a note; the note feeds the same
   channels agents already consume (plan → review feedback; code → a seeded
   Reflection). Deploy gate fires for every mode, dry-run included.
5. **Deploy rails** (`gitops/`) — gitleaks hard-block, main-branch refusal (4
   layers), pathspec staging excludes, deterministic commit message + PR body so
   the human approves the literal artifact, token centralized and never logged,
   secrets Fernet-encrypted at rest.

---

## Chunk 6 — FAILURE TAXONOMY (hook: *4 words*)

| State | Meaning | Trigger | Exit |
|---|---|---|---|
| **FAILED** | real verdict | attempts exhausted, secret found, iteration cap, local-export failure | terminal; `failure_reason` surfaced from the last `run.failed` event |
| **BLOCKED** | provably unwinnable | contradictory requirements — reflector `blocked` decision or diff-forensics backstop (revert cycle / stagnation) | terminal; the conflict is named in the artifact ("requirements conflict at X", not "3/3 attempts") |
| **STALLED** | recoverable interruption | crash, transient 429/5xx, token budget exhausted | `POST /continue` re-drives from the checkpoint (token stalls require an `additional_tokens` top-up — validated, audited via `run.token_budget_raised`); or `POST /fail` closes it |
| **STOPPED** | human hard-stop | `POST /stop`; cooperative flag checked at the TOP of every node (a sandbox kill can't reliably interrupt a ReAct loop — tool errors get eaten as observations) | terminal; bounded to at most one in-flight node |

Governing principle: **never convert a recoverable state into a terminal one.**
Corollary: failure severity is *calibrated* per case — secret leak = hard block;
PR-creation failure = degrade; local-export failure = fail (it IS the deliverable).

---

## Chunk 7 — MEMORY (hook: *4 stores, 4 lifetimes*)

| Store | Lifetime | Content | Why separate |
|---|---|---|---|
| **Blackboard checkpoints** | one run | full working state per superstep | resume/HITL/crash recovery |
| **Events table** | forever | every action: tools, LLM usage, lifecycle, gitops | audit trail; monotonic `seq` = dashboard history cursor; ALSO the metrics + USD-cost source (Grafana + pricing are pure derivations over it — cost stays correct even after a crash) |
| **Code index** (`code_chunks`) | per repo, synced | AST-chunked (tree-sitter) code, local MiniLM 384-dim embeddings, pgvector | agents locate by meaning; `baseline` + `workspace` namespaces keep retrieval honest while agents edit |
| **Lessons** | per repo, cross-run | confirmed failure lessons + human operator rules | system improves on a repo over time; rules always injected unranked (never lose a similarity race), human-touched entries immune to retraction |

Supporting cast (know these exist, one line each):
- **API** (`src/api/`): FastAPI control plane; drives the SAME graph; 202 +
  background worker; preflights provider + repo before creating anything.
- **Observability**: structlog → ring buffer → SSE for live logs; events table for
  durable history — two channels, two lifetimes, both on purpose.
- **Settings hub**: DB is source of truth, `.env` only seeds; secrets masked +
  encrypted; per-run overrides win.
- **Repo registry**: validated repos, per-repo tokens, canonical URL = the memory
  key joining lessons + code index.
- **Grafana**: provisioned dashboards, pure SQL over the audit tables — success
  rate, autonomy ratio, denials, secrets blocked, cost per delivered microtask.

---

## Self-test (do this cold, day before)

1. Recite the 8 lifecycle steps with one sentence each. *(← generates most answers)*
2. Name the 4 failure words + trigger + exit for each.
3. Name the 5 safety rings inside-out.
4. For each agent: the one thing that makes it trustworthy (planner: quality gate;
   reviewer: independent investigation; coder: diff+compile; tester: deterministic
   verdict; reflector: failure-only + backstop).
5. Answer in one breath: "Why is no LLM ever on the accept path?" — *because
   acceptance must be reproducible and evidence-based; LLM judgment only routes
   (reflector), it never accepts.*
