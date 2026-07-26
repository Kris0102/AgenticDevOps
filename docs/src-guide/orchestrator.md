# src/orchestrator/ — The MAS Control Plane

**Role in the MAS:** the orchestrator is the *only* coordinator. Every agent-to-agent
interaction is mediated here (star topology): agents never call each other, they read
and return pieces of a shared **blackboard** state, and the orchestrator's routers
decide who runs next. This is the folder to understand first.

## Core ideas

1. **Blackboard pattern** (`state.py`): a single Pydantic `BlackboardState` is the
   shared memory of the whole system. Agents never mutate it in place — each node
   returns a dict of *changed keys only*, and LangGraph merges it. One owner, no
   direct agent-to-agent messages, full auditability.
2. **Graph, not choreography** (`orchestrator.py`): the workflow is an explicit
   LangGraph `StateGraph`. Routing is data-driven — routers inspect the blackboard's
   status field (e.g. `NEEDS_CODE`, `REFLECT`, `READY_TO_DEPLOY`) and pick the next
   node. Routers are pure: **no side effects in routers**, all DB writes happen in
   nodes.
3. **Durable execution** (`checkpointer.py`): LangGraph's `PostgresSaver` snapshots
   the blackboard after every superstep, keyed by `thread_id = run_id`. A crash or a
   human pause resumes from the checkpoint — no hand-rolled state table. Backed by a
   psycopg `ConnectionPool` (not one shared connection) so concurrent runs and status
   polls each borrow their own connection (ADR 0032).

## Graph topology

```
START → planner → plan_reviewer → (planner | coder | hitl)         # planning loop
        coder → tester → (reflector | hitl | coder | deploy)       # execution loop
        tester → reflector → (coder | tester)                      # failure diagnosis
        tester → deploy → hitl → deploy_push → END                 # terminal deploy
        hitl → (planner | coder | deploy | deploy_push | END)
```

- **Planning loop (actor–critic):** Planner drafts a plan of microtasks, the Plan
  Reviewer independently investigates and approves/rejects. Rejection loops back with
  structured blockers; the loop is capped (`max_planner_reviewer_iterations`) and the
  cap is enforced *in the node*, not the router.
- **Execution loop:** Coder implements one microtask → Tester verifies with
  deterministic checks. The **Tester owns the verdict**. Pass → next microtask or
  deploy. Fail → the **failure-only Reflector** diagnoses root cause and routes the
  retry: `fix_target=code` → coder, `fix_target=test` → tester re-synthesizes a wrong
  test (bounded by `REFLECTOR_MAX_TEST_RESYNTH`). No LLM call on a green pass, no
  blind retries.
- **Deploy (terminal):** on the final pass the run always funnels through the deploy
  gate so a human sees the final diff, then `deploy_push` dispatches on the run's
  `DeployMode`: `dry_run` / `push` / `pr` / `local_only` (ADR 0036).

## Key files

| File | What it does |
|---|---|
| `orchestrator.py` | Builds the graph: registers nodes, wires edges, routing functions gated by per-edge expected-status allowlists |
| `state.py` | `BlackboardState` + every shared model (Plan, Microtask, Reflection, Constraints, Iteration, HITL, Phase enum…). Single source of truth for types |
| `bootstrap_node.py` | `BootStrapper` — runs *before* the graph on every invocation (ADR 0005): upserts the Run row, ensures the sandbox container is running, clones the repo on first run, ingests the semantic baseline |
| `planner_node.py`, `reviewer_node.py`, `coder_node.py`, `tester_node.py`, `reflector_node.py`, `deploy_node.py` | One node-runner per agent: thin wrappers that resolve config, call the agent's `invoke()`, persist status/events, and translate the result into a node-return dict |
| `hitl.py` | The shared HITL gate: `interrupt()` pauses the graph when a phase is in `hitl_checkpoints` (PLAN / CODE / DEPLOY). Approve routes forward; reject requires a note and routes it into the right feedback channel (plan → review_feedback, code → a seeded `Reflection`) |
| `checkpointer.py` | `CheckpointerFactory` — PostgresSaver over a psycopg connection pool |
| `token_budget.py` | Run-wide token budget (ADR 0025): checked at the top of every node's `invoke()`; exceeding it raises `TokenBudgetExceededError` → run STALLS (recoverable, human can add budget) instead of failing |
| `run_control.py` | Cooperative stop registry (ADR 0033): every node checks `is_stop_requested(run_id)` first, so a human Stop bounds the run to at most one in-flight node |
| `run_failure.py` | `RunFailureHandler` — centralized FAILED bookkeeping (status + audit events), caller owns the commit |

## Human-in-the-loop (HITL)

Three gates (`plan`, `code`, `deploy`), configured per run. Implementation is
LangGraph-native `interrupt()` — the graph suspends mid-superstep with the sandbox
alive and the checkpoint intact; resuming is `Command(resume=...)` via the API, one
`graph.invoke()` per gate. Reject always requires a human note, and the note is fed
back into the same channels the agents already use (no special-case paths).

## Safety rails that live here

- Per-microtask attempt cap (kills runaway coder↔reflector loops).
- Planner↔reviewer iteration cap (kills planning ping-pong).
- Run-wide token budget → STALLED, not FAILED (human decides: add tokens or close).
- Stop-request checkpoint at the top of every node.
- Uncaught graph crash → STALLED (continuable), never silently FAILED.

## Interview sound bites

- "Agents are functions over a blackboard; the orchestrator is the only thing with
  control flow."
- "We separated *decision* (routers, pure) from *effect* (nodes, DB writes) — that's
  what makes the graph replayable and checkpointable."
- "Failure handling is first-class: a failed test doesn't retry blindly, it goes
  through a Reflector that decides whether the code or the test is wrong."
