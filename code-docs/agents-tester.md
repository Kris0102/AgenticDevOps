# src/agents/Implementing/Tester/ — Code Explanation

~1600 lines — the most engineered agent. A phase pipeline (ADR 0016) with
two-track grounding; the LLM only ever *writes* a test file — every verdict is
computed from exit codes and deterministic parsing.

```
TestPipeline.run(ctx):
  DetectProjectPhase → DeterministicChecksPhase (Track A) → SynthesisPhase (Track B)
  → TrivialGuardPhase → ExecutePhase → VerdictPhase
```

---

## pipeline.py (112) — `TestPipeline`

Builds collaborators once (the tester LLM, TWO ReAct test-writer agents — one
with the Python system prompt, one JS) and the ordered phase list. `run()`
constructs a `TestContext` and folds it through the phases; each phase may opt
out via `should_run(ctx)`. Two details:
- `self.token_usage` is a **live snapshot** updated after every phase — the
  crash path in TesterRunner reads it because the result dict (which normally
  carries usage) never gets built when a phase raises; captured again in a
  `finally`.
- The pipeline does NOT decide routing — TesterRunner derives PASS/FAIL from the
  emitted checks. Separation: pipeline produces evidence, runner judges.

## context.py (113) — `TestContext`

The accumulator dataclass passed phase→phase. Read it as the pipeline's wire
protocol: inputs (state, microtask, final_validation, `rejection_note` — the
re-synthesis request channel), detect outputs (`project_type`, `skip`,
`js_test_framework` — the repo's ACTUAL runner: jest/vitest/mocha/node:test;
always-Jest once failed correct code, "the tester's #1 false-negative"),
Track A results, Track B artifacts (`synth_test_file/content`, `guard_ok`,
`synth_exec_check`, `synth_untestable_reason`, `synth_verdict_role`), verdict
outputs. `to_result()` renders the node-facing dict.

## phases/detect_phase.py (77)

Marker-based: `package.json` → javascript (framework sniffed from
devDependencies/scripts), Python markers → python, else `ctx.skip = True`
(unsupported → the whole pipeline degrades to a SKIPPED verdict — "the Reflector
treats skipped as passed, nothing to fix").

## phases/deterministic_checks_phase.py (361) — Track A

The LLM-free primary grounding. Per project type: install deps, then
- **suite** (`pytest` / npm test — labelled `integration_tests`),
- **build** (py_compile targets / `npm run build` if the script exists) — gating,
- **typecheck** (mypy / `tsc --noEmit` if configured) — gating,
- **lint** (ruff / eslint if configured) — advisory.
All **config-gated**: a check runs only when the repo opted into the tool — "a
repo without a linter isn't punished for not having one."

**Baseline-delta gating (GH #28/#72):** `_establish_baseline` captures the
pristine tree's failing-test COUNT once per run (stored on
`suite_baseline_failures` in blackboard state); `_parse_failing_count` regexes
jest's `Tests: N failed` line (preferred over the suite-level line) or pytest's
`N failed`. The verdict later gates on `current > baseline` — inherited red
tests don't fail the agent, NEW regressions do.

**Subproject checks:** `_subproject_js_checks` — root detection is blind to a
`dashboard/` nested in a Python repo; for JS/TS targets it finds the nearest
`package.json` and runs `npm --prefix` build/tsc there (gating). Without it,
`.tsx` microtasks had zero Track A signal (run 27198e26).

## phases/synthesis_phase.py (140) — Track B

LLM writes a test file from the **Planner's acceptance criteria** — authored
independently of the Coder ("what keeps the tests from being tautological").

- `should_run` encodes two hard-won rules:
  1. **Synthesize only when needed** — first time, or when a rewrite was
     explicitly requested (`rejection_note` set by reflector fix_target=test or
     a HITL reject). On a normal coder-retry the existing test is reused —
     "otherwise the test becomes a moving target the coder can never converge
     on (it satisfies vN, we regenerate vN+1, …)."
  2. **Refuse untestable targets up-front** (`_TESTABLE_EXTS`): a `.tsx` target
     under a pytest harness can only produce reject-bait (run 27198e26 made a
     pytest file where every test was @skip'd "non-Python file" → guard reject →
     eternal FAIL). Sets `synth_untestable_reason` instead. vitest is the one JS
     runner allowed TS/TSX.
- Output goes to **`.agentic_tests/`** (`SYNTH_TEST_DIR`) — git-ignored (seeded
  at bootstrap) AND pathspec-excluded at push: verification scaffolding, never a
  deliverable. Depth-1 so relative imports resolve.

## grounding.py (144) + phases/guard_phase.py (110) — the anti-trivial guard

PURE functions (no LLM, no I/O): Python counts real assertions via the **ast**
module (assert statements + assertEqual/pytest.raises-style calls) and detects
all-@skip files; JS uses regexes (no in-process JS parser — "good enough for the
trivial cases"). The guard phase also checks a JS synth test only imports
**declared+installed** packages. A rejected file sets `guard_ok=False` → the
verdict treats Track B as "not verified" rather than letting a do-nothing test
rubber-stamp the Coder.

## phases/execute_phase.py (107)

Runs the synth test with the repo's OWN runner (per detect). Two classification
subtleties, both misattribution fixes:
- Exit codes other than 0/1 (collection error, config error, watchdog timeout)
  → **ERRORED** — "a harness problem, NOT a statement about the code."
- `_load_error_reason`: browser globals (`document`, `window`…) or module-
  resolution failures in the output mean the test **died importing**, never
  executed the code under test → environment error, not a code failure. Scoped
  tightly so a genuine `ReferenceError` in the code still FAILS.

## phases/verdict_phase.py (225) — where the verdict is computed

`_combined_unit_check` builds the canonical `unit_tests` check:
1. Gating inputs = Track A `{integration_tests, typecheck, build}` — minus
   **inherited suite failures** (failing count ≤ baseline).
2. Track B gates ONLY when *grounded* (`_synth_gates`: the Planner supplied
   concrete IOExamples the test asserts real values against). Ungrounded synth
   is advisory: a PASS counts as positive evidence, a FAIL does NOT fail the
   microtask ("an ungrounded prose-derived LLM test is the tester's main
   false-negative — it fails correct code"). ERRORED never decides.
3. Verdict ladder: skip-project → SKIPPED; any deciding FAILED → FAILED; any
   deciding PASSED → PASSED; **nothing decided → SKIPPED with a loud
   `unverified` reason** (ADR 0037) — "the coder can't fix 'nothing could run',
   and failing it loops reflector+coder to BLOCKED on correct code." The human
   still reviews the diff at the gates.
4. Also stamps `synth_verdict_role` (gated/advisory/excluded/not-run — single
   source of truth, shown in the test-synth artifact) and dumps the debug
   output file.

## tester_tools.py (141) / results.py (101) / tester_output.py (196)

- `TesterTools`: gateway-routed tools under `Roles.TESTER` (the only role with
  `pytest`/`npm`); LLM-facing wrappers truncated, deterministic-phase calls raw.
  The test-writer is deliberately NOT given `get_repo_diff` (avoids verify
  loops).
- `results.py`: `CheckResult` (label/command/status/exit/stdout/stderr/
  artifact_text) + `CheckStatus` (PASSED/FAILED/SKIPPED/ERRORED);
  `decides_verdict` = PASSED|FAILED only.
- `tester_output.py`: per-microtask debug dump (test content, verdict, note,
  reflections, tokens).

---

## Relationships

```
tester_node (Runner) ─► TestPipeline.run ─► ctx.to_result()
Track A: repo's own suite/build/typecheck/lint (exit codes)  ─┐
Track B: acceptance_criteria ─► LLM writes .agentic_tests/ ──┤─► VerdictPhase → unit_tests check
         guard (ast/regex) + execute (own runner)            ─┘
Runner._verdict_passed reads unit_tests: PASSED|SKIPPED → pass; else → REFLECT
rejection_note ◄── reflector (fix_target=test) / HITL — triggers re-synthesis
suite_baseline_failures ◄─► blackboard (established once, gates on delta)
```

Exam sentence: "The Tester's LLM writes tests but never grades them: assertions
are counted by an AST parser, execution is exit codes, inherited failures are
subtracted by a baseline count, and when nothing could be verified it says
UNVERIFIED instead of lying in either direction."
