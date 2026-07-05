# 0023 — Classify 429 "model overloaded" (and 5xx) as transient, not a degrade signal

Status: accepted
Date: 2026-07-05

## Context
A run against a busy cluster made **no progress and never terminated**. The inference
endpoint was returning HTTP **429 "Model … is overloaded: GPU cache at 0.00%"** — a
*transient* "come back later" signal. But every agent's LLM-call error handling only
treated `APITimeoutError` and `APIConnectionError` as transient (→ `RetriableError` →
STALL). A 429 raises `openai.RateLimitError`, which fell through to the **generic
`except Exception`** — whose behaviour was to **degrade**:

- Planner investigation → `investigation_summary = "Investigation failed (continuing to
  synthesis)"` → synthesized a plan off nothing.
- Reviewer investigation → same → reviewed 8 microtasks with no grounding → approved/rejected
  semi-randomly.
- Reflector → emitted a fallback "assessment_failed" diagnosis.
- Tester / Coder → the 429 reached the node's generic handler; for the Tester that meant
  `RunFailureHandler` → **terminal FAILED** on a transient overload.

Net: the planner↔reviewer loop spun on ungrounded investigations (a **no-progress vicious
loop**), and a Tester overload could kill the run outright. The `RetriableError` docstring
even *claimed* it covered "rate-limit, or 5xx" — the intent existed; the catch lists never
implemented it.

## Decision
One shared classifier, `Exceptions/RetriableError.py::is_transient_llm_error(exc)`, plus
`TRANSIENT_LLM_ERRORS = (APITimeoutError, APIConnectionError, RateLimitError,
InternalServerError)`. It returns True for those types AND for a 429/5xx that arrived
**wrapped** — e.g. raised inside a LangGraph ReAct agent, where the concrete type is lost —
by sniffing `status_code` and the message (`"overloaded"`, `"code: 429"`, `"rate limit"`,
`"503"`, `"service unavailable"`, `"try again later"`). The message fallback is essential:
the cluster's 429 reaches the planner/reviewer *investigation* handlers as a generic
exception, so type-only matching would keep missing it.

Every LLM-call site now routes **transient → `RetriableError` (STALL + /continue)** and only
degrades/fails on genuinely deterministic errors:
- Planner + Reviewer: investigation and synthesis/review handlers check `is_transient_llm_error`
  first → `RetriableError`; deterministic (recursion budget, `ValidationError`) still degrades
  or raises as before.
- Coder + Reflector: same pattern at their call sites.
- Node backstops (`tester_node`, `reflector_node`): the generic `except Exception` classifies
  a transient → `RetriableError` **before** `RunFailureHandler`, so an overload STALLS instead
  of going terminal FAILED. The Tester has no internal transient handling, so this is its only
  guard.
- `Service._drive`: the explicit `RetriableError` branch stalls with the agent's reason; a
  defensive `is_transient_llm_error` check in the catch-all stalls any raw transient that still
  escapes.

A STALLED run is recoverable — `/continue` re-runs the node once the GPU frees, re-doing the
investigation with real grounding.

## Investigation-failure discipline (planner + reviewer)
Removing the transient case from the generic `except` exposed a second problem: the planner
and reviewer **investigation** phases degraded on ANY non-transient failure — setting
`investigation_summary = "Investigation failed"` and synthesizing/reviewing anyway. This is
asymmetric with the Coder, whose LLM call IS the deliverable: the Coder recovers only from a
recursion-budget hit (and only because `repo.diff` deterministically confirms whether work
was saved), and `raise`s on any other error. The investigation has no such deterministic
downstream check, so blindly continuing produces a groundless plan/verdict that wastes the
planner↔reviewer budget.

Applied the Coder's split to both investigation handlers:
- **`GraphRecursionError`** (tool-step budget exhausted) → SOFT: plan from the objective /
  review from the plan (langgraph drops the partial messages, so there's nothing to salvage;
  the model was working, just slowly). Logs a hint to raise the tool-step budget.
- **Transient** (429/timeout/5xx) → `RetriableError` → STALL.
- **Any other (genuine) error** → `RunExecutionError` → clean **FAILED** via the node — never
  synthesize/review off a broken investigation.

## Why this is the right split
`RetriableError` re-runs the same node on `/continue`. That is correct ONLY for a **transient**
cause (a retry can succeed). A deterministic failure (bad structured output, recursion-budget
exhaustion, a code bug) must NOT become `RetriableError` — it would loop `stall → continue →
stall` forever with no progress. So the classifier is deliberately narrow: timeouts, connection
drops, 429, 5xx. Everything else keeps its existing degrade/fail path.

## Alternatives considered
- **Match only the concrete `RateLimitError` type** — misses the 429 that arrives wrapped from
  inside a ReAct agent (exactly the failing case). The status/message fallback is required.
- **Keep degrading on a failed investigation** — the observed no-progress loop. Rejected:
  synthesizing/reviewing off a failed investigation wastes the planner↔reviewer budget and
  produces groundless verdicts.
- **Auto-retry with backoff inside the agent instead of stalling** — plausible later, but a
  stall + human `/continue` keeps a heavily-overloaded cluster from being hammered and matches
  the existing recovery UX. Could add bounded in-node backoff for short 429s as a follow-up.

## Related
Complements ADR 0021 (uncaught crash → STALLED, human /fail). This narrows what counts as a
transient stall (adds 429/5xx) and stops the degrade-past-overload behaviour that ADR 0021's
crash-path never covered because the overload was being swallowed, not thrown.
