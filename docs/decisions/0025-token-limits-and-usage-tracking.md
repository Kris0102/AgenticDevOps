# 0025 — Token usage tracking, a global per-run token budget, and dashboard surfacing

Status: accepted
Date: 2026-07-07

## Context
There was **no token accounting anywhere** in the codebase (zero uses of LangChain's
`usage_metadata`). The user needs to cap total token spend per run, see actual consumption
live, and recover a run that hits the cap instead of losing it.

A cap needs a verdict on exhaustion. Failing the run outright (like `max_total_iterations` /
the microtask attempt caps) doesn't fit: those caps mean the run is genuinely stuck, but a
token budget is a cost-control policy knob — the run could be one microtask from DONE when it
trips. The codebase already has the right mechanism: a **STALLED** run (non-terminal,
checkpoint intact, sandbox alive), recoverable via `POST /runs/{id}/continue` or closed via
`POST /runs/{id}/fail` — the same machinery used for transient LLM errors (ADR 0023) and
uncaught crashes.

The backend piece (capture + enforcement + stall) shipped first, deliberately without any
dashboard change. Dashboard work followed in two more steps: first plain visibility + the
ability to raise the cap from a stalled run, then, once `llm.usage` events existed, breaking
that consumption down per agent and per microtask instead of only as one run-wide total.

## Decision
1. **Capture.** `providers/token_usage.py::TokenUsage`/`sum_usage(messages)` sums
   `AIMessage.usage_metadata` across a ReAct trajectory or a structured call; every agent
   (Planner, Reviewer, Coder, Tester, Reflector) tracks its usage per invocation. The three
   `.with_structured_output(...)` call sites (Planner synthesis, Reviewer review, Reflector
   assess) switched to `include_raw=True` so `usage_metadata` stays reachable, with
   `parsing_error` re-raised explicitly to preserve prior error handling.
2. **`record_usage()`** (`orchestrator/token_budget.py`) is the single place, called once per
   node, that: writes usage into the agent's per-iteration debug file, logs `llm.usage`,
   emits durable `EventType.LLM_USAGE_RECORDED` (carries `microtask_id` for Coder/Tester/
   Reflector, `None` for the Planner, which runs before any microtask exists), accumulates
   `Iteration.tokens_used_total`, and mirrors the new total onto `runs.tokens_used_total`
   (same precedent as the `llm_provider`/`llm_model` denormalized mirrors).
3. **Enforcement.** `Constraints.max_total_tokens` (own field — not a reuse of the dead,
   dollar-denominated `cost_budget_usd`) is checked by `token_budget_exceeded()` at the top of
   every agent node's `invoke()`, mirroring the existing `max_total_iterations` guard. No
   partial degrade — the next node simply refuses its LLM call once the total is reached.
4. **Exceeding it stalls, not fails.** Each Runner raises `TokenBudgetExceededError`
   (deliberately not a `RunExecutionError`, so `RunFailureHandler` never sees it); it escapes
   `graph.invoke()` to `Service._drive`, which calls `_mark_stalled(..., kind="token_budget")`
   — `Run.status` is untouched, so the run reports STALLED, recoverable exactly like a
   transient stall.
5. **Dashboard visibility.** `runs.max_total_tokens` denormalized column;
   `RunResponseWithDetails` also carries `tokens_used_total`; `RunResponse.stalled_kind`
   (`'token_budget'` vs `'transient'`, read back from the `RUN_STALLED` event) lets the UI
   show the raise-cap input only for a token-budget stall. `RunOverview`'s "tokens used/cap"
   tile is computed client-side from the already-polled `llm.usage` events, not from
   `info.tokens_used_total` — `info` is fetched once per run and would otherwise freeze for
   the life of a running run.
6. **Raising the cap.** `POST /runs/{id}/continue` takes `ContinueRunRequest.max_total_tokens`;
   `Service.raise_token_budget` patches the checkpointed `Constraints` via
   `graph.update_state`, rejects with 422 (`InvalidTokenBudgetError`) if the new cap is `<=`
   tokens already spent, mirrors it onto `runs.max_total_tokens`, and emits
   `run.token_budget_raised` (old/new cap, tokens used) — all synchronously in the route,
   before the background re-drive. **The new cap is mandatory, not optional, for a
   token-budget stall**: continuing one unchanged just re-trips the same check immediately, so
   `/continue` 422s without it, and `App.tsx` mirrors this client-side (`capRequired`/
   `capValid`), disabling "Continue" until a positive value is entered. Any other stall kind
   keeps blank = plain retry.
7. **Settings/New Run.** Fixed a pre-existing bug where clearing a nullable numeric Settings
   field produced `NaN` instead of `null` (prerequisite for "no cap" to actually work). New
   Run passes `max_total_tokens` through silently, the same pattern already used for
   `max_total_iterations`/`max_microtask_attempts`.
8. **Per-agent / per-microtask breakdown.** `TokensByAgent` panel: a bar chart of `llm.usage`
   totals grouped by `payload.agent`, fixed agent order, always shown (even at zero).
   `MicrotasksPanel` (new "microtasks" tab): sums Coder+Tester+Reflector `llm.usage` per
   `microtask_id` next to each microtask's full planner record (Planner tokens excluded here —
   not attributable to one microtask). `RunOverview` also renders a "token cap at start" line
   plus one line per `run.token_budget_raised` event, and refetches its run info when a
   `capVersion` prop bumps after a raise, so the tile's denominator updates immediately.
   `EventTimeline`'s tool-call toggle no longer hides `llm.usage` (previously folded in) — now
   that summarized views exist elsewhere, hiding the raw per-call trail by default stopped
   pulling its weight as a noise filter.

## Alternatives considered
- Reuse `Constraints.cost_budget_usd` for the budget — dollar-denominated, dead code, wrong
  unit for this.
- Enforce the cap only in the Reviewer, mirroring where `max_total_iterations` lives — misses
  Coder/Tester/Reflector-heavy runs that never revisit the Reviewer.
- Fail the run outright on exhaustion — a token cap is a cost-control knob, not evidence the
  run is stuck; FAILING would force discarding a possibly near-complete checkpoint.
- A dedicated `RunStatus` value for "token-capped" — STALLED is already the right derived
  state and keeps `/continue` working as "just re-run the node".
- A dedicated per-call `token_usage` table — premature; the existing `llm.usage` events
  already give per-call (and now per-agent/per-microtask) granularity.
- Reading `tokens_used_total` off `RunResponseWithDetails` for the tile instead of summing
  events — that model is fetched once per run and would go stale while a run is live.
- A live status-bar token counter and a runs-list badge — declined by the user; cheap to add
  later since the backend fields already exist.
- A visible per-run cap override field on the New Run page — inconsistent with the existing
  silent-passthrough pattern used for the other two constraints.
- A dedicated `PATCH /runs/{id}/constraints` endpoint for raising the cap — no scenario raises
  the budget without also continuing, so it'd just be an extra round trip.
- A soft warning instead of a hard 422 on a too-low new cap, and leaving the raise-cap input
  optional for a token-budget stall — both let a human think they'd fixed it and immediately
  hit the identical stall again.
- Keeping `llm.usage` folded into the tool-call toggle once per-agent/per-microtask summaries
  existed — reconsidered and reverted; no longer needed as a noise filter.

## Consequences
Run-wide cost control now exists end to end: capture → enforcement → recoverable stall →
dashboard visibility → raise-cap-and-continue → per-agent/per-microtask attribution, entirely
via patterns already used elsewhere (settings-hub knobs, `Iteration` running counters,
denormalized `runs` columns, `model_copy` partial updates, the STALLED/`_drive` machinery) —
no new architectural concepts. The `include_raw=True` switch is a small, contained risk: a
parsing failure now flows through `raw["parsing_error"]` instead of a raised exception,
re-raised explicitly to keep existing error handling behaving the same.

Known gaps: no live status-bar counter or runs-list badge yet (backend fields already support
adding them). Runs stalled before this shipped have no `kind` on their stall event, so their
stalled-bar falls back to a plain "Continue run" no-op. `RunOverview`'s tile only refreshes
its cap on an explicit raise (`capVersion` bump), not on every poll.

## Amendment (2026-07-08): thousands-based input, incremental raise, M-suffix display
Three usability changes to the pieces above, all UI/API-contract only — no change to how the
cap is enforced (`token_budget_exceeded()`, §Decision 3) or captured (§Decision 1-2).

1. **Settings input is now in thousands.** `defaults.max_total_tokens` is still stored and
   enforced as a raw token count everywhere in the backend/DB (unchanged), but the Settings
   field now reads/writes it scaled by 1,000: entering `200` means 200,000 tokens. Carried by
   a new generic `scale` property on the schema-driven field descriptor
   (`SETTINGS_SCHEMA` entry → `SchemaField.scale` → `SettingsPage.tsx`'s `renderField`),
   rather than hardcoding the one field — any future thousands-scaled numeric setting can
   reuse it. `renderField` also now rejects negative/non-numeric input inline (a
   `fieldErrors` map + a `.fld-error` hint next to the input) instead of silently coercing to
   `NaN`/a stale value.
2. **Raising the cap on a token-budget stall is now an INCREMENT, not a new absolute cap.**
   `ContinueRunRequest.max_total_tokens` → `additional_tokens`: `/continue` now ADDS the given
   amount to `Constraints.max_total_tokens` (`Service.raise_token_budget`) instead of
   replacing it. A run capped at 200k that stalls and gets `200` (→ 200,000) entered again
   ends up with a 400k cap, not a 200k one. Rationale: the old absolute-cap input required the
   human to remember (or go look up) the current cap and add to it mentally every time; an
   increment is what the human actually means ("give it 200k more to work with") and removes a
   footgun where entering the same number twice looked like a no-op cap-bump instead of
   doubling it. Also entered in thousands, matching the Settings field's convention, and
   validated the same way (positive-number check, inline error, `App.tsx`'s `addTokensValid`)
   instead of the old bare `capValid` disable-with-no-explanation. The audit event
   (`run.token_budget_raised`) keeps its `old_max_total_tokens`/`new_max_total_tokens` payload
   shape (dashboard-compatible) and gains an `additional_tokens` field.
3. **Display formatting gains an M suffix above 1,000,000.** `fmtTokens` (duplicated in
   `RunOverview`, `TokensByAgent`, `MicrotasksPanel` — unchanged duplication, not
   consolidated) now renders `n >= 1_000_000` as e.g. `1.0M` instead of `1000.0k`; everything
   below that threshold is unchanged (`200.0k` still reads as `200.0k`). Purely a display
   change — no stored value crosses the M/k boundary differently.

Alternatives considered: converting the whole backend (DB column, `Constraints`,
`RunResponse`) to thousands units — rejected, raw tokens are the correct unit for every
non-human consumer (enforcement, usage summing, events) and scaling only at the two UI entry
points keeps the blast radius to the settings form and the continue-bar input. Keeping the
absolute-cap semantic for `/continue` and just showing the current cap next to the input for
context — rejected in favor of the simpler increment, since showing "current cap" would have
required threading `max_total_tokens` onto the base `RunResponse` (today only on
`RunResponseWithDetails`) for no benefit once the input itself no longer needs that number.
