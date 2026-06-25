# Tester agent — how it works

The Tester is the **verification** step of the execution loop. After the Coder edits files
for one microtask, the Tester decides — **deterministically** — whether that microtask is
done. It does not "review" code with an LLM opinion; it produces grounded pass/fail
evidence from real command exit codes.

See `docs/decisions/0015-*` and `0016-*` for the *why*. This file is the *what/how*.

---

## Where it sits in the graph

```
coder ──NEEDS_TEST──► tester ──► verdict
                         │
   PASS  → mark microtask COMPLETED/DONE, emit NEEDS_CODE (next task) / DONE (final)
   │        └─ CODE HITL gate fires here (human reviews diff + tests)
   │           approve → coder (next) / deploy ;  reject → reset microtask + Reflection → coder
   FAIL  → REFLECT ──► reflector (diagnose, seed Reflection) ──► coder
```

- The **Tester owns its verdict** (ADR 0016). PASS advances; FAIL goes to the failure-only
  Reflector. No LLM runs on a green pass.
- Wiring: `src/orchestrator/tester_node.py` (`TesterRunner`) is the graph node;
  `src/orchestrator/orchestrator.py` `_tester` / `_tester_router` route it.

---

## The pipeline — six phases

The agent is a thin orchestrator (`TestPipeline`, `pipeline.py`) that runs an **ordered list
of `TestPhase` objects**, threading one mutable `TestContext` through them. Each phase reads
what earlier phases wrote and adds its own output.

```
DetectProjectPhase        → ctx.project_type ("python"/"javascript"/"unknown"), ctx.skip
DeterministicChecksPhase  → ctx.track_a_checks = [suite, build, typecheck, lint]   (Track A)
SynthesisPhase            → ctx.synth_test_file, ctx.synth_test_content            (Track B, LLM)
TrivialGuardPhase         → ctx.guard_ok, ctx.guard_reason                          (deterministic)
ExecutePhase              → ctx.synth_exec_check                                    (runs synth file)
VerdictPhase              → ctx.checks (canonical unit_tests + raw checks), summary, output file
```

`TestPipeline.run` (`pipeline.py`):
```python
for phase in self.phases:
    if phase.should_run(ctx):
        _log.info("tester.phase.enter", phase=phase.name); ctx = phase.run(ctx)
    else:
        _log.info("tester.phase.skip", phase=phase.name)
return ctx.to_result()
```
`should_run` lets a phase opt out (e.g. synthesis skips on an unsupported project) while the
list stays flat and readable.

### Files
| file | role |
|------|------|
| `pipeline.py` | `TestPipeline` — builds the LLM test-writer agents + the phase list, runs them |
| `context.py` | `TestContext` — the accumulator dataclass passed phase→phase |
| `results.py` | `CheckResult` dataclass + `CheckStatus` enum + `make_check`/`skipped_check` |
| `grounding.py` | pure deterministic anti-trivial guard (AST for Python, regex for JS) |
| `phases/base.py` | `TestPhase` ABC (`name`, `should_run`, `run`) |
| `phases/detect_phase.py` | Phase 1 — project type |
| `phases/deterministic_checks_phase.py` | Phase 2 — Track A (suite/build/typecheck/lint) |
| `phases/synthesis_phase.py` | Phase 3 — Track B (LLM writes tests from criteria) |
| `phases/guard_phase.py` | Phase 4 — trivial-test guard |
| `phases/execute_phase.py` | Phase 5 — run the synthesized file |
| `phases/verdict_phase.py` | Phase 6 — combine into the canonical verdict |
| `tester_tools.py` | gateway-routed sandbox ops (read/write/diff/exec/run) |
| `tester_output.py` | writes the human-readable on-disk tester output file |
| `prompt/tester_prompt.py` | system prompts + test-writing prompt builders |

---

## The currency: `CheckResult` (`results.py`)

Every check — existing suite, build, typecheck, lint, synthesized tests — is the **same
typed record**:

```python
@dataclass
class CheckResult:
    label: str            # evidence slot: unit_tests/integration_tests/build/typecheck/lint
    command: List[str]
    status: CheckStatus
    exit_code: int
    duration_ms: int = 0
    stdout: str = ""; stderr: str = ""; artifact_text: str = ""
    artifact_ref: str = ""              # filled later when persisted to the DB

    @property
    def decides_verdict(self) -> bool:  # only PASSED/FAILED count
        return self.status in (CheckStatus.PASSED, CheckStatus.FAILED)
```

`CheckStatus` separates outcomes that **decide** a microtask's fate from those that must not:

| status | meaning | decides verdict? |
|--------|---------|------------------|
| `PASSED` | ran, exit 0 | yes |
| `FAILED` | ran, real negative (assertion failed, build broke) | yes |
| `SKIPPED` | nothing to verify (no suite, unsupported project) | no |
| `ERRORED` | ran but **harness/infra** problem (collection error, timeout, gateway error) | **no** |

`make_check(label, command, raw_result, *, error_exit_codes=...)` turns a raw sandbox-run
dict into a `CheckResult`:
```
gateway "error" key            → ERRORED   (denied / timeout / exception)
exit 0                         → PASSED
exit in error_exit_codes       → ERRORED   (e.g. pytest 2/3/4 = collection/usage error)
any other non-zero             → FAILED
```
`ERRORED` is the key to **not blaming the Coder** for a broken *test* (e.g. a synthesized
test with a bad import → pytest exit 2 → ERRORED, not FAILED).

---

## Phase-by-phase

### 1. DetectProjectPhase
Finds markers at repo root (`package.json` → javascript; `requirements.txt`/`setup.py`/
`pyproject.toml`/`setup.cfg` → python). Unknown → `ctx.skip = True` (the rest of the
pipeline no-ops, verdict = SKIPPED).

### 2. DeterministicChecksPhase (Track A — primary grounding)
Runs the repo's **own** checks; verdict from exit codes, no LLM. Installs deps first. Each
check is **config-gated** — runs only if the repo opted into that tool:

| check (label) | Python | JS | gates? |
|---|---|---|---|
| suite (`integration_tests`) | `pytest -q` (exit 5 = no tests → SKIPPED; 2/3/4 → ERRORED) | `npm test` (only if a real `test` script) | yes |
| build (`build`) | `py_compile` of the microtask's **target files** | `npm run build` (if a `build` script) | yes |
| typecheck (`typecheck`) | `python -m mypy .` (if `[tool.mypy]`/`mypy.ini`) | `npx tsc --noEmit` (if `tsconfig.json`) | yes |
| lint (`lint`) | `python -m ruff check .` (if `[tool.ruff]`/`ruff.toml`) | `npx eslint .` (if eslint config) | **advisory** |

`mypy`/`ruff` run via `python -m` because the gateway allowlists `python`, not those tools.
Build is scoped to the microtask's target files (not the whole repo) so pre-existing
breakage elsewhere doesn't fail every microtask. Results land in `ctx.track_a_checks`.

### 3. SynthesisPhase (Track B — fallback grounding)
The LLM writes a test file from `microtask.acceptance_criteria` (text **+ `IOExample`s**) —
an input authored by the **Planner**, independent of the Coder, which is what stops the test
from just mirroring (and rubber-stamping) the implementation. Writes to
`tests/test_<id>.py` / `.test.js`, reads it back into `ctx.synth_test_content`.

### 4. TrivialGuardPhase (deterministic, `grounding.py`)
Rejects a synthesized file that can pass without verifying anything: **0 assertions**, all
`@skip`, or empty. Python uses the `ast` (counts `assert` + `assertX`/`pytest.raises`); JS
uses a regex on `expect(`/`assert(`. Sets `ctx.guard_ok`. A rejected guard means there is no
valid Track-B test.

### 5. ExecutePhase
Only runs if a synth file exists **and** the guard passed. Runs `pytest <file>` / `jest
<file>`. Non-0/1 exit → `ERRORED` (harness), logged as "not blaming code". Result →
`ctx.synth_exec_check`.

### 6. VerdictPhase — the decision
Combines everything into the canonical **`unit_tests`** `CheckResult` (the one the Reflector
and node read), plus emits each raw Track-A check for the audit trail.

```python
GATING_LABELS = {"integration_tests", "typecheck", "build"}   # lint excluded (advisory)
gating  = [c for c in ctx.track_a_checks if c.label in GATING_LABELS] + [synth_exec_check?]
deciding = [c for c in gating if c.decides_verdict]            # PASSED/FAILED only
```

| condition | verdict |
|-----------|---------|
| `ctx.skip` (unsupported project) | **SKIPPED** (nothing to verify) |
| any `deciding` is FAILED | **FAILED** (`failed: <labels>`) |
| any `deciding` is PASSED | **PASSED** |
| supported project, nothing decided (all skipped/errored, or guard rejected) | **FAILED "unverified"** |

Two deliberate properties:
- **No silent pass** — a supported project that produced no real signal is `FAILED
  "unverified"`, not green.
- **No misattribution** — `ERRORED`/`SKIPPED` don't decide, so a flaky synth harness error
  is ignored when Track A grounds the microtask (→ PASS), and is reported honestly
  ("unverified — harness error") when it's the only signal.

`lint` is excluded from `gating`, so a lint failure is recorded but **cannot** flip the
verdict.

---

## Handoff to the graph (`tester_node.py`)

`ctx.to_result()` returns `{"check_results": ctx.checks, "summary", "last_test_output_path",
"repo_diff", ...}`. `TesterRunner`:

1. **`_persist_results`** — per check: write a DB artifact, then a `CheckOutput(status,
   exit_code, artifact_ref)` into `evidence.checks` keyed by `label`
   (`unit_tests`→`evidence.checks.unit_tests`, `build`→`...build`, …).
2. **`_verdict_passed`** — reads the one `unit_tests` check:
   ```python
   return check.status in (CheckStatus.PASSED, CheckStatus.SKIPPED)   # skipped = nothing to fix
   ```
3. PASS → mark microtask COMPLETED/DONE, emit `NEEDS_CODE`/`DONE`, set `_tested_microtask_id`
   (so the CODE gate can reset it on reject). FAIL → `REFLECT` → reflector.

Note: `evidence.checks` is a fixed-field Pydantic model (`lint/typecheck/unit_tests/
integration_tests/build`) — that's why check labels must be exactly those names, and why the
**combined** verdict is emitted as `unit_tests`.

---

## Config (env)
| var | purpose |
|-----|---------|
| `CLUSTER_API_KEY` / `OPENAI_API_KEY` | LLM key for the test-writer (Track B) |
| `LLM_MODEL` | model for synthesis (default `google/gemma-4-31B-it`) |
| `TESTER_MAX_TOOL_STEPS` | ReAct rounds for the test-writer (default 10) |
| `TESTER_TIMEOUT_SECONDS` | LLM request timeout (default 120) |
| `LLM_RETRIES` | LLM retry count (default 3) |

Permissions: `TesterPermissions` (`src/tool_gateway/permissions.py`) — `python, pip, pytest,
node, npm, npx, apt-get, grep, ls, cat, find, mkdir, git` + read/write/diff/run tools.

---

## Known limitations (tracked on GitHub)
- Existing suite has no baseline — a pre-existing red fails every microtask (#28).
- Synthesis always runs, even when the existing suite already covers it (#29).
- Guard checks assertions *exist*, not that they *exercise the target*; JS guard is regex (#30).
- On a synth harness error with no Track-A signal, retry synthesis once before "unverified" (#31).
- No dedicated unit tests for `grounding.py` / `VerdictPhase` yet (#32).
- Reflector is failure-only; "always-on judge" is under discussion (#26).
- Coder doesn't compile-check before the Tester (#27).
