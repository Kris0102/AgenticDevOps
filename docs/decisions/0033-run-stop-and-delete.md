# 0033 — Dashboard: manual run Stop and Delete

Status: accepted
Date: 2026-07-13

## Context

The dashboard had no way to end a run early or remove one from history. Requirements:

- **Delete** a run permanently — only from the run detail page, not the overview list.
- **Stop** a running run early, landing it in a new terminal state, gray, labeled
  "manually stopped" (distinct from `failed`).
- **A stop must be clean** — the run must actually stop executing, not keep working in
  the background while the UI claims otherwise.
- **A running/paused run cannot be deleted directly** — must be stopped first; delete
  must never race a live driver thread.

`give_up`/`POST /runs/{id}/fail` (closing out a STALLED run) is the structural precedent
both features mirror: DB-row-wins-over-checkpoint, `window.confirm`/`window.prompt` (no
modal component exists here).

## Decision

### Stop: cooperative per-node checkpoint, not a sandbox kill

`graph.invoke()` runs synchronously in a background thread with no built-in cancellation
point, and Python has no safe way to force-kill a live thread — a first draft tried to
work around that by force-destroying the run's Docker sandbox, assuming the resulting
exception would propagate up and unwind the thread. **That didn't hold up in testing**
(see Alternatives) — tool-call failures are caught deep inside `ToolGateway`/the tool
wrappers and fed back to the LLM as a normal observation instead of raising, and a lot of
Planner/Reviewer investigation runs via `semantic_search` (pgvector), which never touches
the sandbox at all. The actual mechanism:

- **`orchestrator/run_control.py`** — a lock-guarded `set[str]` of stop-requested
  `run_id`s: `request_stop` / `is_stop_requested` / `clear_stop`.
- **`OrchestratorGraph._check_stop(state)`** is the first line of every node wrapper that
  does real work (`planner`, `plan_reviewer`, `coder`, `tester`, `reflector`, `deploy`,
  `deploy_push` — not `hitl`, already a suspend point). If the run's `run_id` is flagged,
  it raises `RunStoppedError` before the node does anything.
- **`Service.stop_run(run_id, note=None)`**: calls `request_stop()` first, then still
  force-destroys the sandbox (secondary now — real cleanup, and still breaks whatever
  sandbox call genuinely is in flight), then **blocks until `is_active(run_id)` is
  False** before writing `Run.status = "STOPPED"` — never releases the claim early, or a
  second driver could start on the same `run_id` while the old thread is still mutating
  the checkpoint (ADR 0032). Bounded by `_STOP_WAIT_TIMEOUT_SECONDS = 180`; past that it
  raises `RunStopTimeoutError` (→ HTTP 503, safe to retry) instead of lying about the
  outcome. Once confirmed inactive: writes the terminal status, logs `run.stopped`
  (reason), retracts the run's lessons (mirrors `give_up`, ADR 0018), purges its semantic
  workspace index, clears the stop flag.
- `_drive` gets an `except RunStoppedError` branch ahead of the generic catch-all: logs
  and returns without a misleading `run.stalled` event (`stop_run`'s own write is the
  authoritative record).
- Kept synchronous (mirrors `/fail`) rather than backgrounded with a transitional
  "stopping…" `RunState` — the frontend just shows a busy/disabled button meanwhile.

**Bound this gives you:** a Stop request interrupts at most one more in-flight node —
the node already executing still runs to completion (bounded by its own
`*_max_tool_steps`/`*_timeout_seconds`), but the next node transition, which used to sail
straight through, now aborts immediately. Any *new* node added to the graph must call
`_check_stop` itself — there's no structural enforcement (decorator/registration hook),
just a convention note in `CLAUDE.md`'s "Adding a new agent".

`STOPPED` is a raw `Run.status` string (`STOPPED_STATUS = "STOPPED"`), not a `RunStatus`
blackboard-enum member — that enum is deeply threaded through node routing, and a stopped
run never re-enters the graph, so adding a dead-end routable value there is pure risk for
no benefit. `RunState.STOPPED = "stopped"` (the API-facing lifecycle enum) is what the
dashboard consumes; `_list_state`/`_response` both special-case the DB string (the latter
must check it against both `STALLED` *and* `PAUSED_HITL` — a run stopped while paused at
a HITL gate still has its checkpoint's pending interrupt, so `_derive_state` reports
`PAUSED_HITL` first). `RunResponse.stopped_reason` mirrors `failure_reason`.

New exceptions `RunAlreadyTerminalError`/`RunStopTimeoutError`/`RunNotFoundError`
(→ 409/503/404) and `RunStoppedError` (internal-only, caught in `_drive`).

### Delete: refuses a live run, cascades per-run data, keeps cross-run memory

`Service.delete_run(run_id)`: refuses `RUNNING`/`PAUSED_HITL` (409 — stop it first),
force-tears-down the sandbox, deletes the run's workspace `code_chunks`
(`SemanticStore.delete_workspace`) and its LangGraph checkpoint thread
(`checkpointer.delete_thread`), then manually deletes its `events`/`artifacts`/
`microtasks`/`sandboxes` rows (none of these tables have an FK/cascade). **Lessons are
deliberately kept** — they're cross-run memory (ADR 0018) meant to outlive the run that
produced them; only a run *failure* retracts lessons (a trust judgment made at write
time), which is orthogonal to deleting a run's own audit history.

### Surfacing (dashboard)

`RunState`/`api.ts` gain `'stopped'` + `stopped_reason`; `App.tsx` gets Stop (shown for
`running`/`paused_hitl`) and Delete (shown once terminal) buttons plus a gray banner,
following the existing `onGiveUp`/`ReposPage.remove()` confirm-prompt idiom; `RunsHome`
labels it "manually stopped" (no stop/delete button there, list stays uncluttered);
`StatusBar` overrides its auto-derived badge text the same way; `PipelineGraph` adds an
isolated `stop` SVG node (gray, `--cat-stop`) with **no edges** — a manual stop can
interrupt from any node, so wiring convergent edges from all of them would be clutter;
the node's own highlight (driven off `run.stopped` in the event trail, and
`run_state === 'stopped'` suppressing the normal pulsing-active node) is the "you are
here" signal, same treatment `failed` already gets.

## Alternatives considered

- **Sandbox-kill as the sole stop mechanism**, relying on the resulting exception to
  propagate out of the node. Rejected after manual testing showed the run continuing
  through several more full plan/review cycles regardless — `ToolGateway.execute_tool`
  catches every sandbox exception and returns `{"error": ...}`, the Planner/Reviewer tool
  wrappers hand that back to the LLM as a normal observation instead of raising, and much
  of investigation (`semantic_search`) never touches the sandbox at all. Making tool
  exceptions propagate instead was also considered and rejected: that swallowing is
  deliberate agent-robustness behavior unrelated to stopping a run, and it likely
  wouldn't even work — `langchain.agents.create_agent`'s underlying `ToolNode` has its
  own default tool-error handling that converts a raised exception back into a
  `ToolMessage` before it reaches the caller.
- **Checking the stop flag inside `ToolGateway.execute_tool`** for faster mid-loop
  interruption (vs. only at node entry). Rejected for now — same `ToolNode` swallowing
  problem, so it likely wouldn't reliably buy anything the node-entry check doesn't
  already give; a single node's own tool-loop is already bounded by its step/timeout caps.
- **Force `mark_inactive` after a short fixed wait**, regardless of whether the thread
  had actually exited. Rejected — would let a second driver start on the same `run_id`
  while the zombie thread might still be writing to the same checkpoint/DB row.
- **Adding `STOPPED` to the `RunStatus` blackboard enum.** Rejected — pure routing risk
  for a value that's never actually routed on.
- **Cascading Lesson deletion on run delete.** Rejected — lessons are cross-run memory;
  deleting a run's history is orthogonal to whether its lessons were already judged
  trustworthy at write time.
- **Backgrounding `POST /runs/{id}/stop`** (202 + poll a transitional state). Rejected —
  unnecessary complexity for what's a sub-second call in the common case; mirrors `/fail`.
- **Drawing edges from every node into `stop`.** Rejected as visual clutter for a rare
  action.

## Consequences

- A Stop request bounds the run to at most one more in-flight node, not an unbounded
  number of further cycles — but can still block the HTTP call up to
  `_STOP_WAIT_TIMEOUT_SECONDS` (180s) worst case while that one node finishes.
- A new gray terminal state (`stopped`) exists across every status surface — future
  features enumerating run states must account for it alongside
  `completed`/`failed`/`stalled`.
- Deleting a run permanently loses its audit trail (events/artifacts/microtasks/
  sandboxes/workspace index/checkpoint) — no undo. Lessons persist independently.
- A running/paused run must go through Stop before Delete is available, both in the UI
  and the API (409) — an intentional two-step flow.
- Any future graph node must remember to call `_check_stop` itself; nothing enforces it.
