# src/orchestrator/ (part 2: node runners) — Code Explanation

Six Runner classes — the glue between the graph and the agents. All share one
shape (the project's node-runner pattern):

```
invoke():
  1. token-budget guard  (raise TokenBudgetExceededError → STALL)
  2. build gateway/tools (per-node ToolGateway with a search-only ContextEngine)
  3. resolve config      (state["config"] or resolve_run_config() — fallback at the boundary)
  4. call the agent      (plain class in src/agents/)
  5. except ladder:      RunExecutionError → RunFailureHandler (FAILED)
                         RetriableError → re-raise (STALL)
                         other + is_transient_llm_error → wrap in RetriableError (STALL)
                         other → record crash usage, re-raise (STALL — "not proven unrecoverable")
  6. record_usage        (tokens → Iteration + runs row + llm.usage event, even on crash paths)
  7. persist             (microtask rows, artifacts, Run.status, domain events)
  8. return node dict    (changed keys only)
```

Once you can recite that shape, each runner is "the shape plus its specifics".

---

## planner_node.py — `PlannerRunner`

Specifics:
- `_init_tools`: gateway + `PlannerTools.build_tools()`; failure degrades to NO
  tools (warn + `[]`) rather than crashing — the planner can still synthesize
  from the prompt alone.
- `_run_planner` captures `planner.token_usage` in a `finally` — so a mid-invoke
  crash still reports the spend (the caller's except block records it).
- `_persist_results` has the **plan-replacement rule**: a fresh plan DELETES
  MicrotaskRecord rows not in the new id set — a rejected draft's microtasks
  must not interleave with the revision on the dashboard; surviving ids are
  `merge`d (upsert).
- Plan artifact id is **versioned by the planner↔reviewer iteration**
  (`art_<run>_plan_v<n>`) so a re-drafted plan doesn't overwrite the rejected one
  — both stay browsable.

## reviewer_node.py — `ReviewerRunner`

Specifics:
- **The planning-loop cap lives HERE** (not in a router):
  `planner_reviewer_iteration_current >= max_planner_reviewer_iterations` →
  `RunFailureHandler.handle(...)` with reason "loop did not converge". Enforced in
  the node so it produces a real FAILED status + audit event.
- Increments the iteration counter via `iteration.model_copy(update=...)` on the
  RETURN dict — the project's "never mutate state in place" rule in action.
- **Lesson write on approve only** (ADR 0018): when the result status is
  NEEDS_CODE (approve), `LessonStore.record_plan_lessons` persists the addressed
  rejections — approval is the confirmation that the prior rejections were valid.
  Saving on reject would store unvalidated guesses.
- Persists each verdict as a `review` artifact (`[approve|reject] notes`) and
  emits `PLAN_REVIEWED` — "without it the audit log has no record a review
  happened, only a status flip."

## coder_node.py — `CoderRunner`

Specifics:
- `_select_microtask` — pure derivation from state: reuse
  `current_microtask_id` if unfinished (the reflector-retry path), else first
  PENDING by `idx` whose `dependencies` are all COMPLETED/DONE. No bookkeeping
  field ("derived from state" — a project principle).
- `_check_stop_limit` — **the per-microtask attempt cap** (audit finding #1):
  `attempts >= (microtask.max_attempts or constraints.max_microtask_attempts)` →
  `RunExecutionError` → FAILED. This is what bounds the coder↔tester↔reflector
  loop.
- `_gather_file_context` — **hybrid grounding**: pre-reads every target/context
  file THROUGH THE GATEWAY (each read is a `tool.executed` audit event; missing
  file → `_MISSING_FILE_SENTINEL` so the prompt says "you are creating this file")
  — grounding is guaranteed, not left to optional tool calls.
- Crash path has an extra subtlety: it **commits the session before re-raising**
  — the grounding pre-reads' audit events really happened; without the commit the
  exception would roll them back and a stalled run lost its coder audit trail.
- Post-run: pops the internal `_diff_text` hand-off, persists the microtask row +
  the diff as an artifact (`kind="diff"`, content = the actual diff text — this
  is what `_diff_cycle_kind` later does forensics on), then
  `_sync_workspace(files_touched)` → re-embeds changed files (non-fatal).

## tester_node.py — `TesterRunner`

Specifics:
- `final_validation = not _has_remaining_microtasks(...)` — decides whether a
  PASS emits NEEDS_CODE (next microtask) or DONE (→ deploy).
- **The verdict is 8 lines** (`_verdict_passed`): find the check labeled
  `unit_tests`; PASSED or **SKIPPED** → pass (skipped = "nothing to verify,
  nothing to fix" — the ADR 0037 unverified semantics); missing check → fail
  (route to reflector). No LLM anywhere near this decision.
- `_persist_results`: stores one artifact PER check (`test-<label>`, full
  command/exit/stdout) and updates `evidence.checks.<label>` with the
  artifact_ref — state carries pointers, DB carries content. Also stores the
  synthesized test SOURCE as its own `test-synth` artifact (with guard verdict +
  role header) so a human can read what Track B wrote.
- On PASS: marks the microtask COMPLETED/DONE, writes the **code lesson**
  (`_record_code_lesson` — the latest reflection for this microtask is now
  confirmed by green; a first-try pass has no reflection → nothing stored),
  emits MICROTASK_COMPLETED; on FAIL emits MICROTASK_FAILED.
- `_build_state_result`: FAIL keeps the microtask in flight
  (`current_microtask_id` set) with status REFLECT; PASS clears it and sets
  `_tested_microtask_id` — the key `HitlGate.apply(Phase.CODE)` consumes. The
  `shared` dict carries `suite_baseline_failures` on EVERY exit so the run-level
  baseline survives to the next microtask.

## reflector_node.py — `ReflectorRunner` (the richest one)

Flow after the agent returns:
1. Pop internal keys: `_fix_target` ("code"/"test"), `_reflector_blocked`.
2. `will_resynth_test` = fix_target=="test" AND resynth budget left. This flag
   **suppresses the stagnation backstop** — when the diagnosis blames the TEST,
   the coder made no code change *correctly*; reading that as stagnation would
   BLOCK a fixable case (the GH #44/#61 interaction, weather findCity).
3. `_diff_cycle_kind(db, microtask)` — deterministic loop forensics over the
   per-attempt `diff` artifacts (whitespace-normalized): last==previous →
   `stagnation`; last==two-ago (≠previous) → `revert_cycle`. ≥3 attempts
   required; best-effort (never raises).
4. `blocked` (LLM decision OR forensics) → `_handle_blocked`: microtask →
   BLOCKED (state + row), conflict reflection persisted as an artifact
   ("the dashboard's answer to *why did this stop*"), then
   `RunFailureHandler.handle` with reason
   `BLOCKED — requirements conflict on microtask X: <conflict>` — a readable
   verdict instead of "attempts exhausted 3/3".
5. Otherwise `_build_state_result` routes: `test` (+budget) → NEEDS_TEST with
   `test_rejection_note` = the fix instructions and `test_resynth_count + 1`;
   else → NEEDS_CODE (coder retry; microtask stays current either way).
- `_test_output`: rebuilds the full tester evidence from the **durable check
  artifacts** (via `evidence.checks.<label>.artifact_ref` → artifacts table) —
  restart-safe, no host-file side channel; includes every check that ran, pass
  or fail, so the diagnosis sees full context.

## deploy_node.py — `DeployRunner`

Not an LLM agent — deliberately BYPASSES the ToolGateway (docstring: "there is no
allowlist to enforce for a fixed deploy step"); drives GitOps directly, still
emits durable events. Dispatch on the run's checkpointed `deploy_mode`:

1. `DRY_RUN` → `_finish_dry_run`: compute the would-have-shipped fileset, emit
   `GITOPS_DEPLOY_SKIPPED`, ship nothing.
2. `_resolve_push_target`: branch = per-run `branch_to_push_to` › registry
   `push_branch_override` › `agent/run-<id>`; forbidden = the repo's default
   branch. A stale override that now equals the default branch **degrades** to
   the run branch (the work is finished — land it somewhere safe, don't throw it
   away). A directly-forbidden target → FAILED.
3. **Secret gate before anything ships**: `scan_secrets`; findings → 
   `GITOPS_SECRET_BLOCKED` + FAILED; a scan that couldn't run ALSO blocks (fail
   closed).
4. Commit message precedence in one expression: gate-entered
   (`deploy_commit_message`, the gate overwrote the seed) › built default
   (`build_commit_message` — the same function `Service` used to prefill the
   gate, ADR 0035).
5. `LOCAL_ONLY` → `_export_local`: bundle→fetch; `LocalExportError` → FAILED
   (no degrade — "pretending success while delivering nothing would be worse").
6. `PUSH`/`PR` → `push_branch`, then `_maybe_create_pr`: only for mode PR; empty
   fileset → skip ("No commits between base and head" is guaranteed);
   `PrCreationError` → **degrade** (pr_error carried in the `gitops.pushed`
   payload — the push already landed, ADR 0027). PR URL returned on
   `repo.model_copy(update={"pr_url": ...})` — state updated via the return
   dict, never mutated.

---

## The pattern to cite in the exam

"Runners are the impedance match between a checkpointed graph and plain agent
classes: the graph node stays a pure state→dict function, the Runner owns every
side effect (DB, events, budgets, caps), and the agent underneath knows nothing
about LangGraph. That's why we could swap the CLI for the API, and hand-rolled
snapshots for the checkpointer, without touching a single agent."
