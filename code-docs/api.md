# src/api/ — Code Explanation

~2855 lines. FastAPI control plane + the `Service` run engine. Drives the SAME
checkpointed graph as the launcher — a remote control, not a second engine.
Human-driven, so it bypasses the ToolGateway.

---

## app.py (469 lines) — the routes

Thin layer: validate → delegate to `Service` → map domain exceptions to HTTP
codes. Route inventory (grouped):

- **Runs:** `POST /runs` (202; resolve+preflight FIRST, then background),
  `GET /runs/{id}` (+`/cost`, `/config`), `PATCH /runs/{id}/model` (mid-run
  per-agent model switch), `PATCH /runs/{id}/config` (constraint patch),
  `POST /resume|continue|fail|stop`, `DELETE /runs/{id}`.
- **HITL:** `GET /runs/{id}/hitl` (review context), `POST /runs/{id}/resume`
  (body = `HitlResume`).
- **Telemetry:** `GET /runs/{id}/events?after=<seq>` (durable),
  `GET /runs/{id}/logs` + `/logs/stream` (SSE, live).
- **Registry:** `GET/POST /repos`, `PUT/DELETE /repos/{id}`,
  `POST /repos/{id}/validate`, `GET /repos/{id}/branches` (live, never cached).
- **Lessons:** `GET/POST /repos/{id}/lessons`, `PUT/DELETE /lessons/{id}`.
- **Settings:** `GET/PUT /settings`, `GET /settings/schema` (typed field
  descriptors for the UI, incl. the thousands-`scale` convention),
  `POST /settings/providers/{id}/test`, `POST /settings/github/test`.

The key pattern in `create_run`: **all validation happens synchronously in the
route** (repo resolution, provider preflight, deploy-rights check, local-path
validation) → any 4xx means NO run row, no sandbox, no cost. Only after
everything passes is `service.run(...)` scheduled via `BackgroundTasks` and 202
returned with the minted run_id.

## requests/requests.py (139 lines)

- `RunRequest` — the run intent: objective/trigger/constraints/policy/hitl +
  repo (**exactly one of `repo` (legacy) or `repo_id` (registry)** — a pydantic
  model-validator enforces it), optional per-run `llm` override,
  `commit_message`, `push_branch`, `deploy_mode`, `local_export_path` (a second
  validator ties it to `local_only` mode).
- `ContinueRunRequest` — optional `additional_tokens` (validator: positive if
  set) — the token-budget top-up.
- `AgentModelPatch` / `RunConfigPatch` — mid-run settings-console changes.
- Repo/Lesson CRUD request models.

## responses/responses.py (356 lines)

View models, all derived: `RunResponse` (+`stalled_kind` — read back from the
last RUN_STALLED event's `kind` so the dashboard knows to demand a token
top-up), `RunResponseWithDetails` (run list), `HitlReviewContext` (a `PlanView`
for plan gates, green/red diff for code gates, commit-message prefill for deploy
gates), `EventView`, `LogView`, `RunCostView`, `RunConfigView`, `RepoView`,
`LessonView` (with the `injected` flag), `BranchListResponse`.

## service/service.py (1834 lines) — `Service`

The engine. Class docstring carries the two core contracts:
1. **One `graph.invoke()` per gate** — `run` drives to the first interrupt/END,
   each `resume` exactly one gate further.
2. **Stateless service** — all durable state is the LangGraph checkpoint
   (thread_id=run_id); the class is constructed once at startup.

### Concurrency (ADR 0032, docstring + `_active`)
- `_active: set[str]` + lock = "which runs does THIS process currently drive".
  `mark_active` is an atomic **test-and-set**: a second driver on the same
  run_id gets False → route 409s (two `graph.invoke`s over one checkpoint would
  corrupt it). Claimed synchronously in the route, BEFORE the background task,
  so the 202 response never briefly reads STALLED.
- Empty after a restart → every previously-running run correctly reads STALLED
  and becomes continuable. This is how RUNNING vs STALLED is *derived*: a
  non-terminal checkpoint + no active worker = STALLED.

### `resolve_and_preflight(request)`
Resolves the RunConfig (override › DB › env), then health-checks **every
distinct (provider, model) pair across all five agents** (deduped) — "a
mis-keyed provider on ANY agent must fail fast before a run is created, not
mid-run on the first agent that touches it."

### `run(...)` / `resume(...)`
- `run`: `bind_run` (logs), build the initial blackboard
  (`resolve_deploy_mode` fixes the DeployMode NOW), `_bootstrap` (BootStrapper
  outside the graph), `_drive`, `_finalize`, respond. `finally: mark_inactive`.
- `resume`: NO bootstrap (mid-graph pause in a live process — sandbox up,
  checkpoint intact); requires a pending interrupt (else 409);
  `log_bus.reset` (the paused run will produce again);
  `_drive(Command(resume=decision))`.

### `_drive(run_id, input)` — the exception ladder that defines run outcomes
```
TokenBudgetExceededError → _mark_stalled(kind="token_budget")   # policy knob, human can raise
RetriableError           → _mark_stalled()                       # LLM hiccup, retry same node
RunStoppedError          → log only                              # stop_run's wait loop finishes it
other transient (sniff)  → _mark_stalled()                       # defensive
any other Exception      → _mark_stalled("Uncaught …")           # a BUG is not a domain failure
```
The last case has the strongest comment: an uncaught crash leaves the checkpoint
intact and is "one fixed bug away from finishing" — so it STALLS (continuable),
never force-FAILs. (Previously it wrote FAILED while the checkpoint stayed
non-terminal → list said FAILED, detail said STALLED — a real bug this fixed.)
`_mark_stalled` deliberately does NOT touch `Run.status` — it only emits a
`RUN_STALLED` event with the reason+kind; STALLED is derived state.

### `raise_token_budget(run_id, additional_tokens)` (ADR 0025)
Called synchronously from the route before `/continue`'s background re-drive (so
a bad value 422s the HTTP request): reads the checkpoint, **ADDS** to the cap
(increment, not absolute — continuing unchanged would instantly re-trip the
guard), validates the new cap exceeds tokens already used, patches the
checkpointed Constraints via **`graph.update_state`**, mirrors onto
`runs.max_total_tokens`, emits `run.token_budget_raised` with old/new/added.

### Run controls
- `continue_run` — re-drive a STALLED run from its checkpoint (`_drive(None)` —
  LangGraph resumes from the last snapshot).
- `give_up` (`POST /fail`) — human closes a stalled case → terminal FAILED.
- `stop_run` — `request_stop(run_id)` (the cooperative flag), then WAITS for the
  driver thread to exit (`is_active` poll, bounded → `RunStopTimeoutError`),
  then writes terminal STOPPED + `run.stopped` event.
- `delete_run` — 409 while live; deletes the run's rows (+ checkpoint).

### HITL context + the prefill invariant
`review_context` builds what the human reviews: plan gate → the plan +
microtasks; code gate → the diff; deploy gate → `_deploy_message_preview`, which
calls **the same `build_commit_message`** the DeployRunner will use — the gate
prefill and the pushed message cannot diverge (ADR 0035).

### Cost view
`run_cost` aggregates the `llm.usage` events by (agent, model), summing the
`cost_usd` each event already carries (priced once in `token_budget.
record_usage`); models without a price flag `has_unpriced` → the total is an
honest lower bound.

### Lessons plumbing
`_lesson_views` derives `injected` for operator rules from the SAME
`OPERATOR_RULE_LIMIT` constant the store uses — a displaced rule visibly reads
"not sent to the agents" instead of silently doing nothing.

### Repo gating (ADR 0029)
`_require_run_rights(result, deploy_mode)` — the mode decides the rights:
push/pr need push access (pr also PR access), dry_run/local_only need clone
only. Checked at run creation against the SAME mode the deploy node will
execute — the resolve-once invariant end to end.

## settings_schema.py (57 lines)
Typed field descriptors for the Settings UI (labels, types, `scale` for
thousands-entry of token fields) — the UI renders from this instead of
hardcoding field lists.

---

## Relationships

```
dashboard (React) ─► app.py routes ─► Service
Service.run/resume/continue ─► BootStrapper (run only) ─► graph.invoke / Command(resume)
Service._drive except-ladder ◄── Exceptions/* (the failure taxonomy, one branch each)
Service._deploy_message_preview ─► gitops/commit_message (same string DeployRunner pushes)
Service.run_cost ◄── events (llm.usage, cost_usd)      Service.logs/stream ◄── log_bus
Service repo/lesson/settings methods ─► RepoStore / LessonStore / SettingsStore
```

Exam sentence: "The API holds no run state — RUNNING vs STALLED is derived from
'is a worker in this process driving that run_id', which is also what makes
restart recovery free: after a crash the set is empty, so every orphaned run
reads STALLED and is continuable from its checkpoint."
