# 0004 — PR #9 review follow-up: snapshot persistence deferrals

Status: accepted
Date: 2026-06-08

## Context
A high-effort `/code-review` of merged PR #9 (`5959b21...4598fa2` — bootstrap node,
`run_state` reconstruction, DB-backed sandbox registry) surfaced 10 ranked findings.
Seven were concrete, fixable bugs (DB-corrupting `str(Enum)` serialization, a
permanently-stuck-FAILED clone path, a Docker 409 crash on stale containers, a dropped
`Reviewer.role` attribute, gaps in the `EventType` registry, a schema-drift crash on
resume, and silently-discarded CLI args on resume) and were fixed directly — see the
commit that references this ADR.

The remaining three findings are architectural/performance observations about
`RunStateStore.save` (the new full-`BlackboardState` snapshot mechanism). Per the user:
this is POC1 — only the Bootstrapper and Planner are wired into the graph; Coder,
Tester, and the real Reviewer are stubs. Acting on these now would mean designing a
generalized mechanism around nodes that don't exist yet.

## Decision
Defer the following three observations until Coder/Tester/Reviewer are real agents and
actually call `RunStateStore.save`:

1. **Ad-hoc per-node snapshotting.** `RunStateStore.save` is currently called manually
   inside each result-producing `db.session_scope()` block — 3x in `BootStrapper.invoke`
   (`bootstrap_node.py`), 1x in `planner_node` (`orchestrator.py`). `coder_node` /
   `tester_node` / `plan_reviewer_node` are stubs and don't snapshot at all. Once they're
   real, every author must remember to wire this in — a single graph-level after-node
   snapshot wrapper (e.g. wrapping `builder.add_node` or post-processing `graph.invoke`
   results) would remove the "did the author remember" risk class entirely.
2. **Full-state re-serialization per snapshot.** `blackboard_state_to_dict` dumps every
   microtask/artifact/reflection on each call; `RunStateStore.save` does a full-row
   `UPDATE` of the JSON blob. For a long run with dozens of microtasks and retries, every
   subsequent snapshot re-serializes and rewrites the entire growing history — O(n²)
   total cost across a run. Worth revisiting once real runs accumulate enough microtasks
   for this to be measurable (delta-snapshotting, or splitting `state_json` from a
   separate append-only `microtasks`/`events` projection, are candidate fixes).
3. **`merge_state`'s shallow merge vs. LangGraph's reducers.** `merge_state` does
   `{**state, **partial}`, which diverges from LangGraph's own merge for any field with a
   custom reducer — currently only `messages` (`Annotated[List[AnyMessage], operator.add]`).
   No node returns a partial `messages` update today, so this is latent. The moment one
   does, the "early view" `merge_state` produces (and persists to `run_state`) would
   differ from what LangGraph actually applies, reconstructing a state that never existed.

## Alternatives considered
- **Generalize the snapshot mechanism now.** Rejected — there's nothing to generalize
  against yet; a wrapper designed around 2 nodes (Bootstrapper, Planner) risks being
  wrong for the shapes Coder/Tester/Reviewer actually need (e.g. snapshotting mid-loop on
  the execution-verification cycle vs. once per node).
- **Switch to delta/event-sourced persistence instead of full snapshots.** Rejected for
  now — `run_state` as a full-snapshot table is the documented mechanism
  (`ARCHITECTURE_preview.md` §3.2); changing the storage model is a bigger redesign than
  this review's scope warrants, and the O(n²) cost isn't measurable yet at POC scale.

## Consequences
- Snapshotting remains a manual, per-node concern through POC1 — acceptable because only
  2 of 5 graph nodes produce meaningful state changes right now, and both already call
  `RunStateStore.save`.
- When Coder/Tester/Reviewer land (CLAUDE.md TODO #4, "wire `planner → plan_reviewer →
  coder → tester`"), this ADR is the trigger to revisit #1 (generalize snapshotting) and
  re-measure #2 (snapshot cost) before they ship — don't let three more ad-hoc call sites
  get copy-pasted in.
- #3 stays a documented landmine; if a future node design needs to return partial
  `messages`, `merge_state` must be fixed first (or the field excluded from its merge).
