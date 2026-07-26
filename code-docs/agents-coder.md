# src/agents/Implementing/Coder/ — Code Explanation

~780 lines across coder.py, coder_tools.py, coder_support.py, coder_output.py,
prompt/coder_prompt.py. Implements ONE microtask per invocation, verified
deterministically.

---

## coder.py (414 lines) — `Coder`

### Why a SINGLE ReAct loop (class docstring, 72–83)
The Planner needs a separate investigate→synthesize split because its final
artifact is structured JSON (json_mode can't interleave tool calls). The Coder's
final artifact **IS the file edits** — the tool calls ARE the work. Grounding
comes from pre-fetched file contents in the prompt (the Runner did that);
verification comes from `repo.diff` after the loop.

### `invoke(state, microtask, file_contents)` (110–152) — the spine
1. Lessons: `find_lessons(scope="code", query = title+description+target_files)`
   + operator rules → one block, computed once (constant across retries),
   excludes this run (same-run reflections arrive via `state.reflections`).
2. `_code_until_diff` — the ReAct/empty-diff loop (below).
3. `_ensure_compiles` — the warm compile gate (below).
4. `_files_from_diff(diff_text)` — touched files parsed **from the actual git
   diff headers ("never from LLM self-report")**.
5. `_enforce_forbidden_paths` — any touched file under a `forbidden_paths` rule
   → `RunExecutionError` (a constraint, enforced deterministically after the
   fact).
6. Debug log dump + `_build_result`.

### `_code_until_diff` (154–259) — the empty-diff retry loop
Up to `coder_max_empty_diff_retries` attempts:
- Build the prompt (`build_coder_prompt`: objective, plan, microtask contract,
  the pre-read file contents, current `reflections` — the fix instructions from
  the reflector or a human code-gate reject — plus `retry_feedback` on a second
  attempt). Lessons block prepended.
- Run the ReAct agent with `recursion_limit = 2*max_tool_steps + 5` (each tool
  round ≈ model step + tools step).
- **`GraphRecursionError` is not fatal**: the model may have written the files
  and then looped verifying — so check the actual diff; if non-empty, the work
  is done ("diff_recovered_after_recursion_limit"). Only an empty diff counts as
  a failed attempt.
- Transient LLM error → `RetriableError` (STALL; the log line even documents the
  retry semantics: client max_retries re-sent the same prompt, `/continue`
  re-drives with the same prompt again).
- After the loop: non-empty diff → return it PLUS the message list (so the
  compile gate can continue the same conversation) and the attempts log.
- Empty diff after all attempts → `RunExecutionError` → run FAILED.
- `retry_feedback` is targeted per failure mode: recursion-loop → "be direct:
  write each target file ONCE, don't call get_repo_diff in a loop"; plain empty
  diff → "no file actually changed — you must call write_sandbox_file".

### `_ensure_compiles` (265–321) — the WARM compile gate (GH #27)
- `_compilable_targets`: buckets targets — `.py` → `python -m py_compile`,
  `.js/.mjs/.cjs` → `node --check`; anything else (md/json/ts) skipped (Tester
  covers those).
- On failure: append a `HumanMessage` with the compiler report **to the same
  message thread** and re-invoke the agent — the coder fixes in-context with all
  its prior reasoning available (no cold re-prompt), up to
  `coder_max_compile_retries`.
- On exhaustion: **proceed with a warning** — "the Tester's deterministic build
  check is the backstop, so we don't hard-fail here." Calibrated severity again.

### `_build_result` (361–412)
Returns the node dict: `status=NEEDS_TEST`, the microtask updated to VERIFYING
with `attempts+1` and `files_touched`, `evidence.latest_diff` (a `Diff` pointing
at the artifact ref the Runner will fill with the diff text), the diff Artifact
for the index — and two important small things:
- `"test_resynth_count": 0` — **new code resets the reflector→tester resynth
  budget** (GH #33; the counter is per-code-version).
- `"_diff_text"` — internal hand-off to the Runner (popped before the dict
  reaches LangGraph).

## coder_tools.py (154 lines) — `CoderTools`

Gateway-routed LangChain tools: `read_sandbox_file`, `write_sandbox_file`,
`get_repo_diff`, `run_sandbox_command`, `semantic_search`. Notables:
- `to_sandbox_path(path)` — normalizes a repo-relative path to the absolute
  `/workspace/repo/...` form (the sandbox path validator requires it); shared
  with the Runner's grounding pre-reads.
- LLM-facing outputs go through `truncate_for_llm`; `get_repo_diff` used by the
  RUNNER for verification reads the raw gateway result (deterministic phases
  never truncate).
- All calls present `Roles.CODER` — so `pytest` is structurally impossible.

## prompt/coder_prompt.py

`CODER_SYSTEM_PROMPT` + `build_coder_prompt(...)`: the full grounding context —
objective, plan summary/architecture/data contracts, the microtask contract
(description/approach/acceptance criteria/target+context files), each pre-read
file's REAL content (or `_MISSING_FILE_SENTINEL` = "you are creating this
file"), reflections (fix instructions from failed attempts), forbidden paths,
and retry feedback. The rules section forbids running tests and demands
complete-file writes (no fragments/ellipses).

## coder_support.py / coder_output.py

- `format_react_transcript(messages)` — human-readable ReAct trace for debug
  files.
- `create_code_file_log(...)` — per-microtask debug dump under `src/output/`:
  every attempt's prompt + transcript, the final diff, reflections, lessons,
  token usage. This file is the forensics source when a coder run goes weird.

---

## Relationships

```
coder_node (Runner) ─► pre-reads target/context files (gateway) ─► Coder.invoke
Coder ReAct ─► CoderTools ─► ToolGateway(Roles.CODER) ─► sandbox writes
verification: coder_tools.get_repo_diff (deterministic) → empty? retry : proceed
compile gate: py_compile / node --check via gateway → warm in-thread fix
result → NEEDS_TEST → tester;  diff text → Runner → artifacts table (kind="diff")
                                            └─ later read by reflector's _diff_cycle_kind
```

Exam sentences: "The coder is never believed — its files_touched come from
parsing the git diff, its syntax is compile-checked, and an empty diff is a
failed attempt no matter what the model said. And the two retry mechanisms are
deliberately different temperatures: empty-diff retries are cold re-prompts,
compile fixes are warm continuations of the same conversation."
