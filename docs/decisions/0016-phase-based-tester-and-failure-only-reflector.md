# 0016 — Phase-based Tester, structured acceptance criteria, failure-only Reflector

Date: 2026-06-24
Status: Accepted (implements ADR 0015's two-track design; supersedes the always-REFLECT
tester→reflector wiring introduced alongside the first real Reflector)

## Context
The Tester became a real agent but as one ~500-line class with its phases buried in
private methods, and a concurrent refactor wired it so it *always* emitted `REFLECT` —
every outcome, including a green pass, took an LLM round-trip through the Reflector to
"approve". That is wasteful, puts an LLM in the pass path (violates grounded-verification),
and subordinates the Tester to the Reflector. ADR 0015 also called for two-track grounding
and structured, example-bearing acceptance criteria. This ADR implements all of it.

## Decisions

### 1. Phase-based Tester (`src/agents/Implementing/Tester/`)
A slim `TestPipeline` orchestrates an ordered list of `TestPhase` classes, each in its own
module under `phases/`, so the flow is visible and each phase is independently testable:
`DetectProjectPhase` → `DeterministicChecksPhase` (Track A) → `SynthesisPhase` (Track B) →
`TrivialGuardPhase` → `ExecutePhase` → `VerdictPhase`. A `TestContext` accumulator is
threaded through. The old monolithic `tester.py` is deleted.

### 2. Two-track grounding (ADR 0015)
- **Track A (primary, deterministic):** `DeterministicChecksPhase` runs the repo's own
  suite (`pytest` / `npm test`) plus **build / typecheck / lint**, each verdict from an
  exit code, no LLM. Build/typecheck/lint are **config-gated** — a check runs only when the
  repo opted into that tool (config file / npm script present): Python build = `py_compile`
  of the microtask's target files, typecheck = `mypy` (if `[tool.mypy]`/`mypy.ini`), lint
  = `ruff` (if `[tool.ruff]`/`ruff.toml`); JS build = `npm run build`, typecheck =
  `tsc --noEmit` (if `tsconfig.json`), lint = `eslint` (if eslint config). `mypy`/`ruff`
  run via `python -m` (gateway allowlists `python`, not the tools). Build + typecheck +
  suite **gate** the verdict; **lint is advisory** (recorded, non-gating — style shouldn't
  block acceptance).
- **Track B (fallback):** `SynthesisPhase` has the LLM write tests from the Planner's
  `acceptance_criteria` (independent of the Coder). `TrivialGuardPhase` (`grounding.py`)
  deterministically rejects assertion-free / all-skipped / empty files via `ast` (Python)
  or regex (JS), so a do-nothing test can't rubber-stamp the Coder.
- `VerdictPhase` combines both into the canonical `unit_tests` check (the Reflector and
  node read pass/fail from `evidence.checks.unit_tests`); a Track A regression therefore
  can't be hidden. The raw existing-suite result is also surfaced as `integration_tests`.

### 3. Structured acceptance criteria (`state.py`)
`Microtask.acceptance_criteria: List[str]` → `List[AcceptanceCriterion]`, where
`AcceptanceCriterion = {text: str, examples: List[IOExample]}` and
`IOExample = {given: dict, expect: Any}`. With examples present, the Tester asserts
directly on planner-supplied I/O (deterministic, no prose re-interpretation) — the
strongest anti-tautology grounding. `examples` is optional (prose fallback for
side-effecting behaviour). The Planner synthesis prompt teaches the new shape; the quality
gate validates non-empty `text`. The DB JSON column is unchanged; migration
`b1c2d3e4f5a6` backfills existing rows. `AcceptanceCriterion.render()` is the single
rendering helper used by every prompt/output-file consumer. API `MicrotaskView` stays
`List[str]` (a lean dashboard projection — the service renders `.render()`).

### 4. Failure-only Reflector contract (ADR 0016)
The Tester owns its verdict again:
- **PASS** → Tester marks the microtask COMPLETED/DONE, emits `NEEDS_CODE`/`DONE`, sets
  `_tested_microtask_id`. The **CODE** HITL gate fires here (`orchestrator._tester` wraps
  the pass result); approve advances (coder/deploy), reject resets the microtask +
  seeds a Reflection → coder.
- **FAIL** → `REFLECT` → the **Reflector**, now failure-only: it diagnoses, seeds a
  `Reflection`, emits `NEEDS_CODE` → coder. No approve path, no microtask completion, no
  gate firing, and crucially no LLM call on green.

Routing: `_tester_router` handles REFLECT/NEEDS_HITL_APPROVAL/NEEDS_CODE/DONE;
`_reflector_router` collapses to NEEDS_CODE→coder. The duplicate `Phase` enum in `hitl.py`
was deleted and re-exported from `state.py` (single source of truth).

## Alternatives rejected
- **Keep the always-REFLECT wiring** — rejected: wasteful LLM call on every pass, weak
  grounding, Tester subordinated to Reflector.
- **A new parallel "what to test" field** — rejected: `acceptance_criteria` already is the
  Planner→Tester contract; a second field is a drifting source of truth.
- **Reflector does an LLM "did the tests really cover it?" check on pass** — rejected: the
  deterministic `TrivialGuardPhase` is stronger and keeps the pass path LLM-free.

### 5. Typed check records + honest verdict semantics
Checks are a typed `CheckResult` (`results.py`), not `Dict[str, Any]`. `CheckStatus` splits
the *deciding* outcomes (`PASSED`/`FAILED`) from the non-deciding ones:
- `SKIPPED` — nothing to verify (no suite / unsupported project).
- `ERRORED` — the check ran but hit a **harness** problem (pytest collection error exit
  2/3/4, gateway error, timeout). ERRORED does **not** decide the verdict — so a flaky
  synthesized-test import no longer falsely blames the Coder (fix: misattribution). If a
  Track-A check grounds the microtask, an ERRORED synth is ignored → PASS.

`VerdictPhase` decides from `PASSED`/`FAILED` checks only; if a **supported** project
produced **no** deciding check (all skipped/errored, or the guard rejected the synth file),
the verdict is `FAILED "unverified"` — it does **not** silently pass (fix: zero-verification
hole). Unsupported projects still `SKIPPED` (intentional). `lint` stays advisory. GitHub
#28–#32 track the follow-ups (suite baseline, synth-only-when-uncovered, stronger guard,
synth retry-on-harness-error, pipeline unit tests).

## Consequences / still open
- SynthesisPhase runs on every supported project (not yet "only when uncovered").
- Build/typecheck/lint now implemented (config-gated). Open: typecheck/lint only run when
  the repo configured the tool — repos with no config get suite + build only.
- Interface-only (no impl body) test-writer grounding still deferred (ADR 0015 "ideal").
- Reflector-as-always-on-judge vs failure-only is under reconsideration (GitHub #26).
- Coder compile self-check before the tester is proposed separately (GitHub #27).
- Whole-run coder↔tester↔reflector iteration cap still missing (audit #1).
