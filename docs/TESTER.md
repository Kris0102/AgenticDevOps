# The Tester — Grounded Verification Agent

> Documents the Tester as of `main` @ `2c9925a` (includes ADR 0037). If you are reading
> this on the MAS result branch (`feat-add-task-tab`), the code there predates ADR 0037 —
> the doc describes the intended, current design.

---

## 1. The elevator answer

The Tester is the agent that decides whether the Coder's change is **accepted**. It is
the system's implementation of the core architecture rule: *code is only accepted when
deterministic checks pass — no LLM-only review* ("grounded verification"). It runs after
every Coder iteration, produces a PASS/FAIL/UNVERIFIED verdict from real command exit
codes, and **owns that verdict**: on PASS it marks the microtask completed itself; on
FAIL it routes to the Reflector for diagnosis. No other agent can overrule it with an
opinion — the verdict is computed, not judged.

The central design tension it solves: **an LLM cannot be trusted to verify an LLM.** A
model reviewing code "looks correct" is worthless as a gate. But most microtasks have no
pre-existing test covering them. The Tester resolves this with **two tracks**:

- **Track A (deterministic, trusted):** run what the repo already defines — its own test
  suite, build, typecheck, lint. Zero LLM involvement. This is the primary gate.
- **Track B (synthesized, distrusted-by-default):** an LLM writes a test — but from the
  **Planner's acceptance criteria**, not from the Coder's code (so it can't be
  tautological), and it is only allowed to *fail* the microtask when it is **grounded**
  in concrete input/output examples a human-adjacent source (the Planner) specified.
  Everything else about it is checked deterministically before it is believed.

---

## 2. Where it sits in the architecture

```
coder → tester → ┬— PASS, more microtasks → coder     (next microtask)
                 ├— PASS, final          → deploy     (→ deploy gate → push/PR/local)
                 ├— PASS + CODE gate on  → hitl       (human reviews the diff)
                 └— FAIL                 → reflector  (diagnosis → coder retry or test rewrite)
```

Key ownership facts (ADR 0016):

- **The Tester owns its verdict.** `TesterRunner._verdict_passed` reads exactly one
  thing: the canonical `unit_tests` check. PASSED/SKIPPED → advance, FAILED → Reflector.
- **The Reflector is failure-only.** On a green pass there is *no LLM call at all* on the
  review path — the Reflector never sees passing work. This both saves tokens and keeps
  the acceptance decision deterministic.
- **The CODE HITL gate fires on the Tester's PASS**, not on the Coder's output — the
  human reviews work that already passed machine checks.

Two node-level files implement the seam:

- `src/orchestrator/tester_node.py` — `TesterRunner`: graph plumbing. Stop-limit check,
  token budget check, runs the pipeline, persists results (DB artifacts, events,
  microtask status), decides `NEEDS_CODE` / `DONE` / `REFLECT`, applies the CODE gate.
- `src/agents/Implementing/Tester/` — the agent itself, a **phase pipeline** (below).

---

## 3. General structure: an explicit phase pipeline

The Tester is deliberately *not* one big class. `pipeline.py` builds an ordered list of
`TestPhase` objects and runs them over a shared, accumulating `TestContext`:

```python
self.phases: List[TestPhase] = [
    DetectProjectPhase(self.tools),        # 1. what kind of repo is this?
    DeterministicChecksPhase(self.tools),  # 2. Track A: suite/build/typecheck/lint
    SynthesisPhase(...),                   # 3. Track B: LLM writes a test (maybe)
    TrivialGuardPhase(self.tools),         # 4. deterministic sanity-check of that test
    ExecutePhase(self.tools),              # 5. run the synthesized test
    VerdictPhase(self.tools),              # 6. fold everything into ONE verdict
]
```

Each phase has `should_run(ctx)` + `run(ctx)`; the pipeline logs enter/skip per phase, so
a run's log reads as a visible sequence. `TestContext` (`context.py`) is a dataclass
accumulator: phase N writes fields, phase N+1 reads them (`project_type`,
`track_a_checks`, `synth_test_file`, `guard_ok`, `synth_exec_check`, `checks`, …).
`to_result()` renders the node-facing dict at the end.

Why this shape (vs the old monolith): each phase is testable alone, the order is
explicit, and a phase can be skipped without if-nests (e.g. guard/execute simply don't
run when synthesis produced nothing). It also gives crash-safe token accounting: the
pipeline keeps `self.token_usage` as a live snapshot in a `finally`, so tokens spent
before a crash are still recorded by the node's except path.

Every command the Tester runs goes through `TesterTools` → **ToolGateway** with the
TESTER role: an allowlist (`git python pip pytest node npm npx apt-get grep ls cat find
mkdir mv`) plus audit events for every call. The Tester writes only its own test files —
it is gateway-blocked from editing code.

---

## 4. Phase-by-phase

### Phase 1 — `DetectProjectPhase` (`detect_phase.py`)

Detects `project_type` from **root** markers: `package.json` → javascript (priority),
else `requirements.txt`/`setup.py`/`pyproject.toml`/`setup.cfg` → python, else
unknown → `ctx.skip = True` (whole pipeline no-ops, verdict SKIPPED — an unsupported
repo passes downstream; there is nothing to verify).

For JS it also detects the **test framework** — the fix for the tester's historical #1
false negative (writing Jest tests for a node:test repo, where `expect` doesn't exist):

```python
# scripts.test is ground truth (what the repo actually runs); devDeps are fallback;
# default node:test — the only runner needing no install.
for fw in ("vitest", "mocha", "jest"):
    if fw in test_script: return fw
```

### Phase 2 — `DeterministicChecksPhase` (Track A, `deterministic_checks_phase.py`)

The LLM-free primary grounding. Per language it runs, each verdict from an exit code:

| check | python | javascript | gates? |
|---|---|---|---|
| `integration_tests` | `pytest -q --ignore=.agentic_tests` | `npm test` (synth dir hidden) | yes — on the **delta** (below) |
| `build` | `py_compile` of the microtask's target files | `npm run build` if script exists | yes |
| `typecheck` | mypy, if configured | `tsc --noEmit`, if tsconfig | yes |
| `lint` | ruff, if configured | eslint, if configured | **no — advisory** (style must not block) |

Design decisions worth naming in an interview:

- **Config-gated:** build/typecheck/lint run only when the repo opted into the tool. A
  repo without a linter is not punished for not having one.
- **Suite-baseline delta (GH #28/#72):** on the first Tester run, the repo's own suite is
  run on the **pristine tree** (Coder's changes stashed, suite run, changes restored) and
  its failing-test count becomes the run's fixed baseline. Afterwards, a failing suite
  gates only when `current_failures > baseline` — inherited breakage (the repo shipped a
  red test) is advisory, a **new** regression gates. So a pre-broken repo doesn't fail
  every microtask, yet the Coder breaking *other* tests is still caught.
- **Synth-dir hygiene (ADR 0019):** the Tester's own generated tests live in
  `.agentic_tests/` (git-ignored, never pushed). During a Track A suite run the dir is
  physically `mv`-ed out of the tree — every JS runner globs the tree, and a stale synth
  file from a previous microtask once crashed an unrelated microtask's suite run.
- **Dependency install is cached per run** (marker dir in the sandbox) — npm/pip install
  runs once per run, not per retry.
- **Subproject checks (ADR 0037):** root detection is blind to a JS app nested in a
  Python repo (this repo's own `dashboard/`). For the current microtask's JS/TS target
  files, the phase walks up to the nearest `package.json` and runs *that* project's
  `npm --prefix <dir> run build` (gating) or `tsc --noEmit -p <dir>` — so a `.tsx`
  microtask gets a real compiler verdict. Target-scoped: Python microtasks never pay the
  npm cost.

### Phase 3 — `SynthesisPhase` (Track B, `synthesis_phase.py`)

An LLM (a small ReAct agent with read tools, `TESTER_MAX_TOOL_STEPS` budget) writes ONE
test file into `.agentic_tests/`, prompted from the **Planner's `acceptance_criteria`**
— structured criteria with optional concrete `IOExample`s — *not* from the Coder's diff.
That independence is the point: a test derived from the code it tests proves nothing.

Three restraint rules:

- **Write once, not per retry.** `should_run` synthesizes only the first time (or on an
  explicit rewrite request). Otherwise a coder-retry would face a freshly regenerated
  test every attempt — a moving target it can never converge on.
- **Refuse untestable targets (ADR 0037).** If no target file is importable by the
  detected harness (`.tsx` under pytest; `.tsx` under node:test — only vitest loads
  TS/TSX), synthesis is skipped with a recorded reason instead of generating a
  skip-decorated pytest file for the guard to reject.
- **Value-grounding (ADR 0022):** the prompt instructs — assert **structure only** for
  any criterion without an `IOExample`. The LLM never invents an expected number/string:
  an LLM cannot reliably *compute* the right value, and a wrong invented value fails
  correct code.

Degradation: if the writer exceeds its tool budget (`GraphRecursionError`), the phase
keeps whatever was written — the guard/verdict handle an unusable file. No crash.

### Phase 4 — `TrivialGuardPhase` (`guard_phase.py` + `grounding.py`)

Deterministic sanity check of the synthesized file — no LLM. Rejects when:

- **No real assertions.** Python: `ast` walk counting `assert` statements +
  assertion-style calls. JS: regex counting `expect(...)` / `assert(...)` **and** dotted
  `assert.equal(...)` forms (the missing dotted form once zero-scored every node:test
  file → rejected correct tests, run 7a64fb2b).
- **Everything skipped.** All test functions `@skip`-decorated → nothing executes.
- **Undeclared/uninstalled JS imports.** Extracts every import specifier, resolves each
  inside the sandbox via `require.resolve`, checks names against `package.json` — a test
  importing a library the repo doesn't have can only error, not verify.

A rejected file sets `guard_ok=False`; it is **excluded from the verdict** (Track A
decides), never blamed on the Coder.

### Phase 5 — `ExecutePhase` (`execute_phase.py`)

Runs the synth file with the repo's own runner (`pytest -q <file>`, `npx jest
--runTestsByPath <file>`, `npx vitest run <file>`, `npx mocha <file>`, `node --test
<file>`), 60s bound. Two hard-won classification rules:

- **Exit-code taxonomy:** only 0 (pass) and 1 (assertion failure) are code verdicts.
  2/3/4/5 (collection/config errors) and 124/137 (the sandbox watchdog killed a hang —
  e.g. a browser module with a live `setInterval` keeping Node alive) → **ERRORED** =
  harness problem, excluded from the verdict.
- **Load-error reclassification:** an exit-1 that is really a *load* failure is
  indistinguishable from an assertion failure by exit code. The phase scans output for
  module-resolution markers (`Cannot find module`, `ERR_MODULE_NOT_FOUND`, `SyntaxError:`)
  and browser globals (`document is not defined`, …) and flips FAILED→ERRORED. Scoped
  tightly: a genuine `myVar is not defined` in the *code* stays FAILED.

### Phase 6 — `VerdictPhase` (`verdict_phase.py`)

Folds both tracks into ONE canonical `unit_tests` check — the only thing the node (and
Reflector) read. The decision table:

```
unsupported repo (ctx.skip)              → SKIPPED   (passes; nothing to verify)
any deciding check FAILED                → FAILED    (real code/build/type failure)
any deciding check PASSED                → PASSED
supported repo, nothing decided          → SKIPPED "unverified" (pass-through, loud)
```

Who gets to "decide": Track A's `integration_tests`/`build`/`typecheck` (lint never;
inherited suite failures never) — plus the synth check **only if it gates**:

```python
# Track B GATES only when grounded: the Planner supplied ≥1 concrete IOExample AND
# the guard passed. Otherwise advisory: a PASS counts as positive evidence, a FAIL
# does NOT fail the microtask.
```

That asymmetry is the heart of Track B's trust model. An ungrounded LLM test failing is
the tester's main **false-negative** generator (it fails correct code); an ungrounded
test *passing* is still weak positive evidence. So: grounded → full gate; ungrounded →
its PASS counts, its FAIL doesn't.

**The "unverified" rule (ADR 0037)** — the newest and most interview-worthy decision.
Originally "nothing decided → FAILED (don't silently pass)". Run `27198e26` proved that
wrong: a `.tsx` microtask in a Python-rooted repo had *no* runnable check, so it failed
**structurally** — a FAIL no code change can flip. The Reflector got a failure with zero
failing output to explain, hallucinated a code defect ("implementation truncated"), the
Coder had nothing to fix, produced identical diffs, and the stagnation detector blocked
the run. On correct code. Every time. The rule is now: unverified → **SKIPPED
pass-through, loudly labelled** — reason on the artifact, "UNVERIFIED" in the summary, a
warning log — because the human at the CODE/DEPLOY gates still reviews the actual diff,
and Track A still gates the moment it has any signal. Honest uncertainty beats
manufactured failure.

---

## 5. Outputs — where results go

- **`evidence.checks`** (blackboard): fixed-field model (`unit_tests`,
  `integration_tests`, `build`, `typecheck`, `lint`), each with status/exit
  code/artifact ref. The canonical verdict is the `unit_tests` field.
- **DB artifacts** (per iteration): one artifact per check (`test-build`,
  `test-integration_tests`, …, full command output), plus `test-synth` — the synthesized
  test's *source* + its **role in the verdict** ("gated" / "advisory — failure ignored" /
  "excluded — trivial-guard rejected"), so a human can always see what Track B did.
- **Events**: `microtask.completed` / `microtask.failed` with per-check summaries;
  `llm.usage` for the synth agent's tokens (budget-enforced like every agent).
- **Routing statuses**: PASS → `NEEDS_CODE` (more microtasks) or `DONE` (final;
  → deploy); FAIL → `REFLECT`. The CODE HITL gate is applied here on a pass.
- On a Reflector `fix_target="test"` verdict, the Tester re-synthesizes the wrong test
  (`NEEDS_TEST` + `test_rejection_note`), bounded by `REFLECTOR_MAX_TEST_RESYNTH`.

---

## 6. Configuration knobs (Settings hub, ADR 0020)

| knob | effect |
|---|---|
| `TESTER_MAX_TOOL_STEPS` (default 6) | tool-call budget of the synth writer; kept tight so a wandering agent fails fast |
| `tester_timeout_seconds` | LLM request timeout for the writer |
| `REFLECTOR_MAX_TEST_RESYNTH` (default 2) | bound on the reflector→tester test-rewrite loop; resets when the coder runs |
| per-run `RunConfig.llm.tester` | which provider/model writes the tests |

Command timeouts are per-call (`suite 300s / synth exec 60s / installs 120–600s`), all
enforced by the sandbox watchdog (`timeout --kill-after=5`) — a hung command returns
124/137 instead of freezing the run.

---

## 7. Limitations (honest list — say these before being asked)

1. **Verification depth = what the repo defines.** A repo with no suite, no build, no
   typecheck gets only Track B — and an ungrounded Track B can't fail anything. Floor:
   "it compiles and an LLM's structural test passed." UNVERIFIED pass-through (ADR 0037)
   makes this *visible* but doesn't fix it; the human gate is the backstop.
2. **No runtime/behavioral verification for UI.** `.tsx` work is verified by compiler
   only (subproject `tsc`/vite build). Nothing renders the component or clicks it — no
   jsdom, no browser harness in the sandbox.
3. **Track B value-blindness by design.** Without Planner `IOExamples`, synthesized
   tests assert structure only — they can't catch a wrong *value* (correct-looking
   function returning wrong number). The Planner is nudged to supply examples for
   boundary cases; when it doesn't, that class of bug passes.
4. **JS assertion counting is regex, not a parser.** Good enough for the trivial cases
   it guards, foolable in principle (assertions in comments/strings count).
5. **Root-first detection.** A repo that is *only* a nested app (no root markers) detects
   UNKNOWN → whole tester skips. Subproject checks help only when the root detected as
   something.
6. **Dep installs cached per run** — a later microtask adding a dependency won't see it
   installed until a new run/sandbox.
7. **`lint` advisory means style never blocks** — deliberate, but it means eslint-level
   bugs (unused var that indicates a typo) don't gate.
8. **One synth file per microtask.** Multi-file/integration-level acceptance criteria
   are squeezed into one test file per microtask.

---

## 8. War stories (each is a real problems-log entry — strong interview material)

| incident | lesson baked into the code |
|---|---|
| Jest tests written for a node:test repo — could never pass, blocked correct code | framework detection from `scripts.test`; synthesis AND execution are framework-aware |
| Synth test died importing a browser module (`document is not defined`) — exit 1 blamed the coder | load-error reclassification FAILED→ERRORED; ERRORED never decides |
| `assert.equal(...)` scored zero assertions → guard rejected correct node:test files | dotted-form regex; two fixes colliding is why the guard and the writer must evolve together |
| test with live `setInterval` hung `node --test` forever → whole run frozen | sandbox watchdog `timeout --kill-after`; 124/137 = harness error |
| `.tsx` microtask: nothing runnable → "unverified" FAILED → reflector hallucinated from a truncated diff → stagnation BLOCK on correct code (run 27198e26) | UNVERIFIED = SKIPPED pass-through (ADR 0037); synthesis refuses untestable targets; subproject Track A |
| repo shipped one red test → every microtask failed Track A | pristine-tree baseline; gate on the failure-count delta |

Common thread — the Tester's failure mode is almost never "missed a bad change"; it is
**failing correct code** (false negatives), and every mechanism above exists to remove a
source of those while keeping the gate deterministic.

---

## 9. Likely interview questions, short answers

**Q: Why not let an LLM review the code directly?**
Ungrounded: a model's "looks right" has no error signal. The architecture's core rule is
that acceptance requires deterministic evidence — exit codes, not opinions. LLMs are used
where generation is needed (writing a test), and everything they produce is checked
deterministically before it is believed (guard → grounded-gating → exit codes).

**Q: How do you stop the LLM-written test from being tautological?**
It is synthesized from the *Planner's* acceptance criteria, never from the Coder's diff —
independent provenance. Plus the trivial-guard (real assertions, nothing skipped, deps
resolvable) and the grounded/advisory split.

**Q: What happens when tests fail?**
Tester emits FAILED with the raw outputs → Reflector (failure-only) diagnoses with the
diff + criteria + test source → routes `fix_target=code` (coder retry) or `=test`
(tester rewrites the test, bounded). Per-microtask attempt cap bounds the loop; a
stagnating coder (identical diff) blocks the microtask.

**Q: What if the repo has no tests at all?**
Suite check is SKIPPED (not failed — "no tests found" is not a failure), build/typecheck
gate if configured, Track B carries what it can. Worst case: UNVERIFIED pass-through,
loudly labelled, human gate as backstop.

**Q: Why is the verdict emitted as `unit_tests`?**
`evidence.checks` is a fixed-field model and everything downstream (node routing,
Reflector) reads that one field — folding both tracks into it means a Track A regression
can never be invisible. The raw per-check results are still emitted alongside for audit.
