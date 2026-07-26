# src/orchestrator/ (part 1: core) — Code Explanation

The control plane: graph topology, blackboard schema, HITL machinery, bootstrap,
checkpointing, budgets, stop control, failure handling. Node runners are in
`orchestrator-nodes.md`.

---

## orchestrator.py (323 lines) — `OrchestratorGraph`

One class so the whole state machine reads top-to-bottom. Three sections:

### 1. Node wrappers (55–140)
Each graph node is a thin method: `_check_stop(state)` FIRST (raises
`RunStoppedError` if a human requested stop — ADR 0033; the docstring explains why
a sandbox kill alone can't stop a run: tool exceptions get swallowed as LLM
observations), then `node.enter` log, then delegate to the Runner class.

Gate wiring detail worth knowing:
- `_plan_reviewer` wraps its result in `HitlGate.apply(Phase.PLAN, ...)`.
- `_tester` gates `Phase.CODE` — but ONLY when `result["_tested_microtask_id"]`
  is set (i.e. a PASS); a FAIL (REFLECT) passes through ungated.
- `_deploy` is a pure **gate producer**: returns
  `HitlGate.apply(Phase.DEPLOY, {"status": READY_TO_DEPLOY}, state)` — no work.
- `_deploy_push` runs `DeployRunner`, and if the runner FAILED (secret block etc.)
  passes that through untouched — stamping DONE over it once left the checkpoint
  DONE while the DB said FAILED (comment documents the bug).
- `_hitl` (128–140): builds the payload, loops
  `interrupt(payload)` → validate `HitlResume` → **reject without note →
  re-interrupt with an error field** (never route on unactionable input) → on a
  valid decision, `HitlGate.resolve`.

### 2. Routers (146–252) — pure functions, status → node name
- `_status_gate(expected, next)` is a factory: proceed only when `status` ∈
  expected, else END. Used planner→reviewer (`{NEEDS_REVIEW}`). The docstring
  states the principle: *derive the legal next phase from status; don't just rule
  out the one bad value* — an inconsistent status halts rather than advances.
- `_plan_review_router`: NEEDS_HITL_APPROVAL→hitl / NEEDS_PLAN→planner (revise) /
  NEEDS_CODE→coder / else END.
- `_tester_router`: REFLECT→reflector / NEEDS_HITL_APPROVAL→hitl /
  NEEDS_CODE→coder / DONE→**deploy** (every deploy mode goes through the deploy
  gate, even dry-run).
- `_reflector_router`: NEEDS_CODE→coder / NEEDS_TEST→tester (re-synth).
- `_hitl_router`: dispatches post-decision. Subtlety at DONE: it must distinguish
  "CODE gate approved the FINAL microtask" (→ deploy) from "DEPLOY gate rejected"
  (→ END) — it inspects `hitl.pending_approvals[-1].type == "code"`.
- No router touches the DB. All side effects live in nodes.

### 3. Construction (`_build`, 258–320)
8 nodes registered, `START → planner` unconditional (bootstrap is OUTSIDE the
graph), conditional edges wired per the routers, `deploy_push → END`. Module tail:
`graph = OrchestratorGraph().compile()` — the singleton the API imports.

**Why bootstrap is not a node** (class docstring): LangGraph resume continues from
the last completed node, not START — a bootstrap node would be *skipped on every
resume*, exactly when re-verifying the sandbox matters most (ADR 0005).

## state.py (707 lines) — the shared vocabulary

Everything typed. The key models:

- **Enums:** `Phase` (plan/code/test/reflect/deploy — gate names), `RunStatus`
  (the routing vocabulary: NEEDS_PLAN … NEEDS_HITL_APPROVAL, DONE, FAILED),
  `MicrotaskStatus` (PENDING → IN_PROGRESS → COMPLETED/DONE/BLOCKED…),
  `DeployMode` (dry_run/push/pr/local_only), `EventType` (every audit event name —
  the single registry).
- **`JSONValue`** (top of file): `Any` annotated with an explicit `anyOf` JSON
  schema — because a bare `Any` serializes to `{}`, which Anthropic's structured-
  output converter rejects ("Schema must have a 'type'…"). A provider-quirk fix
  encoded in the type system.
- **Config models** (ADR 0020/GH #71): `ProviderProfile` (endpoint/key/thinking
  toggle/max_retries/extra_request_params), per-agent `*LLMConfig` subclasses of
  `BaseLLMConfig`, `Knobs` (every tuning value, timeouts sized for
  thinking-enabled Claude models), `LLMConfig` (five per-agent selections),
  `RunConfig` (resolved once at creation; `provider_for()` resolves an agent's
  provider by id).
- **Planning contract:** `Plan` (+ `review_feedback: List[ReviewFeedback]` — the
  reviewer/HITL channel), `Microtask` (description=WHAT, approach=HOW,
  target_files/context_files declared BEFORE coding, attempts counter),
  `AcceptanceCriterion` (+ `IOExample` — the Planner→Tester grounding contract;
  `render()` produces the prompt line).
- **Evidence:** `Evidence.checks` = five `CheckOutput` slots (lint/typecheck/
  unit/integration/build), each carrying an `artifact_ref` into the artifacts
  table — the blackboard holds POINTERS, content lives in the DB.
- **HITL models:** `PendingApproval` — note `forward_status` (the original
  advance status, so approve re-emits exactly it) and `microtask_id` (so a code
  reject can reset the right microtask).
- **`Iteration`:** loop counters + `tokens_used_total`. (`global_iteration` /
  `global_iterations_used` are acknowledged dead fields — honest answer if asked.)
- **`BlackboardState(TypedDict)`** (559): the full board. Notable channels:
  `messages` is `Annotated[List, operator.add]` (LangGraph append-merge);
  `test_rejection_note` (HITL/reflector → tester, cleared after read);
  `test_resynth_count`; `suite_baseline_failures` (GH #28/#72 — pristine-tree
  failing-test count, delta-gating); `deploy_commit_message`; `deploy_mode`
  (resolved once); `local_export_path`.
- `create_initial_blackboard_state(...)`: every key initialized explicitly —
  TypedDict has no defaults; note `hitl=hitl` passes the caller's gate config in.

Two `BACKCOMPAT` blocks (RunDefaults/Constraints) alias the old
`max_total_iterations` name so pre-rename checkpoints/settings still load —
tagged for removal on the next DB wipe.

## hitl.py (349 lines) — `HitlGate`

The single code path for every human gate. Three verbs:

### `apply(phase, result, state)` (142–189) — called by phase nodes
If the phase is gated AND the node produced that gate's trigger status:
- capture + strip the internal `_tested_microtask_id` key (must never reach the
  TypedDict state),
- create a `PendingApproval` storing `forward_status` (the exact status to re-emit
  on approve — critical because the CODE gate triggers on BOTH `NEEDS_CODE` and
  `DONE`) and `microtask_id`,
- rewrite status to `NEEDS_HITL_APPROVAL`, log `HITL_REQUESTED`.
Otherwise return the result untouched — one `if` decides gated vs not.

`GATES` table (106–119): PLAN(trigger NEEDS_CODE, reject NEEDS_PLAN),
CODE(trigger (NEEDS_CODE, DONE), reject NEEDS_CODE),
DEPLOY(trigger READY_TO_DEPLOY, reject DONE — run ends un-deployed, no loop-back).

### `request_payload(state)` (192–232) — called by the hitl node
Builds the human-facing `HitlRequest` from the latest pending approval, with a
per-phase prompt text (plan file ref for plan gates, reflection path for code
gates, commit-message hint for deploy).

### `resolve(state, decision)` (235–278) — called after interrupt returns
- **Approve** → next status = the stored `forward_status`;
  `_approve_payload` may carry `deploy_commit_message` (deploy gate only).
- **Reject** → the gate's reject status + `_write_feedback` routes the note:
  - PLAN → append a `ReviewFeedback(source=HUMAN)` to `plan.review_feedback` —
    the SAME channel the LLM reviewer writes; the planner can't tell them apart.
  - CODE → reset the tested microtask to IN_PROGRESS, set it current, and seed a
    `Reflection(failure_type="human_rejection")` — the SAME channel the reflector
    writes; the coder retry mechanism is reused as-is.
  - DEPLOY → nothing to seed (run ends).
Marks the approval resolved (reviewer/notes recorded), logs `HITL_DECIDED`.

`HitlStateError` — raised when the hitl node runs with no pending approval:
"a routing bug, not a bad human decision — fail loudly."

## bootstrap_node.py (221 lines) — `BootStrapper`

Called by the launcher BEFORE `graph.invoke`, on EVERY invocation:
1. `_upsert_run_record` — insert the Run row (with denormalized display fields:
   headline coder model, deploy_mode, token cap) or touch `updated_at` (heartbeat).
2. `_ensure_sandbox_running` — warn if the image is missing, then the idempotent
   `sandbox_create` (create OR reconnect), `SANDBOX_CREATED` event.
3. Clone decision: `workspace_path == WORKDIR` means "not cloned yet" (the
   pre-clone default) — covers both first run AND a resume after a failed clone.
   On clone: `GitOps.clone_and_checkout` (token via `RepoStore.resolve_token`),
   `_ensure_gitignore`, `_ingest_baseline`, then status → NEEDS_PLAN + three
   events. Resume path: return just the refreshed `Sandbox` model.
- `_ensure_gitignore` (177–198): seeds/merges `_GITIGNORE_PATTERNS` (deps, caches,
  secrets, `.agentic_tests/`) via a `sh -c` loop with `grep -qxF` per line —
  idempotent, append-only, preserves the repo's existing file. Best-effort:
  push-time excludes are the backstop.
- `_ingest_baseline` is try/except-wrapped: a failed semantic index "degrades to
  the planner re-reading the sandbox — must never break the run."

## checkpointer.py (67 lines) — `CheckpointerFactory`

PostgresSaver over a **psycopg ConnectionPool** (max 20), separate from
SQLAlchemy's pool because PostgresSaver needs raw psycopg connections
(autocommit + dict_row) held for process lifetime. Why a pool, not one shared
connection: a lone psycopg connection is NOT thread-safe, and the graph is driven
from multiple threads (several runs' workers + API polls) — ADR 0032. Uses
`JsonPlusSerializer` so the Pydantic models round-trip.

## token_budget.py (118 lines)

- `token_budget_exceeded(iteration, constraints)` — the one-line check every node
  runs FIRST.
- `record_usage(...)` — the single sink for spend: updates `Iteration`
  (returned → node result dict), mirrors onto `runs.tokens_used_total`, logs
  `llm.usage`, computes `cost_usd` via `cost_for` and stores it ON the event —
  "the durable event is self-contained: the API cost view and Grafana both read
  this ONE number" (the dashboard can't call Python).
- `record_usage_safe(...)` — crash-path variant, no-op on zero usage. Every
  node's `except` block calls it: "the LLM calls already hit the provider and
  were BILLED — losing them because the node then failed understates everything."

## run_control.py (29) / run_failure.py (70)

- `run_control`: module-level `_stop_requested: set[str]` + lock;
  `request_stop` / `is_stop_requested` / `clear_stop`. Deliberately trivial —
  in-process cooperative flags (single-process deployment).
- `RunFailureHandler.handle(...)`: the one terminal-FAILED path — sets
  `Run.status = FAILED`, emits `RUN_FAILED` (+ retracts the run's unconfirmed
  lessons), returns the `{"status": FAILED}` node result. Mirrors EventLogger:
  caller owns the commit.

---

## Relationships

```
Service/RunLauncher ─► BootStrapper.invoke() ─► graph.invoke(initial_state | Command(resume))
graph (OrchestratorGraph) ─► node wrappers ─► Runner classes (orchestrator-nodes.md)
node wrappers ─► _check_stop (run_control) ─► RunStoppedError → Service
phase nodes ─► HitlGate.apply ─► hitl node ─► interrupt() ⇄ POST /resume ─► HitlGate.resolve
every Runner ─► token_budget (guard + record) ─► events + runs row
terminal failures ─► RunFailureHandler ─► FAILED + run.failed event
checkpointer ─► Postgres `checkpoints` tables (one snapshot per superstep)
```
