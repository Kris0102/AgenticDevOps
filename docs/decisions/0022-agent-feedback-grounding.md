# 0022 — Agent feedback grounding: converge the planning loop, ground synthesized tests

Status: accepted
Date: 2026-07-05

## Context
Two separate runs FAILED on the same underlying weakness — an LLM feedback channel that
was **unbounded or ungrounded**, so a loop couldn't converge before its cap fired. Neither
was hallucination; both were fixable in how we shuttle feedback between agents.

- **Run `5300771d` (planning loop).** A 1-word plan error (`data_contracts: temperature`
  said `string`; the codebase returns `number`) took 7 planner↔reviewer iterations to fix
  and hit `max_total_iterations` right as it converged. Cause A: `planner.py` rendered
  **every** prior `review_feedback` entry, so the planner drowned in 1,2,…,6 stacked
  rejections (same points, "(UNRESOLVED)" tags). Cause B: the reviewer's single free-text
  `notes` field captured its ReAct reasoning ("let me verify the codebase…") — non-actionable
  text fed forward as a "blocker." GH #43.

- **Run `f86480ec` (execution loop).** For a "distance-3 boundary → Did you mean" case the
  Planner supplied no `IOExample`, so the Tester **invented** input `"Brlinnnn"`,
  hand-miscalculated its Levenshtein distance as 3 (it is 4), and asserted a spec-violating
  message no correct code produces. The Reflector then chose `fix_target=code` every round —
  its Phase-2 diagnosis prompt is **never given the acceptance criteria or the failing test
  source**, so it was structurally blind to the spec — and the coder burned all attempts →
  FAILED. The `fix_target=test` re-synthesis valve (ADR 0016 / GH #33), built for exactly
  this, never fired. GH #44.

The unifying principle: **a test/verdict is only trustworthy if its expected value comes from
an independent, reviewed source — not recomputed by the same fallible LLM reasoning.** Copying
a reviewed value is reliable; recomputing it (a distance, a message) is not.

## Decision

### Planning loop — latest-only feedback, structured blockers (GH #43)
- `planner.py::_build_revision_context` renders only the **latest** review verdict (each
  review is a complete re-assessment that supersedes earlier ones). The full history stays
  on the blackboard for audit; only the prompt rendering is trimmed. `reviewer.py`'s plan
  dump is trimmed to the latest feedback for symmetry.
- `ReviewerOutput` gains `blockers: List[str]` (concrete "defect + fix"), **forwarded to the
  planner instead of the free-text `notes`** (`to_feedback_notes()`), so reviewer reasoning
  can't leak into the actionable channel. Prompt requires findings-only and to APPROVE when
  no concrete defect exists. Dead `revision_context` removed.
- Result: the same class of run converges in **2 iterations** with clean structured blockers.

### Execution loop — spec-aware reflector + grounded synthesis (GH #44)
Three layers, defense in depth (prevent → refuse → catch):
1. **Planner nudge** (`prompts/synthesis.py`): MUST supply an `IOExample` for any case whose
   expected output is a **precise computed value** — boundary/threshold, exact error/message,
   computed number, edge input — because the Tester cannot compute these reliably and must
   copy the value verbatim. Domain-neutral (math / parsing / rounding examples).
2. **Tester refuses to guess** (`tester_prompt.py`, both languages): for any case with no
   concrete example, assert **structure only** (throws / shape / type) — never a self-computed
   value/number/distance/message.
3. **Reflector catches it anyway** (`reflector.py` + `reflector_prompt.py`): the Phase-2
   diagnosis prompt now injects the **acceptance criteria** (with IOExamples) and the
   **failing synthesized test source**; the code-vs-test guidance is rebalanced to route
   `fix_target=test` when a test asserts a value contradicting an example or self-computed for
   an un-exampled case. Still defaults to `code` when unsure; the `REFLECTOR_MAX_TEST_RESYNTH`
   cap bounds abuse.

## Alternatives considered
- **Stack all feedback but tell the planner to ignore stale items** — relies on the model to
  triage noise it shouldn't have been given; latest-only removes the noise at the source.
- **Bump the iteration caps instead of fixing feedback** — hides the convergence problem,
  costs more LLM calls, and still fails on a slightly harder case. Rejected.
- **Deterministic value-grounding guard now** (parse synth assertions, reject any exact value
  not traceable to an IOExample) — the strongest guarantee, but a per-language semantic parse.
  Deferred to GH #44 as a follow-up; the three prompt/routing layers above are the right-sized
  first cut.
- **Make the planner the sole guarantee** — it can miscompute too; the value would then be
  wrong in one reviewable place (better than silently invented at test time), but the tester +
  reflector layers are still needed so no single LLM is trusted as a calculator.

## Status of follow-ups
GH #44 tracks: the deterministic value-grounding guard, implementing the promised
`IOExample`→assertion path (the `IOExample` docstring claims it exists; it does not — examples
are flattened to prose by `render()`), and measuring planner example coverage across domains.
