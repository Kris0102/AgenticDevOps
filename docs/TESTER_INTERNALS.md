# Tester Internals — every object, every attribute, every flow

> Companion to `TESTER.md` (concepts, design rationale, interview prep). This one is the
> code-level reference: each class and attribute of the test process, and a walkthrough
> of every path a microtask can take through the Tester. Source of truth: `main` @
> `2c9925a` (post-ADR-0037).
>
> Files covered:
> `src/orchestrator/tester_node.py`, and under `src/agents/Implementing/Tester/`:
> `pipeline.py`, `context.py`, `results.py`, `grounding.py`, `tester_tools.py`,
> `tester_output.py`, `prompt/tester_prompt.py`, `phases/{base,detect_phase,
> deterministic_checks_phase,synthesis_phase,guard_phase,execute_phase,verdict_phase}.py`

---

## Part 1 — The object catalogue

### 1.1 The big picture: who hands what to whom

```
BlackboardState ──▶ TesterRunner.invoke()                 (graph node, owns verdict+persistence)
                        │  builds
                        ▼
                    TestPipeline(run_id, TesterTools, RunConfig)
                        │  constructs once: 6 TestPhase objects + 2 LLM test-writer agents
                        │  per microtask:
                        ▼
                    TestContext  ◀─── every phase reads+writes this one object
                        │  to_result()
                        ▼
                    result: Dict ──▶ TesterRunner._persist_results / _build_state_result
                        │                       │
                        ▼                       ▼
                    DB (artifacts, events,   node-return dict → LangGraph merges
                    microtasks, runs)        into BlackboardState (checkpointed)
```

Two separate "result" shapes exist on purpose:

- **`CheckResult`** (dataclass, `results.py`) — the pipeline-internal currency. Rich:
  full stdout/stderr, command, duration, artifact text.
- **`CheckOutput`** (pydantic, `state.py`) — the blackboard projection. Lean:
  status + exit_code + artifact_ref. The heavy text goes into DB artifacts, not the
  checkpoint; the checkpoint keeps only pointers.

---

### 1.2 `TestContext` (`context.py`) — attribute by attribute

One dataclass instance per Tester invocation (i.e. per microtask attempt). Created by
`TestPipeline.run`, threaded through all six phases, rendered by `to_result()`.
Grouped exactly as the file groups them:

#### Inputs (set at construction, read-only for phases)

| attribute | type | set from | consumed by | purpose |
|---|---|---|---|---|
| `state` | `BlackboardState` | node's state | Synthesis (objective/plan for the prompt), DeterministicChecks (`suite_baseline_failures` from a previous invocation) | read-only window into the checkpointed run state |
| `microtask` | `Microtask` | node's `_select_microtask()` | everything | the unit under test: targets, acceptance criteria, idx |
| `run_id` | `str` | node | all logging + artifact ids | correlation key |
| `final_validation` | `bool` | node: `not _has_remaining_microtasks(...)` | VerdictPhase summary, node routing | true on the LAST unfinished microtask — a PASS then ends the run (`DONE` → deploy) instead of `NEEDS_CODE` |
| `rejection_note` | `Optional[str]` | `state["test_rejection_note"]` | SynthesisPhase (`should_run` + prompt) | the reflector's/human's "this TEST is wrong" note; presence forces a test REWRITE |
| `reflections` | `List[Reflection]` | `state["reflections"]` | Synthesis prompt, debug output file | prior failure diagnoses — context so a rewritten test doesn't repeat a diagnosed mistake |

#### Filled by `DetectProjectPhase`

| attribute | type | values | purpose |
|---|---|---|---|
| `project_type` | `str` | `"python" \| "javascript" \| "unknown"` | selects every downstream branch: which Track A checks, which test-writer agent, which runner |
| `skip` | `bool` | default `False` | `True` = unsupported repo → DeterministicChecks/Synthesis `should_run` False, verdict SKIPPED. The "whole pipeline no-ops" switch |
| `js_test_framework` | `Optional[str]` | `"jest" \| "node:test" \| "vitest" \| "mocha"`, None for python | Track B writes AND runs the synth test in the repo's own framework — the fix for the historical #1 false negative (Jest test in a node:test repo) |

#### Track A (filled by `DeterministicChecksPhase`)

| attribute | type | purpose |
|---|---|---|
| `track_a_checks` | `List[CheckResult]` | labelled `integration_tests`/`build`/`typecheck`/`lint` results — root project AND (ADR 0037) nearest-`package.json` subprojects of the microtask's JS/TS targets. VerdictPhase folds the gating subset into the verdict |
| `suite_baseline_failures` | `Optional[int]` | run-level constant (GH #28/#72): failing-test count of the repo's OWN suite on the **pristine tree**, established once on the first Tester run (via stash → run suite → restore), then carried on the blackboard. `None` until established |
| `suite_current_failures` | `int` | transient, this attempt's failing count. VerdictPhase gates `integration_tests` on the DELTA: `current > baseline` = new regression → gate; `current <= baseline` = inherited breakage → advisory. A FAILED-but-unparseable suite is forced to `baseline + 1` so it still gates |

#### Track B (filled by Synthesis / Guard / Execute)

| attribute | type | writer | purpose |
|---|---|---|---|
| `synth_test_file` | `Optional[str]` | SynthesisPhase | path of the written test (`.agentic_tests/test_<mt>.py` or `.test.js`). `None` = synthesis didn't run this attempt (normal coder-retry reuses the existing file, or untestable/unsupported) |
| `synth_test_content` | `str` | SynthesisPhase (read back from sandbox) | the actual source — input to the guard, persisted as the `test-synth` artifact |
| `synth_untestable_reason` | `str` | SynthesisPhase `should_run` (ADR 0037) | non-empty when synthesis was refused because NO target file is importable by the harness (e.g. `.tsx` under pytest) — flows into the unverified verdict reason so the dashboard says WHY nothing ran |
| `guard_ok` | `bool` (default `True`) | TrivialGuardPhase | `False` = the synth file is trivial (no assertions / all skipped / undeclared JS deps) → ExecutePhase skips it, verdict excludes it |
| `guard_reason` | `str` | TrivialGuardPhase | human-readable guard verdict ("3 assertion(s)" or the rejection reason) — surfaced on artifacts |
| `synth_exec_check` | `Optional[CheckResult]` | ExecutePhase | the synth test's run result (label `unit_tests`), possibly reclassified FAILED→ERRORED (load failure). `None` = never executed |
| `synth_verdict_role` | `str` | **VerdictPhase** (`_synth_role`) | single-source-of-truth sentence for "what role did the synth test play?" ("gated the verdict (passed)" / "advisory — failure ignored" / "excluded — trivial-guard rejected" …) — carried into the `test-synth` artifact so the dashboard never shows a bare status without its meaning |

#### Verdict outputs (filled by `VerdictPhase`)

| attribute | type | purpose |
|---|---|---|
| `checks` | `List[CheckResult]` | THE output: `[combined unit_tests verdict, *track_a_checks]`. The node reads only this |
| `summary` | `str` | one-liner for events/logs: "microtask checks passed." / "… FAILED." / "… UNVERIFIED — passed through without any check deciding." |
| `output_path` | `Optional[str]` | debug dump path (`tester_output.py` writes `src/output/<run>/implementing/tester_microtask_N_iteration_M.txt`) — carried to `state["last_test_output_path"]` for the reflector |
| `repo_diff` | `str` | the coder's diff at verdict time (`tools.get_repo_diff()`, includes intent-to-add so created files show) — reflector input |
| `token_usage` | `TokenUsage` | accumulated LLM spend (only Synthesis spends). Pipeline mirrors it live (`self.token_usage = ctx.token_usage`) inside a `finally`, so a crash mid-phase still lets the node record what was already spent |

#### `TestContext` methods

- **`synth_artifact()`** → payload for the `test-synth` DB artifact. Returns `None` only
  when synthesis didn't run this iteration; if it ran but produced nothing usable, an
  artifact still records that ("the tester tried but produced nothing" stays visible).
  Status resolution order: exec check status → "rejected (trivial-guard)" → "not run".
- **`to_result()`** → the node-facing dict:

```python
{
  "check_results": self.checks,                     # List[CheckResult] → verdict + evidence
  "summary": ..., "final_validation": ...,
  "last_test_output_path": ..., "repo_diff": ...,
  "synth_test": self.synth_artifact(),              # _INTERNAL_ → test-synth artifact
  "suite_baseline_failures": ...,                   # carried back to the blackboard
  "test_rejection_note": None,                      # ALWAYS cleared — a rewrite note must not leak to the next microtask
  "_token_usage": self.token_usage,                 # _INTERNAL_ → popped by the node before the dict touches state
}
```

Keys prefixed/marked `_INTERNAL_` are node-consumed and never become blackboard state —
the node `pop`s `_token_usage` and reads `synth_test` before building the state result.

---

### 1.3 `CheckStatus` + `CheckResult` + factories (`results.py`)

```python
class CheckStatus(str, Enum):
    PASSED  = "passed"
    FAILED  = "failed"    # ran, real negative verdict (assertion failed, build broke)
    SKIPPED = "skipped"   # nothing to verify — not a failure
    ERRORED = "errored"   # harness/infra problem — must NOT decide the verdict
```

The four-way split is the whole point. Two of the states are *evidence*; two are
*absence of evidence* — and the misattribution bug family (blaming the Coder for a
harness problem) is exactly what happens when ERRORED collapses into FAILED.

`CheckResult` fields: `label` (which `evidence.checks` slot: `unit_tests` /
`integration_tests` / `build` / `typecheck` / `lint`), `command`, `status`, `exit_code`,
`duration_ms`, `stdout`, `stderr`, `artifact_text` (pre-rendered body for the DB
artifact), `artifact_ref` (back-filled by the node when persisted).

```python
@property
def decides_verdict(self) -> bool:
    return self.status in (CheckStatus.PASSED, CheckStatus.FAILED)
```

**`make_check(label, command, result, *, error_exit_codes=set())`** — normalizes a raw
gateway run dict into a typed check. Classification order:

1. gateway `error` key (denied / timeout / exception) → ERRORED
2. exit 0 → PASSED
3. exit in `error_exit_codes` → ERRORED (per-caller: pytest `{2,3,4}` collection/usage,
   synth exec `{2,3,4,5,124,137}` incl. watchdog kills)
4. anything else → FAILED

**`skipped_check(label, reason)`** — a SKIPPED result with the reason in `stderr`
(e.g. "no existing pytest suite in repo").

---

### 1.4 `TestPhase` (`phases/base.py`)

```python
class TestPhase(ABC):
    name: str = "phase"
    def should_run(self, ctx) -> bool: return True
    @abstractmethod
    def run(self, ctx) -> TestContext: ...
```

Contract: constructor injects collaborators, `run` is the short entry point, helpers are
`_`-prefixed. `should_run` is how the pipeline stays a flat sequence — a phase opts out
instead of the pipeline growing if-nests. The pipeline logs `tester.phase.enter` /
`tester.phase.skip` per phase, so a run's log shows exactly which phases fired.

### 1.5 `TestPipeline` (`pipeline.py`)

Built ONCE per Tester invocation (in the node). Construction does three things:

1. `build_chat_llm(...)` for the tester's configured provider/model
   (`config.llm.tester`, `tester_timeout_seconds` knob).
2. Creates TWO LangGraph ReAct agents — `python_agent` and `js_agent` — same LLM + same
   `TesterTools.build_tools()`, different system prompts
   (`TESTER_SYSTEM_PROMPT_PYTHON` / `_JS`). `recursion_limit =
   tester_max_tool_steps * 2 + 5` (each tool step = 2 graph nodes + slack).
3. Instantiates the six phases in order.

`run(state, microtask, final_validation)` constructs the `TestContext`, loops
`should_run`/`run`, and keeps `self.token_usage` as a live mirror of `ctx.token_usage`
(also in a `finally`) — the crash-path token accounting hook. Returns `ctx.to_result()`.
The pipeline explicitly does NOT route: "it produces the node-facing result dict; it
does NOT decide routing."

### 1.6 `TesterTools` (`tester_tools.py`)

Every side effect goes through the **ToolGateway** with `Roles.TESTER` — allowlisted +
audited (`tool.executed` / `tool.denied` events). Two API tiers:

**LLM-facing tools** (`build_tools()` → given to the ReAct test-writer):

| tool | wraps | notes |
|---|---|---|
| `read_sandbox_file` | `FS_READ_FILE` | output capped `truncate_for_llm(…, MAX_FILE_READ_CHARS=8000)` — an uncapped read once cost a tester call ~245k tokens (compounds quadratically in a ReAct loop) |
| `write_sandbox_file` | `FS_WRITE_FILE` | how the test file lands in the sandbox |
| `execute_in_sandbox` | `SANDBOX_RUN` | `shlex.split`, no shell/pipes; stdout+stderr truncated |
| `semantic_search` | pgvector index | locate code under test without blind reads |

Deliberately **not** exposed: `get_repo_diff` — the writer doesn't need the diff to write
tests from criteria, and exposing it invites a verify-loop that burns the tool budget
(the trap the Coder hit).

**Deterministic-phase API** (never LLM-facing, never truncated):

- `run(argv, timeout_s=300)` → raw gateway dict (`exit_code/stdout/stderr/duration_ms`).
  Used by detect/install/suite/build/guard/execute. Full output is preserved (parsed
  programmatically + stored in artifacts).
- `get_repo_diff()` → runs `git add -N .` first (intent-to-add) so newly CREATED files
  appear in the diff — without it a create-only microtask looks like "the Coder did
  nothing".

Path handling: `to_sandbox_path` maps repo-relative paths (how the Planner declares
`target_files`) onto `/workspace/repo/...` absolutes the FS tools need.

### 1.7 `TesterRunner` (`orchestrator/tester_node.py`)

The graph node. Responsibilities in `invoke()` order:

1. **`_select_microtask()`** — prefer `state["current_microtask_id"]` if unfinished
   (i.e. the one the Coder just worked); else the lowest-`idx` microtask in
   {IN_PROGRESS, EXECUTING, VERIFYING, PENDING}; `None` → `_finish_run()`.
2. **`final_validation`** = no OTHER unfinished microtask exists.
3. **Token-budget guard** (ADR 0025): raises `TokenBudgetExceededError` → run STALLS
   (recoverable via `/continue`), never FAILED.
4. **Build the pipeline** (fresh `ToolGateway` with a search-only `ContextEngine`) and
   run it.
5. **Crash taxonomy** (each records crash tokens first via `_record_crash_usage`):
   - `RunExecutionError` → `RunFailureHandler.handle` → terminal FAILED (deterministic,
     proven-unrecoverable errors only)
   - `RetriableError` → re-raise → `_drive` marks STALLED
   - anything else → transient-LLM check (`is_transient_llm_error` → wrap in
     `RetriableError` → STALLED) else re-raise → STALLED. Generic crashes are *never*
     terminal (ADR 0021).
6. **`record_usage`** — `_token_usage` popped from the result, written to `llm.usage`
   event + `Iteration.tokens_used_total` (tagged with the microtask id).
7. **`_verdict_passed`** — the single deterministic read:

```python
for check in result.get("check_results") or []:
    if check.label == "unit_tests":
        return check.status in (CheckStatus.PASSED, CheckStatus.SKIPPED)
return False   # missing check = fail safe → reflector
```

8. **`_persist_results`** — per check: DB artifact (`kind=f"test-{label}"`, full
   command/status/output text) + `CheckOutput` (status, exit_code, artifact_ref) merged
   into `evidence.checks`; the `test-synth` artifact if synthesis ran; on PASS the
   microtask flips to COMPLETED (or DONE when final) in state AND its `MicrotaskRecord`
   row; `Run.status` updated; `microtask.completed|failed` + `run.status_changed`
   (+ `run.completed` when final) events.
   On PASS it also calls `_record_code_lesson` — the **outcome-validated lesson write**
   (ADR 0018): the latest Reflection for this now-green microtask is confirmed correct,
   so it's persisted to the per-repo lesson store for future runs.
9. **`_build_state_result`** — the node-return dict:
   - FAIL → `{"status": REFLECT, "current_microtask_id": <kept in flight>, …}`
   - PASS → `{"status": NEEDS_CODE|DONE, "microtasks": <updated>,
     "current_microtask_id": None, "_tested_microtask_id": <id>, …}` — the
     `_tested_microtask_id` key is consumed by `HitlGate.apply(Phase.CODE)` so a human
     gate-reject can reset exactly this microtask.
   - both paths carry: `last_test_output_path`, `test_rejection_note: None` (cleared),
     `evidence`, `suite_baseline_failures`.

### 1.8 The Planner→Tester grounding contract (`state.py`)

```python
class IOExample(BaseModel):
    given: Dict[str, JSONValue]   # inputs
    expect: JSONValue             # expected result — planner-authored, concrete

class AcceptanceCriterion(BaseModel):
    text: str                     # the observable behaviour, in prose
    examples: List[IOExample]     # optional concrete cases
    def render(self) -> str: ...  # "text  (e.g. given {...} → expect ...)"
```

These live on `Microtask.acceptance_criteria` and are THE input Track B synthesizes
from. The trust rule downstream: a criterion **with** examples can be asserted on
exactly (grounded → the synth test may gate); a criterion with prose only gets
structure-level assertions (ungrounded → advisory). `Microtask.test_strategy` is an
optional extra hint for the writer.

### 1.9 Blackboard evidence objects (`state.py`)

```python
class CheckOutput(BaseModel):   # per-check projection on the blackboard
    status: str; exit_code: int; artifact_ref: str

class Evidence(BaseModel):
    checks: Checks              # fixed fields: lint/typecheck/unit_tests/integration_tests/build
    latest_diff: ...            # (coder-owned)
```

Fixed fields — not a list — which is WHY VerdictPhase must fold everything into
`unit_tests`: downstream consumers (node routing, reflector, dashboard) read one slot.

### 1.10 `grounding.py` — the pure functions

No LLM, no I/O; deterministic by construction:

| function | mechanism | used by |
|---|---|---|
| `count_python_assertions` | `ast` walk: `Assert` nodes + calls named `assert*` / `raises`/`warns`/`approx` | guard |
| `python_all_skipped` | all `test*` functions carry a decorator whose dump contains "skip" | guard |
| `count_js_assertions` | regex `\b(?:expect|assert)\s*(?:\.\s*\w+)*\s*\(` — bare AND dotted (`assert.deepEqual(`) forms | guard |
| `js_import_specifiers` / `js_dependency_specifiers` | 5 regexes over import/require/export-from/dynamic-import; bare specifiers only (relative + `node:` excluded) | guard dep-check |
| `js_package_name` | specifier → package root (`@scope/pkg/x` → `@scope/pkg`) | guard dep-check |
| `js_declared_dependency_names` | package.json dependencies/devDeps/peer/optional keys | guard dep-check |
| `is_trivial(project_type, source)` | empty file / all-skipped / zero assertions → `(True, reason)` | guard |

### 1.11 Prompts (`prompt/tester_prompt.py`)

- `TESTER_SYSTEM_PROMPT_PYTHON` / `_JS` — role framing for the two writer agents;
  the JS one demands "the project's OWN test framework (named in the task prompt)".
- `build_test_writing_prompt_python/_js(objective, plan, microtask, test_file,
  rejection_note, reflections, [framework])` — the per-microtask task prompt: renders
  acceptance criteria (via `render()`, inlining IOExamples), value-grounding rules
  (STRUCTURE ONLY without examples), the target file path, and on a rewrite the
  rejection note + prior reflections.
- `_JS_FRAMEWORK_GUIDE` — per-framework block (exact import + assertion idiom for
  node:test / jest / vitest / mocha) selected by `ctx.js_test_framework`.

### 1.12 `tester_output.py` — debug dump

`create_test_file_as_output(...)` writes
`src/output/<date>_<run8>/implementing/tester_microtask_N_iteration_M.txt` — header
(run, tokens, rewrite flag), the synth test source, the combined check result. Pure
debugging/audit convenience (the durable copies are DB artifacts); its path is carried
into state as `last_test_output_path`.

---

## Part 2 — Every flow, through the code

Notation: `→` = call/data flow. All flows start the same way, so:

**Common prologue (every flow).** The actual entry code:

```python
# orchestrator/tester_node.py — TesterRunner.invoke()
def invoke(self) -> Dict[str, Any]:
    microtask = self._select_microtask()
    if microtask is None:
        return self._finish_run()                                    # → Flow 13

    final_validation = not self._has_remaining_microtasks(microtask.microtask_id)

    with db.session_scope() as db_session:
        iteration: Iteration = self.state.get("iteration")
        constraints: Constraints = self.state.get("constraints")
        if token_budget_exceeded(iteration, constraints):            # → Flow 11
            raise TokenBudgetExceededError(phase="test", ...)

        pipeline = TestPipeline(run_id=self.run_id, tester_tools=tester_tools, config=config)
        result = pipeline.run(self.state, microtask, final_validation=final_validation)
```

Microtask selection — prefer the one the Coder just worked, else lowest unfinished idx:

```python
def _select_microtask(self) -> Optional[Microtask]:
    current_id = self.state.get("current_microtask_id")
    if current_id and current_id in by_id and by_id[current_id].status not in _FINISHED:
        return by_id[current_id]
    for mt in sorted(microtasks, key=lambda item: item.idx):
        if mt.status in {IN_PROGRESS, EXECUTING, VERIFYING, PENDING}:
            return mt
    return None
```

And the pipeline's phase loop — `should_run` is the only branching mechanism:

```python
# pipeline.py — TestPipeline.run()
ctx = TestContext(state=state, microtask=microtask, run_id=self.run_id,
                  final_validation=final_validation,
                  rejection_note=state.get("test_rejection_note"),
                  reflections=list(state.get("reflections") or []))
try:
    for phase in self.phases:
        if phase.should_run(ctx):
            _log.info("tester.phase.enter", run_id=self.run_id, phase=phase.name)
            ctx = phase.run(ctx)
            self.token_usage = ctx.token_usage   # live snapshot for the crash path
        else:
            _log.info("tester.phase.skip", run_id=self.run_id, phase=phase.name)
finally:
    self.token_usage = ctx.token_usage
return ctx.to_result()
```

### Flow 1 — Happy path, Python repo, mid-run microtask

**1. Detect** — root marker probes, literally `find`:

```python
def _detect(self) -> str:
    js = self.tools.run(["find", ".", "-maxdepth", "1", "-name", "package.json"], timeout_s=10)
    if js.get("stdout", "").strip():
        return SupportedProjectTypes.JAVASCRIPT.value
    py = self.tools.run(["find", ".", "-maxdepth", "1",
         "-name", "requirements.txt", "-o", "-name", "setup.py",
         "-o", "-name", "pyproject.toml", "-o", "-name", "setup.cfg"], timeout_s=10)
    if py.get("stdout", "").strip():
        return SupportedProjectTypes.PYTHON.value
    return SupportedProjectTypes.UNKNOWN.value
```

→ `project_type="python"`, `skip=False`.

**2. DeterministicChecks** — the python branch of `run()`:

```python
if ctx.project_type == "python":
    self._install_python_deps()               # once per run (marker dir caches)
    self._establish_baseline(ctx, "python")   # pristine suite count, first run only
    ctx.track_a_checks = self._compact([
        self._py_suite(),      # pytest — SKIPPED if exit 5 (no tests collected)
        self._py_build(ctx),   # py_compile of the MICROTASK'S OWN targets (gating)
        self._py_typecheck(),  # mypy, only if [tool.mypy]/mypy.ini present (gating)
        self._py_lint(),       # ruff, only if configured (advisory)
    ])
...
ctx.track_a_checks += self._subproject_js_checks(ctx)   # no JS targets → []
ctx.suite_current_failures = self._current_suite_failures(ctx)
```

The suite check with its two escape hatches:

```python
def _py_suite(self) -> CheckResult:
    cmd = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--ignore=.agentic_tests"]
    result = self.tools.run(cmd, timeout_s=300)
    if int(result.get("exit_code", 1)) == _PYTEST_NO_TESTS:        # exit 5
        return skipped_check("integration_tests", "no existing pytest suite in repo")
    return make_check("integration_tests", cmd, result, error_exit_codes={2, 3, 4})
```

Baseline establishment (only when `state["suite_baseline_failures"]` is None — full
mechanics quoted in Flow 8).

**3. Synthesis** — `should_run` says yes (first time), then:

```python
builder = build_test_writing_prompt_js if ctx.project_type == "javascript" \
          else build_test_writing_prompt_python
agent   = self.js_agent if ctx.project_type == "javascript" else self.python_agent
prompt  = builder(objective=ctx.state.get("objective"), plan=ctx.state.get("plan"),
                  microtask=ctx.microtask, test_file=test_file,
                  rejection_note=ctx.rejection_note, reflections=ctx.reflections)
agent_result = agent.invoke({"messages": [HumanMessage(content=prompt)]},
                            config={"recursion_limit": self.recursion_limit})
ctx.token_usage = ctx.token_usage + sum_usage(agent_result.get("messages", []))
...
ctx.synth_test_file = test_file                                  # .agentic_tests/test_<mt>.py
ctx.synth_test_content = self.tools.read_sandbox_file(test_file) # read BACK what was written
```

Note the read-back: the guard judges what actually landed on disk, not what the LLM
claims it wrote.

**4. Guard** — `is_trivial("python", source)`:

```python
if project_type == "python":
    if python_all_skipped(source):
        return True, "every test is @skip-decorated — nothing executes"
    n = count_python_assertions(source)      # ast walk: Assert nodes + assertX/raises calls
    if n == 0:
        return True, "no assertions found (no `assert` and no assertX/pytest.raises calls)"
    return False, f"{n} assertion(s)"
```

→ `guard_ok=True`, `guard_reason="4 assertion(s)"`.

**5. Execute**:

```python
cmd = ["python", "-m", "pytest", "-q", ctx.synth_test_file]
check = make_check("unit_tests", cmd, self.tools.run(cmd, timeout_s=60),
                   error_exit_codes={2, 3, 4, 5, 124, 137})   # 124/137 = watchdog kill
```

exit 0 → PASSED → `ctx.synth_exec_check`.

**6. Verdict** — the whole decision, verbatim:

```python
gating: List[CheckResult] = [
    c for c in ctx.track_a_checks
    if c.label in self.GATING_LABELS and not is_inherited_suite_failure(c)
]                                             # GATING_LABELS = {integration_tests, typecheck, build}
if ctx.synth_exec_check and (self._synth_gates(ctx)
                             or ctx.synth_exec_check.status == CheckStatus.PASSED):
    gating.append(ctx.synth_exec_check)       # grounded → always; ungrounded → only its PASS
deciding = [c for c in gating if c.decides_verdict]           # PASSED/FAILED only

if ctx.skip:
    status, reason = CheckStatus.SKIPPED, "unsupported project type (not Python or JavaScript)"
elif any(c.status == CheckStatus.FAILED for c in deciding):
    status, reason = CheckStatus.FAILED, f"failed: {labels}"
elif any(c.status == CheckStatus.PASSED for c in deciding):
    status, reason = CheckStatus.PASSED, "checks passed"
else:
    status, reason = CheckStatus.SKIPPED, self._unverified_reason(ctx, gating)  # Flow 5
```

Here: suite PASSED + build PASSED (+ synth PASSED) → **PASSED**. Then
`ctx.checks = [unit, *ctx.track_a_checks]`, `repo_diff` captured, debug file written.

**7. Node persistence + routing**:

```python
passed = self._verdict_passed(result)                 # True
next_status = self._next_status(passed, final_validation)
# def _next_status: not passed → REFLECT; passed → DONE if final else NEEDS_CODE

# on PASS: microtask flips in state (model_copy — never mutate in place)
updated_microtasks = [mt.model_copy(update={"status": completed_status})
                      if mt.microtask_id == microtask.microtask_id else mt
                      for mt in updated_microtasks]
```

Each check → one DB artifact + one lean `CheckOutput` on the blackboard:

```python
for check in checks:
    artifact_ref = self._store_check_artifact(db_session, microtask.microtask_id, check, result, now)
    check_updates[check.label] = CheckOutput(status=check.status.value,
                                             exit_code=check.exit_code,
                                             artifact_ref=artifact_ref)
new_evidence = evidence.model_copy(update={
    "checks": evidence.checks.model_copy(update=check_updates)})
```

Return dict (PASS branch of `_build_state_result`):

```python
return {
    "status": self._next_status(passed, final_validation),   # NEEDS_CODE here
    "microtasks": updated_microtasks,
    "current_microtask_id": None,
    "_tested_microtask_id": microtask.microtask_id,   # for a CODE-gate reject (Flow 12)
    **shared,   # last_test_output_path, test_rejection_note: None, evidence, suite_baseline_failures
}
```

**8. Graph** — the node wrapper applies the gate BEFORE routing:

```python
# orchestrator.py — _tester node wrapper
if result.get("_tested_microtask_id"):
    return HitlGate.apply(Phase.CODE, result, state)   # gate on → NEEDS_HITL_APPROVAL
```

then `_tester_router`: NEEDS_HITL_APPROVAL → hitl; NEEDS_CODE → coder; DONE → deploy;
REFLECT → reflector.

### Flow 2 — Happy path, FINAL microtask

Same until step 7: `final_validation=True` (no other unfinished microtask) → status
**DONE**, microtask → `MicrotaskStatus.DONE`, extra `run.completed` event. Router:
DONE → `deploy` node (every DeployMode passes the gate, ADR 0036) → deploy gate →
`deploy_push` executes the run's mode (push / PR / local export / dry-run no-op).

### Flow 3 — Real test failure → Reflector → coder retry

1. Steps 1–5 as Flow 1, but Execute exits 1 with a real assertion failure → synth check
   **FAILED**; suppose Track A also fine (suite PASSED).
2. **Verdict** — is the synth allowed to gate? The trust rule, verbatim:

```python
@staticmethod
def _synth_gates(ctx: TestContext) -> bool:
    if not ctx.guard_ok:
        return False
    return any(c.examples for c in ctx.microtask.acceptance_criteria)
```

Guard passed AND ≥1 criterion carries `IOExample`s → gates → its FAILED lands in
`deciding` → `any FAILED` branch → `unit_tests` = **FAILED**, reason "failed: unit_tests".
(Ungrounded, its FAIL would never enter `gating` — Flow 5.)

3. **Node** — FAIL branch of `_build_state_result`:

```python
if not passed:
    return {
        "status": RunStatus.REFLECT,
        "current_microtask_id": microtask.microtask_id,   # KEPT in flight for the retry
        **shared,
    }
```

`microtask.failed` event emitted; microtask status stays IN_PROGRESS; no completion.

4. **Graph**: `_tester_router` REFLECT → reflector. Reflector phase-1 investigates
   (read-only tools), phase-2 diagnoses with: acceptance criteria, the diff
   (`_elide_middle`, 12k), test output, the synth test source, prior diagnoses. Emits a
   `Reflection` with `fix_target`.
5. `fix_target="code"` → NEEDS_CODE → coder retries (attempt++; `_check_stop_limit`
   FAILs the run at `max_microtask_attempts`). Next tester invocation reuses the SAME
   test — the no-moving-target rule in `SynthesisPhase.should_run`:

```python
if ctx.rejection_note:
    return True                       # explicit rewrite requested (Flow 4)
return not self._test_exists(ctx)     # first time only — retry reuses the file

def _test_exists(self, ctx) -> bool:
    test_file = _test_file_path(ctx.microtask.microtask_id, ctx.project_type)
    r = self.tools.run(["find", test_file], timeout_s=10)
    return int(r.get("exit_code", 1)) == 0 and bool(r.get("stdout", "").strip())
```

Loop until PASS (→ Flow 1 step 7, plus `_record_code_lesson` persists the now-confirmed
diagnosis to the per-repo lesson store) or attempts exhausted (run FAILED).

### Flow 4 — Reflector says the TEST is wrong (`fix_target="test"`)

1. Reflector (Flow 3 step 4) judges the synth assertion contradicts the criteria →
   `fix_target="test"` → status **NEEDS_TEST** + `test_rejection_note` set on state,
   bounded by `test_resynth_count < REFLECTOR_MAX_TEST_RESYNTH` (then forced to the code
   path; counter resets when the coder runs).
2. Graph routes REFLECTOR → **tester** (not coder). New invocation:
   `ctx.rejection_note` non-empty → Synthesis `should_run` → True (explicit rewrite) →
   prompt includes the rejection note + reflections → NEW test file overwrites the old.
3. Guard/Execute/Verdict as usual. `to_result()` clears `test_rejection_note`
   (always `None` out) so the note never leaks to another microtask.

### Flow 5 — Nothing can decide: the UNVERIFIED pass-through (ADR 0037)

Trigger variants, all ending in the same verdict branch:

- synthesis refused (untestable targets → Flow 6),
- guard rejected the synth file and Track A produced only SKIPPED checks,
- synth ERRORED (load failure / watchdog) and Track A silent,
- synth FAILED but ungrounded (no IOExamples) and Track A silent.

Code path in `VerdictPhase._combined_unit_check`: `deciding` is empty (`ctx.skip` False,
no FAILED, no PASSED) → else-branch:

```python
status, reason = CheckStatus.SKIPPED, self._unverified_reason(ctx, gating)
_log.warning("tester.verdict_unverified", ...)
```

The reason selection — precedence order IS the code order:

```python
@staticmethod
def _unverified_reason(ctx: TestContext, gating: List[CheckResult]) -> str:
    tail = " — passing through UN-VERIFIED; review the diff at the gate"
    if ctx.synth_untestable_reason:
        return f"unverified — synthesis skipped: {ctx.synth_untestable_reason}; no other check decided{tail}"
    if ctx.synth_test_file and not ctx.guard_ok:
        return f"unverified — synthesized tests were rejected ({ctx.guard_reason}) and no other check decided{tail}"
    if ctx.synth_exec_check and ctx.synth_exec_check.status == CheckStatus.ERRORED:
        return f"unverified — synthesized tests could not run (harness error) and no other check decided{tail}"
    if (ctx.synth_exec_check and ctx.synth_exec_check.status == CheckStatus.FAILED
            and not VerdictPhase._synth_gates(ctx)):
        return ("unverified — the synthesized test failed but is advisory (no concrete "
                f"IOExamples to gate on) and no other check decided{tail}")
    return f"unverified — no runnable verification (no suite, no build, nothing synthesized){tail}"
```

And the summary distinction (unsupported-repo skip vs THIS-change-unverified):

```python
if unit.status == CheckStatus.SKIPPED:
    if ctx.skip:
        return f"{scope} skipped."
    return f"{scope} UNVERIFIED — passed through without any check deciding."
```
Node: SKIPPED counts as passed → microtask COMPLETED → advance. **No reflector** — there
is no failing evidence to diagnose. The human gate is the backstop.
(History: this used to be FAILED and deterministically bricked run `27198e26`.)

### Flow 6 — Untestable targets (synthesis refuses)

The refusal, verbatim:

```python
_TESTABLE_EXTS = {"python": (".py",), "javascript": (".js", ".mjs", ".cjs")}
_VITEST_EXTS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")

def _untestable_reason(self, ctx: TestContext) -> str:
    targets = ctx.microtask.target_files or []
    if not targets:
        return ""    # nothing declared — let synthesis try from the criteria alone
    exts = self._TESTABLE_EXTS[ctx.project_type]
    if ctx.project_type == "javascript" and ctx.js_test_framework == "vitest":
        exts = self._VITEST_EXTS          # vitest loads TS/TSX natively
    if any(f.endswith(exts) for f in targets):
        return ""                          # ONE testable target is enough
    return (f"no target file is testable by the {ctx.project_type} harness "
            f"(targets: {', '.join(targets)})")

# in should_run:
untestable = self._untestable_reason(ctx)
if untestable:
    ctx.synth_untestable_reason = untestable
    _log.info("tester.synth_skipped_untestable", ...)
    return False
```

Guard/Execute then skip too (`should_run` keys on `synth_test_file`, which stays None).
Verdict → Flow 5's first `_unverified_reason` branch — UNLESS Track A decided something
(e.g. the ADR 0037 subproject build): then a real PASSED/FAILED exists in `deciding` and
the unverified branch never fires.

### Flow 7 — Harness errors (load failure / hang)

**Load failure** — exit 1 is ambiguous (assertion failure and import crash both exit 1
under `node --test`), so ExecutePhase reads the TEXT:

```python
_BROWSER_GLOBALS = ("document", "window", "navigator", "localStorage", "sessionStorage",
                    "HTMLElement", "customElements", "alert")
_MODULE_LOAD_MARKERS = ("Cannot find module", "Cannot find package", "ERR_MODULE_NOT_FOUND",
                        "ERR_REQUIRE_ESM", "SyntaxError:")

def _load_error_reason(output: str) -> Optional[str]:
    for marker in _MODULE_LOAD_MARKERS:
        if marker in output:
            return marker
    for g in _BROWSER_GLOBALS:
        if f"{g} is not defined" in output:
            return f"{g} is not defined (no DOM/browser env under the test runner)"
    return None

# after make_check:
if check.status == CheckStatus.FAILED:
    reason = _load_error_reason(f"{check.stdout}\n{check.stderr}")
    if reason:
        check.status = CheckStatus.ERRORED      # harness error, not blaming code
```

Scoped by the fixed lists on purpose — a genuine `myVar is not defined` in the CODE
matches neither list and stays FAILED.

**Hang**: sandbox watchdog (`timeout --kill-after=5` wraps every exec) kills → exit
124/137 → member of `error_exit_codes={2,3,4,5,124,137}` → ERRORED in `make_check`.

**Gateway error** (denied command, exec exception): `"error" in result` → ERRORED.

**Runner selection** for the exec itself — framework-matched, one file, explicit:

```python
@staticmethod
def _js_command(ctx: TestContext) -> list[str]:
    f = ctx.synth_test_file or ""
    framework = ctx.js_test_framework or "node:test"
    if framework == "jest":
        return ["npx", "jest", "--runTestsByPath", f, "--no-coverage"]
    if framework == "vitest":
        return ["npx", "vitest", "run", f]
    if framework == "mocha":
        return ["npx", "mocha", f]
    return ["node", "--test", f]   # built in, no dependency
```

Downstream is always the same: ERRORED never enters `deciding` → Track A carries; Track
A silent → Flow 5.

### Flow 8 — Repo ships a red suite (baseline delta)

**Baseline establishment** (first Tester run only) — stash the Coder's work, measure the
pristine tree, restore:

```python
def _pristine_suite_failures(self, lang: str) -> int:
    push = self.tools.run(["git", "stash", "push", "-u", "-m", "agentic-suite-baseline"], timeout_s=60)
    if int(push.get("exit_code", 1)) != 0:
        return 0          # degrade to "gate normally" — never a false 'inherited' that hides regressions
    stashed = "No local changes to save" not in push_out
    try:
        suite = self._py_suite() if lang == "python" else self._js_suite()
        return _parse_failing_count(suite.stdout) if suite.status == CheckStatus.FAILED else 0
    finally:
        if stashed:
            self.tools.run(["git", "checkout", "--", "."], timeout_s=30)   # drop what the run touched
            self.tools.run(["git", "clean", "-fd", "--", "."], timeout_s=30)
            self.tools.run(["git", "stash", "pop"], timeout_s=60)          # restore the Coder's changes
```

Failing-count parsing (jest prints two "N failed" lines — prefer the test-level one):

```python
_JEST_FAILED_RE = re.compile(r"Tests:.*?(\d+)\s+failed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")

def _current_suite_failures(ctx) -> int:
    suite = next((c for c in ctx.track_a_checks if c.label == "integration_tests"), None)
    if not suite or suite.status != CheckStatus.FAILED:
        return 0
    n = _parse_failing_count(suite.stdout)
    return n if n > 0 else (ctx.suite_baseline_failures or 0) + 1   # unparseable → forced ABOVE baseline (gates)
```

**The delta gate** in VerdictPhase:

```python
baseline = ctx.suite_baseline_failures or 0
new_regression = ctx.suite_current_failures > baseline

def is_inherited_suite_failure(c: CheckResult) -> bool:
    return (c.label == "integration_tests"
            and c.status == CheckStatus.FAILED
            and not new_regression)
# → excluded from `gating` when inherited; artifact marks it
#   "advisory — 2 failing ≤ pristine baseline 2 (GH #72)"
```

So: repo ships 2 red tests → baseline 2 → suite FAILs every run with 2 → `2 > 2` false →
excluded → other checks decide. Coder breaks a third → `3 > 2` → gates → FAILED →
reflector, with the suite output as evidence.

### Flow 9 — JS/TS subproject microtask (ADR 0037)

Microtask targets `dashboard/src/pages/PastRuns.tsx` in a python-rooted repo.

1. Detect → python (root markers). Track A python checks: no `.py` targets → `_py_build`
   None; suite runs as usual.
2. `_subproject_js_checks` — the whole mechanism:

```python
_JS_EXTS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")

def _subproject_js_checks(self, ctx: TestContext) -> List[CheckResult]:
    dirs = sorted({
        d for f in (ctx.microtask.target_files or [])
        if f.endswith(self._JS_EXTS)
        for d in [self._nearest_pkg_dir(self._repo_relative(f))]
        if d and d != "."                      # root is the root branches' job
    })
    checks = []
    for d in dirs:
        self._install_subproject_deps(d)       # npm --prefix d install, cached by node_modules presence
        check = self._subproject_build_check(d)
        if check:
            checks.append(check)
    return checks

def _nearest_pkg_dir(self, path: str) -> Optional[str]:
    parts = path.split("/")[:-1]               # ancestors of the file
    while parts:
        d = "/".join(parts)
        probe = self.tools.run(["find", f"{d}/package.json", "-maxdepth", "0"], timeout_s=10)
        if probe.get("stdout", "").strip():
            return d
        parts.pop()                            # walk one level up
    return None

def _subproject_build_check(self, d: str) -> Optional[CheckResult]:
    if self._npm_has_script("build", pkg_dir=d):
        cmd = ["npm", "--prefix", d, "run", "build"]          # e.g. tsc -b && vite build
        return make_check("build", cmd, self.tools.run(cmd, timeout_s=600))
    if <d has tsconfig.json>:
        cmd = ["npx", "--yes", "tsc", "--noEmit", "-p", d]    # bare typecheck fallback
        return make_check("typecheck", cmd, self.tools.run(cmd, timeout_s=300))
    return None
```

`_repo_relative` strips `/workspace/repo/` so Planner-declared absolute paths work:
`"/workspace/repo/dashboard/src/pages/PastRuns.tsx"` → walk probes
`dashboard/src/pages/`, `dashboard/src/`, `dashboard/` → hit.

3. Synthesis: `.tsx` untestable under pytest → refused (Flow 6).
4. Verdict: the subproject `build` check DECIDES (label `build` ∈ GATING_LABELS) —
   compile-clean → PASSED; type error → FAILED → reflector with real tsc output to
   diagnose. The unverified branch is only reached if the subproject had no build script
   AND no tsconfig.
5. Build outputs (`dist/`, `node_modules/`, `*.tsbuildinfo`) stay in the sandbox and are
   git-ignored → never in the fileset/diff/push.

### Flow 10 — Unsupported repo

Detect finds neither package.json nor python markers → `project_type="unknown"`,
`ctx.skip=True`. DeterministicChecks/Synthesis `should_run` False; Verdict first branch:
`unit_tests` = SKIPPED "unsupported project type (not Python or JavaScript)" → node
passes it through (skipped = passed). Distinct from Flow 5's label on purpose: "this
repo can never be verified" vs "this change couldn't be".

### Flow 11 — Crashes and budget

- **Token budget exceeded** (before any work): `TokenBudgetExceededError` → run STALLED
  (`stalled_kind="token_budget"`), human raises the cap via `/continue`.
- **LLM transient** (429/timeout/5xx) anywhere in the pipeline: caught in the node's
  generic handler → `is_transient_llm_error` → `RetriableError` → STALLED. Crash tokens
  recorded first (`_record_crash_usage` reads `pipeline.token_usage`, the live mirror).
- **`RunExecutionError`** (deterministic, proven-unrecoverable) →
  `RunFailureHandler.handle` → terminal FAILED (+ lesson retraction).
- **Anything else** → re-raised → `_drive` marks STALLED (checkpoint intact,
  `/continue`-able). Generic crash is never terminal (ADR 0021).

### Flow 12 — Human overrides at the CODE gate

PASS + `code ∈ hitl_checkpoints` → `HitlGate.apply(Phase.CODE, result, state)` (in the
node wrapper, keyed on `_tested_microtask_id`) → NEEDS_HITL_APPROVAL → graph suspends.
- **Approve** → resume with the tester's intended status (NEEDS_CODE → next microtask /
  DONE → deploy).
- **Reject (note required)** → the tested microtask is reset to IN_PROGRESS and a
  `Reflection` carrying the human note is seeded directly (no reflector LLM call) →
  coder retry with the human's feedback.

### Flow 13 — No microtasks left

`_select_microtask` returns None (all COMPLETED/DONE — e.g. after a resume replays the
tester) → `_finish_run()`: Run.status → DONE, `run.status_changed` + `run.completed`
events, `{"status": DONE, "current_microtask_id": None}` → router → deploy.

---

## Part 3 — Quick reference: state in / state out

**Reads from `BlackboardState`:** `run_id`, `microtasks`, `current_microtask_id`,
`iteration` + `constraints` (token budget), `config` (LLM), `repo` (gateway/lessons),
`evidence` (merged into), `reflections`, `test_rejection_note`,
`suite_baseline_failures`, `status` (event bookkeeping).

**Writes (node-return dict):** `status` (REFLECT / NEEDS_CODE / DONE /
NEEDS_HITL_APPROVAL via gate), `microtasks` (completion statuses), `current_microtask_id`
(kept on FAIL, cleared on PASS), `evidence` (checks + artifact refs),
`last_test_output_path`, `test_rejection_note` (always cleared),
`suite_baseline_failures`, `iteration` (token totals), `_tested_microtask_id`
(gate-internal).

**Writes (DB):** artifacts `test-unit_tests` / `test-integration_tests` / `test-build` /
`test-typecheck` / `test-lint` / `test-synth`; `microtasks` row status; `runs` row
status; events `microtask.completed|failed`, `run.status_changed`, `run.completed`,
`llm.usage`, plus gateway `tool.executed` for every command; lessons (on confirmed PASS
after a reflection).
