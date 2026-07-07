# 0026 — Dashboard surfacing of token limits and usage (ADR 0025 follow-up)

Status: accepted
Date: 2026-07-07

## Context

ADR 0025 shipped per-agent token usage capture, an enforced global per-run token budget, and
made exceeding that budget stall the run (STALLED, recoverable) rather than fail it —
explicitly **backend-only**: "the dashboard cannot yet show any of this." Until this change,
the only way to see token usage was the per-microtask debug files, the live log feed, a raw
`events` query, or `SELECT tokens_used_total FROM runs`; the only way to recover from a
token-budget stall was a no-op retry (`/continue` unchanged) or giving up (`/fail`) — there
was no way to actually raise the cap.

Scoped down from the full surface area by explicit user choice: a runs-list badge
(`RunsHome`) and a live-updating counter in the `StatusBar` were both declined; a per-run
override *form field* for the token cap on the New Run page was declined in favor of
following the existing silent-passthrough pattern already used for
`max_total_iterations`/`max_microtask_attempts` (the value is already a real,
already-settable field on `RunRequest.constraints` — only the dashboard never populated it).

A later request completed the loop: the user wanted to be able to type a new (higher) cap
into the dashboard UI and have "Continue run" actually pick it up, rather than only choosing
between a no-op retry and giving up. Two gaps had to close for that: the frontend had no way
to tell a token-budget stall apart from a generic one (the `RUN_STALLED` event already
carries a `kind` field — `"token_budget"` vs `"transient"`, set by `Service._mark_stalled`,
ADR 0025 — but nothing read it back), and `POST /runs/{id}/continue` took no body at all.

## Decision

**Backend — denormalize the run's cap, mirroring the existing `llm_provider`/`llm_model`
pattern:**
- Migration `b3c4d5e6f7a8` adds `runs.max_total_tokens` (nullable int), chained off
  ADR 0025's `a2b3c4d5e6f7`.
- `bootstrap_node.py::_upsert_run_record` sets it from `Constraints.max_total_tokens` at
  run creation, same site/pattern as `llm_provider`/`llm_model`.
- `RunResponseWithDetails` (and `Service._run_details`) now also carries
  `tokens_used_total` (the existing ADR 0025 column) — both fields flow through the
  already-in-scope `Run` ORM row, no new query.
- `RunResponse` (the lighter, checkpoint-derived model used by `StatusBar`/
  `useRunStatus`'s 1.5s poll) is deliberately **not** touched — no live top-bar counter
  was wanted, so there's no reason to add a DB read to that hot poll path.

**Frontend — `RunOverview` gets a "tokens used" tile, computed client-side from the
already-polled `llm.usage` events** (`total_tokens` summed per run), not from
`info.tokens_used_total`. `RunOverview`'s `info` (`RunResponseWithDetails`) is fetched
**once** per `runId`, not polled — reading the total from there would freeze the number
for the life of a running run. `events`, by contrast, are already re-polled every 3s
while live, so summing `llm.usage` there gives a live-updating total for free, with
identical output for historical runs (events are durable) and no new polling path.
`info.max_total_tokens` (new field, static for the run's lifetime) supplies the "/ cap"
half of the tile when a budget was set.

**`llm.usage` events get a label/summary** (`eventInfo.ts`: `"agent · N tokens (model)"`)
and are **folded into the existing tool-call toggle** in `EventTimeline.tsx` rather than
given a new toggle of their own — they fire once per agent LLM call, comparable volume/
noise to `tool.executed`, and reusing the toggle avoids a second checkbox for what is
functionally the same "hide the noisy stuff" gesture.

**Settings page bug fix, prerequisite for `max_total_tokens` to actually be usable:** the
generic numeric-field renderer in `SettingsPage.tsx` produced `NaN` (not `null`) when a
nullable-int field was cleared, silently breaking the "blank = no cap" UX the field's own
schema description promises. Fixed generically (any `SchemaField` with `nullable: true`),
not with a token-specific special case.

**New Run page:** `max_total_tokens` passed through silently from
`settings.defaults.max_total_tokens` into `buildRunRequest`, identical to how
`maxTotalIterations`/`maxMicrotaskAttempts` already work — no new visible form control.

**Surface why a run stalled (`stalled_kind`).** `Service._last_event_reason` (payload
`.get("reason")` only) is generalized into `_last_event_payload` (returns the full payload
dict). `Service._response` now reads both `reason` and `kind` off the same query and
populates a new `RunResponse.stalled_kind: Optional[str]` field alongside the existing
`stalled_reason`. `dashboard/src/api.ts`'s `RunResponse` mirrors this — it's what lets the
dashboard show the raise-cap input only for a token-budget stall, not a generic one.

**`POST /runs/{id}/continue` accepts an optional new cap.** A new typed body,
`ContinueRunRequest` (`src/api/requests/requests.py`, same "validated Pydantic model"
convention as `RunRequest`/`HitlResume` — not the loose-dict style `fail_run` uses):
`max_total_tokens: Optional[int]`, validated positive when set. A new
`Service.raise_token_budget(run_id, new_max_total_tokens)`:
- reads the current checkpoint's `Constraints` via `graph.get_state(...)`,
- rejects (`InvalidTokenBudgetError`) if there's no checkpointed state, or if the new cap
  is `<=` tokens already spent (`Iteration.tokens_used_total`) — that would just
  immediately re-trip `token_budget_exceeded` again,
- patches it with the established `model_copy(update={...})` idiom (same pattern as
  `orchestrator/token_budget.py::record_usage`) and writes it back via
  `graph.update_state(config, {"constraints": patched})`,
- mirrors the new cap onto the denormalized `runs.max_total_tokens` column (same precedent
  as `bootstrap_node._upsert_run_record`), and
- emits a new durable audit event, `EventType.RUN_TOKEN_BUDGET_RAISED`
  ("run.token_budget_raised") — old cap, new cap, tokens used at the time — consistent
  with this codebase's "every meaningful mutation gets an event" convention.

This runs **synchronously in the `app.py` route**, before
`background.add_task(service.continue_run, ...)` is scheduled — a bad value 422s the HTTP
request immediately instead of silently re-stalling inside the background task where nothing
would surface the failure to the caller. `Service.continue_run` itself is unchanged: the
checkpoint is already patched by the time it runs `graph.invoke(None, ...)`.

**Dashboard UI.** `App.tsx`'s stalled-bar gets a small nullable-number input (`.cb-cap`,
mirrors the `SettingsPage.tsx` nullable-numeric convention: blank = no override), shown only
when `run.stalled_kind === 'token_budget'`. `api.continueRun(id, maxTotalTokens?)` passes it
through in the POST body when set. The continue button relabels to "Raise cap & continue"
when a value is present; leaving it blank preserves the plain-retry no-op exactly.

## Alternatives considered

- **Read `tokens_used_total` off `RunResponseWithDetails` for the tile**, instead of
  summing `llm.usage` events client-side. Rejected — `info` is fetched once per `runId`
  (not polled), so the tile would stay frozen at whatever value existed on page load for
  the entire life of a running run.
- **Add `tokens_used_total`/`max_total_tokens` to `RunResponse`** (the lighter model behind
  `StatusBar`/`useRunStatus`) for a live top-bar counter. Declined by the user — scope
  stayed to the `RunOverview` tile + event breakdown only.
- **Runs-list (`RunsHome`) token badge.** Declined by the user; no backend blocker, could
  be added later trivially since `RunResponseWithDetails` already carries the field now.
- **Visible per-run override input on the New Run page.** Declined in favor of the
  existing silent-passthrough pattern for the other two constraints — consistency over
  incremental UI control.
- **A dedicated toggle/filter for `llm.usage` in the event timeline.** Rejected as an
  unnecessary second control; folded into the existing `tool.*` toggle instead.
- **A dedicated `PATCH /runs/{id}/constraints` endpoint** for raising the cap, instead of
  folding it into `/continue`'s body. Rejected — there's no scenario where you'd want to
  raise the budget WITHOUT also continuing (the checkpoint patch is only useful as a
  prelude to a resume), so a separate endpoint would just add an extra round trip and a new
  resource to reason about for no behavioral gain.
- **Apply the cap patch inside the background `continue_run` task** instead of
  synchronously in the route. Rejected — a validation failure (bad cap) would only be
  observable via a polled `run_state` still reading STALLED, with no way to surface *why*
  the raise didn't take effect. Doing it synchronously lets a bad request 422 immediately,
  matching `resume_run`'s "reject requires a note" synchronous validation.
- **Allow clearing the cap back to unlimited (`null`) via `/continue`.** Rejected as out of
  scope — the user asked to enter a new value, not remove the budget entirely; the
  validated-positive-int contract keeps `ContinueRunRequest` simple.
- **Enforce `new_max_total_tokens > tokens_used_total` as a soft warning instead of a hard
  422.** Rejected — a cap that's still at or below current usage would just re-stall the
  instant the node re-enters, which is a worse UX (the human thinks they fixed it, then
  sees the exact same stall again) than a clear, immediate rejection.

## Consequences

Closes the "backend-only" gap ADR 0025 explicitly deferred, and closes the loop on the
STALLED/recoverable design from the same ADR: a token-budget stop is now recoverable
end-to-end — raise the cap from the dashboard, hit continue, the same checkpoint resumes
with a higher ceiling — not just theoretically recoverable via a no-op retry. Everything
here uses patterns already established in this codebase (denormalized `runs` columns,
schema-driven Settings fields, event-derived stats in `RunOverview`, `model_copy` partial
updates, the STALLED/`_drive` machinery) — no new architectural concepts.

Known, accepted trade-offs: the declined pieces (runs-list badge, live status-bar counter,
visible New-Run cap field) are cheap to add later — the backend fields they'd need
(`tokens_used_total`, `max_total_tokens`) already exist on `RunResponseWithDetails`, and
`RunResponse` is the only remaining gap for a live counter. Runs stalled *before* the
raise-cap feature shipped won't have `kind` in their `RUN_STALLED` event payload, so their
stalled-bar falls back to the plain "Continue run" no-op (not worth a backfill for a
dashboard affordance). `RunOverview.tsx`'s "tokens used / cap" tile, fetched once per
`runId` and not re-polled, may keep showing the old cap until the run is reopened after a
raise — a pre-existing staleness pattern (shared with `llm_model`/`task_description`), left
out of scope.
