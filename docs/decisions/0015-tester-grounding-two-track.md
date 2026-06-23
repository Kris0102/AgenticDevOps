# 0015 — Tester grounding: deterministic checks primary, LLM test-synthesis secondary

Date: 2026-06-23
Status: Accepted (target design) — current Tester is single-track synthesis; gap tracked as TODO

## Context
The Tester was promoted from a stub to a real agent that (1) detects project type
(Python/JS), (2) uses the LLM to **write** a test file for the current microtask, then
(3) runs it (pytest/jest). See `src/agents/Implementing/Tester/`.

This collides with a core project invariant (CLAUDE.md): *grounded verification — code
is only accepted when deterministic checks pass; no LLM-only review.* As built, the
**same model writes the code and the tests**, so the tests can be tautological (assert
what the implementation already does) or trivially passing — the LLM marking its own
homework. It also never runs the repo's existing suite, so pre-existing regressions go
undetected, and "final validation" only re-runs the one microtask's test file (label
fixed in this branch from "full regression" → "final-microtask checks").

## Decision (target design — two tracks, different trust levels)

**Track A — Deterministic checks (PRIMARY, always, no LLM). This is the grounding.**
1. Run the repo's **existing test suite** (regression).
2. Lint / typecheck / build / import-smoke where configured.
3. Verdict comes from exit codes, not a model. A repo that already has tests is verified
   by Track A alone — no generation needed.

**Track B — Test synthesis (SECONDARY, conditional). Only when coverage is absent.**
1. Trigger only when the microtask's `acceptance_criteria` have no existing test.
2. Write tests from the **spec** (`acceptance_criteria`, authored by the *Planner* — a
   source independent of the Coder), reading source only for signatures/imports — NOT to
   mirror its behavior. Independent author ⇒ not tautological.
3. **Deterministic anti-trivial guard:** AST-parse the generated test; reject if zero
   assertions, all-`skip`, or empty bodies. Keeps the grounding deterministic even here —
   assertion *count* is not an LLM judgment.
4. **Commit generated tests** so they persist into the repo and become Track A input on
   the next run — bootstraps a test-less repo into a tested one.

**Verdict wiring:** Track A fail → `REFLECT`; Track B generated-test fail → `REFLECT`
(real signal); Track B pass but trivial-guard rejects → not verified (don't count as pass).

**Ideal, deferred:** give the test-writer the public *interface* (signatures + docstrings
+ criteria), not the full function body, to further cut code↔test collusion.

## Planner ↔ Tester interface — `acceptance_criteria` is the grounding contract

The Tester still writes its own tests (no agent reassignment). What keeps that from being
tautological is the *input* it grounds on: the **Planner's `Microtask.acceptance_criteria`**
— authored independently of the Coder. The contract is **≥1 assertion per criterion**;
the tester reads the Coder's source only for names/imports, never to mirror its behavior.

Do **not** add a new "what to test" field — `acceptance_criteria` already is it, and a
parallel field would be a second source of truth that drifts. `test_strategy` stays as the
optional HOW hint. (Both fields are annotated as tester inputs in `state.py`.)

**Recommended enrichment (strongly-typed criteria):** upgrade
`acceptance_criteria: List[str]` → `List[AcceptanceCriterion]`, where a criterion carries
human-readable intent plus *optional concrete examples* supplied by the Planner:

```python
class IOExample(BaseModel):
    given: dict      # inputs
    expect: object   # expected output

class AcceptanceCriterion(BaseModel):
    text: str
    examples: List[IOExample] = Field(default_factory=list)
```

With an example present, the tester emits `assert add(2, 3) == 5` from *planner-supplied
data* — deterministic, zero interpretation, tautology eliminated (expected value comes from
the spec author, not the implementer). When `examples` is empty (side effects, file writes,
"logs an error"), fall back to prose + LLM-written assertion. Tradeoff: more burden on the
Planner LLM to emit valid examples — hence examples are optional, not required. This is the
preferred Planner→Tester grounding interface; it supersedes free-prose criteria as the
target, but is a schema change (new migration for `microtasks`) so it is deferred with the
rest of the two-track work.

## Alternatives rejected
- **Keep single-track LLM synthesis as-is.** Rejected: violates "no LLM-only review";
  self-rubber-stamping risk; no regression coverage.
- **Drop synthesis, only run existing checks.** Rejected: a brand-new / test-less repo
  would never be verifiable. Synthesis is needed as a *fallback*, not the primary path.

## Consequences / status
- Colleague's current Tester = Track B without the guard, run as the primary path, and
  with no Track A. Functional end-to-end but under-grounded.
- Not refactored now (avoids churning working code mid-branch). Tracked as TODO below /
  in CLAUDE.md "Known bugs". Implement before the Tester is trusted to gate real changes.
