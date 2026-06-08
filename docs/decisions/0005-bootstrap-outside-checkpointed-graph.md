# 0005 — Migrate to LangGraph PostgresSaver checkpointer; run Bootstrap outside the graph

Status: accepted
Date: 2026-06-08

## Context
Issue #11 proposed replacing the custom `RunStateStore`/`merge_state`/
`blackboard_state_to_dict` snapshot mechanism with LangGraph's native
checkpointer (`PostgresSaver`), to resolve the three deferrals recorded in
[0004](0004-pr9-review-followup-deferrals.md): ad-hoc per-node snapshotting,
O(n²) re-serialization cost, and `merge_state`'s blindness to LangGraph's own
`operator.add` reducer on `messages`.

While validating that proposal, a direct conflict surfaced with the documented
decision in `CLAUDE.md`: *"Bootstrap always first: `bootstrapper_node` runs
unconditionally on every invocation... no side effects in routers."*

LangGraph's checkpoint-resume does **not** restart a thread from `START`.
Invoking `graph.invoke(None, config={"configurable": {"thread_id": run_id}})`
against an existing thread loads the last checkpoint and continues from
whatever node is recorded as "next" — completed nodes never re-run. If
`bootstrapper_node` were a graph node, a resumed run whose checkpoint says
"next: coder" would simply skip it. But `BootStrapper` isn't a one-time setup
step — it's an **idempotent environment-readiness check**: is the sandbox
container still running? did the previous clone attempt succeed? It must
re-verify on every invocation, resumes included, or coder/tester run against a
dead sandbox or missing repo.

## Decision
Split the two concerns across the two mechanisms that actually fit them:

- **`BootStrapper.invoke()`** (unchanged internals: `_upsert_run_record`,
  `_ensure_sandbox_running`, `_clone_repository`) is called **directly by
  `RunLauncher`** (`main.py`), before every `graph.invoke()` — fresh runs and
  resumes alike. It is no longer wrapped as a graph node; `bootstrapper_node`
  and `phase_router` are deleted from `orchestrator.py`.
- The graph itself becomes `START → planner → plan_reviewer → coder → tester
  → (reflect | END)`, compiled with `checkpointer=PostgresSaver(...)`
  (`orchestrator/checkpointer.py`), keyed by `thread_id = run_id`. A fresh run
  starts at `planner`; a resumed run continues exactly where the last
  checkpoint left off.
- `RunStateStore`, `merge_state`, `blackboard_state_to_dict`/`_from_dict`, the
  `RunState` ORM model, and the `run_state` table are all deleted — replaced
  by the checkpointer's automatic per-superstep snapshotting into
  `checkpoints`/`checkpoint_blobs`/`checkpoint_writes`/`checkpoint_migrations`
  (created by `PostgresSaver.setup()`, called idempotently from `RunLauncher`
  on every startup — confirmed no name collision with `runs`/`events`/
  `sandboxes`/`artifacts`/`microtasks`).

This resolves the conflict rather than working around it: "bootstrap always
first" stays literally true — bootstrap now runs first, full stop, completely
outside the checkpointed graph's resume semantics. No part of `CLAUDE.md`'s
decision needs to be superseded; this ADR documents the split, not a reversal.

## Alternatives considered
1. **Embrace native resume as-is** — keep `bootstrapper_node` in the graph,
   accept it gets skipped on resume, write an ADR superseding "bootstrap
   always first." Rejected: throws away the self-healing safety net (stale
   container detection, clone-retry-on-resume) that exists precisely because
   sandboxes and clones can fail or disappear between invocations.
2. **Force restart-from-START** — mint a new `thread_id` per CLI invocation so
   the graph always begins at `bootstrapper`. Rejected: then you need a
   separate mechanism to find "the last known state for run X" across
   `thread_id`s for the CLI override-gating logic — reintroducing a
   `RunStateStore`-shaped problem and defeating the migration's purpose.
3. **Don't migrate** — keep `RunStateStore`, just fix `merge_state`'s reducer
   blindness. Rejected: leaves the O(n²) re-serialization cost and ad-hoc
   per-node snapshotting (deferral #1 and #2 from ADR 0004) unaddressed, and
   keeps ~150 lines of custom persistence code that LangGraph replaces natively.

## Consequences
- **Easier**: snapshotting is now automatic and reducer-correct for every
  current and future graph node — Coder/Tester/Reflector get it for free, no
  per-node `RunStateStore.save` call to remember (closes ADR 0004 #1 and #2).
  Resumes no longer re-run completed multi-step agent work (e.g. a finished
  planning phase isn't redone after a crash mid-coding).
- **Harder**: `BootStrapper`'s result is no longer threaded into the
  blackboard automatically by the graph — `RunLauncher` must explicitly merge
  it into the seed state (fresh runs) or push it via `graph.update_state`
  (resumes), being careful to exclude `operator.add`-reduced fields like
  `messages` from that update (the exact class of bug `merge_state` was blind
  to — now avoided by construction, not by convention).
- **New infrastructure dependency**: `langgraph-checkpoint-postgres` +  a raw
  `psycopg` connection (autocommit, dict_row) held separately from
  SQLAlchemy's pool — one more thing to keep healthy, but it's
  framework-maintained rather than hand-rolled.
