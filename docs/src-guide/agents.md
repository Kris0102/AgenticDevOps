# src/agents/ — The Five LLM Agents

**Role in the MAS:** the "workers". Each agent is a self-contained class with one
entrypoint `invoke(state) -> Dict[str, Any]` that reads the blackboard, does its
cognitive job (usually a ReAct loop over gateway-routed tools), and returns only the
state keys it changed. Agents never talk to each other and never touch the
environment directly — every file read, shell command, and search goes through the
`ToolGateway` under that agent's role.

## Layout

```
agents/
  Planning/
    Planner/        # investigate repo → synthesize structured plan of microtasks
    Reviewer/       # critic: independently verify the plan, approve/reject
  Implementing/
    Coder/          # implement ONE microtask, verified by deterministic diff
    Tester/         # phase pipeline: deterministic checks + synthesized tests → verdict
    Reflector/      # failure-only root-cause analysis, routes the retry
  semantic_search.py  # shared pgvector search tool (all agents)
  tool_output.py      # shared truncation cap on LLM-facing tool output
```

## Planner (`Planning/Planner/`)

Two-phase: **investigate** (ReAct loop with read-only tools — file reads, shell,
semantic search, bounded by `PLANNER_MAX_TOOL_STEPS`) then **synthesize** (structured
output: a `Plan` of `Microtask`s with target files, approach, and structured
`acceptance_criteria` incl. `IOExample`s). Synthesis is guarded by a deterministic
quality gate (`quality.py`) — malformed microtasks raise `BadMicroTaskException` and
the validation error is fed back into a retry prompt (up to
`PLANNER_MAX_SYNTHESIS_ATTEMPTS`). Reads the per-repo lesson store so past failures
shape the next plan (prevention, not detection).

## Plan Reviewer (`Planning/Reviewer/`)

The critic in an **actor–critic** pair. It does its *own* investigation with
read-only tools (it does not trust the planner's summary), then emits a structured
approve/reject. Convergence fix (ADR 0022): the planner only ever sees the *latest*
verdict and structured `blockers` (findings only) — not a stack of prose reviews —
so revision loops converge instead of oscillating.

## Coder (`Implementing/Coder/`)

Implements exactly one microtask per invocation. Hybrid grounding: the runner
pre-reads the target/context files into the prompt, then a single ReAct loop edits
files in the sandbox. Verification is **deterministic, not self-reported**: the
runner takes a `repo.diff` after the loop — an empty diff triggers a re-prompt, and
a compile gate (`py_compile` / `node --check`) on the target files feeds compile
errors back into the *same warm conversation* (up to `CODER_MAX_COMPILE_RETRIES`).
The Coder is gateway-blocked from running tests — separation of duties with the
Tester.

## Tester (`Implementing/Tester/`)

The most engineered agent: a **phase pipeline** (`pipeline.py` running ordered
`TestPhase`s: detect → deterministic checks → synthesis → trivial-guard → execute →
verdict). Two-track grounding (ADR 0016):

- **Track A (deterministic):** run the repo's existing suite plus config-gated
  build/typecheck/lint in the sandbox. Build+typecheck+suite gate; lint is advisory.
- **Track B (synthesized):** LLM writes new tests *from the Planner's acceptance
  criteria* — independent of the Coder's code, so the Coder can't grade its own
  homework. A deterministic AST/regex guard (`grounding.py`) rejects vacuous
  assertions, and synthesis asserts structure-only unless the Planner supplied a
  concrete `IOExample` (an LLM must never invent expected values, ADR 0022).

The Tester **owns the PASS/FAIL verdict** — a deterministic function of check
results, no LLM judgment. Unverifiable targets become a loud UNVERIFIED skip rather
than a false FAIL (ADR 0037).

## Reflector (`Implementing/Reflector/`)

**Failure-only** — never runs on green, so a passing pipeline costs zero extra LLM
calls. Given the failing evidence (honest, head+tail-elided — never misleadingly
truncated), the microtask's acceptance criteria, and the failing test source, it
diagnoses root cause and emits a `Reflection` plus a `fix_target`: `code` (coder
retry with the diagnosis injected) or `test` (the synthesized test itself was wrong
→ tester re-synthesizes, bounded). This is what turns a red test into a *directed*
retry instead of a blind one.

## Shared infrastructure

- `semantic_search.py` — one gateway-routed pgvector search tool used by all five
  agents; returns hits with inline snippets (locate + peek in one call).
- `tool_output.py` — `truncate_for_llm`: every LLM-facing tool result is capped
  (head+tail) because a ReAct loop re-sends history each step; one uncapped read
  once cost ~245k tokens. Deterministic phases read raw output and never truncate.

## Interview sound bites

- "Each agent has one job and a role-scoped tool allowlist — the Coder literally
  cannot run pytest."
- "Verification is grounded: diffs, compiles, and test exits decide, never an LLM
  opinion about its own code."
- "Tests are synthesized from the *plan's* acceptance criteria, not from the code
  under test — that independence is the point."
