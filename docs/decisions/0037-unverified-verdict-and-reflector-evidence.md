# 0037 — "Unverified" passes through; the Reflector gets honest evidence; subproject Track A

Status: accepted
Date: 2026-07-16

Amends ADR 0016 (tester two-track grounding, verdict ownership) and ADR 0022 (reflector
spec-aware routing). Diagnosed from run `27198e26` (first MAS-on-its-own-repo run).

## Context — the failure, exactly

Run `27198e26` (task: dashboard changes on this repo itself) BLOCKED on the microtask
`past-runs-tab-ui` after 3 attempts / 5 reflections, although the Coder's diff was
complete and correct. Three interlocking causes:

1. **Verdict policy.** The microtask's targets were `.tsx`. Repo root detects as
   *python* → Track A had no signal (`integration_tests: skipped`, no build/typecheck
   found). Track B's test-writer, asked to test a React component under a pytest
   harness, emitted a pytest file where **every test was
   `@pytest.mark.skip("non-Python file — not testable via pytest")`** — which the
   trivial-guard correctly rejected. The verdict rule "supported project, nothing
   decided → FAILED (do NOT silently pass)" then failed the microtask. Every attempt.
   Deterministically. Nothing the Coder writes can ever pass a check that cannot run.

2. **Reflector evidence.** The Phase-2 diagnosis prompt embedded
   `diff[:2000] + " [truncated]"` — a head-only cut, mid-file, labelled with the word
   *truncated*. The model read the marker as a property of the CODE and diagnosed
   "the implementation of PastRuns.tsx is truncated — rendering logic missing" on a
   complete 3763-char diff. It also demanded App.tsx integration that the plan had
   assigned to a LATER microtask. The Coder, told to "complete" already-complete code,
   produced byte-identical diffs; the stagnation detector then (correctly) BLOCKed.

3. **Track A blindness.** `dashboard/npm run build` (`tsc -b && vite build`) is a real,
   deterministic, gating-grade check for exactly these files — never discovered, because
   project detection only looks at the repo root.

## Decision

### 1. Unverified → SKIPPED (pass through un-gated, loudly labelled)

This deliberately REVERSES the earlier "nothing decided → FAILED, do not silently
pass" rule. That rule assumed a FAIL is actionable; run 27198e26 shows the opposite:
when unverifiability is structural (harness can't run anything for these targets), a
FAIL routes to a Reflector that has **zero failing output to diagnose from** — the one
input shape that reliably makes an LLM invent a defect — and the loop can only end in
BLOCKED on correct code. The verdict's `unit_tests` check is now **SKIPPED** with a
reason that names exactly what was not verified (`_unverified_reason`, now also covering
the new "synthesis skipped — target untestable" case); the tester summary says
"UNVERIFIED — passed through" instead of "skipped"; the node's existing
`skipped = passed` rule advances the microtask. Not silent: the reason is on the check
artifact, the summary, a warning log — and the CODE/DEPLOY gates still put the actual
diff in front of the human. Track A still gates the moment it produces any signal.

### 2. Synthesis refuses untestable targets up-front

`SynthesisPhase._untestable_reason`: when NO declared target file is importable by the
detected harness (python → `.py`; javascript → `.js/.mjs/.cjs`; vitest additionally
`.ts/.tsx/.jsx`), synthesis is skipped with the reason recorded on the context
(`synth_untestable_reason`) instead of burning an LLM call to generate a skip-decorated
file for the guard to reject. No targets declared → synthesis still tries (criteria-only).

### 3. Reflector prompt: elide honestly, scope explicitly

- `_elide_middle` (head+tail, like `truncate_for_llm`) replaces every head-only
  `[:N] + " [truncated]"` cut in both reflector prompts; the marker states it is a
  DISPLAY cut ("NOT a gap or truncation in the code itself"). Diff budget 2000 → 12000
  (a 3763-char diff now rides whole; the old cap was sized for token panic that ADR 0025's
  budgeting already handles).
- Diagnosis prompt gains a SCOPE paragraph: judge only THIS microtask's acceptance
  criteria/targets; integration assigned to later microtasks is not missing work here.

### 4. Target-scoped subproject Track A

`DeterministicChecksPhase._subproject_js_checks`: for the CURRENT microtask's JS/TS
target files, find the nearest ancestor `package.json` (repo-root excluded — the root
branches own that), `npm --prefix <dir> install` once per run (node_modules presence =
cache), then run the subproject's own `build` script (gating), falling back to
`tsc --noEmit -p <dir>` when only a `tsconfig.json` exists. Scoped to targets so Python
microtasks never pay the npm cost. With this, `.tsx` microtasks on this repo get a real
deciding check and rarely reach the unverified path at all — fixes 1/2 are the backstop.

## Consequences / accepted risks

- A garbage-but-compiling change on an untestable target now ships to the gate instead
  of looping to BLOCKED. Accepted: the old behavior never caught it either (it burned
  attempts and blocked); the human at the CODE gate reviews the same diff with a louder
  label. Right-sized to the single-user, gated deployment.
- Subproject `npm install` adds one-time minutes to the first tester run touching that
  subproject; later iterations reuse `node_modules` (sandbox lives for the run).
- `build`-label collision (root + subproject on a mixed-target microtask): both gate
  (the verdict walks the list); only the per-label artifact map keeps the last one.
  Cosmetic; noted in code.
