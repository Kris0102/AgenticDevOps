# 0008 — Coder: single ReAct loop with pre-fetched grounding and deterministic diff verification

Status: accepted
Date: 2026-06-12

## Context

Wiring the real Coder (`src/agents/Implementing/Coder/`) raised the question: should it
copy the Planner's two-phase shape (investigate-ReAct → synthesize), and how do we stop
an LLM coder from hallucinating file contents and producing gibberish edits? The user's
explicit top priority: the Coder must have **all** context — grounding cannot be left to
the model's discretion. MAS properties in play: grounded verification (no LLM-only
self-report), auditability (every environment action through the ToolGateway), safety
(stop conditions, forbidden paths).

## Decision

1. **No separate investigate/ReAct phase.** The Planner needs the split because its final
   artifact is structured JSON (`with_structured_output(json_mode)` can't interleave tool
   calls — see ADR 0007). The Coder's final artifact IS the file edits made via
   `fs.write_file`; the tool calls are the work, so a single ReAct loop suffices.
2. **Hybrid grounding.** `CoderRunner._gather_file_context` pre-reads every
   `target_files` + `context_files` entry through the gateway (each read audit-logged)
   and injects real contents into the prompt, alongside objective, plan summary /
   architecture notes / `data_contracts`, the full microtask, reflections for this
   microtask, and `forbidden_paths`. The ReAct loop is for *extra* reads and the writes.
3. **Deterministic post-verification.** After the loop, `Coder._code_until_diff` calls
   `repo.diff` through the gateway (with `git add -N .` first so created files show up).
   Empty diff → re-prompt with feedback (`CODER_MAX_EMPTY_DIFF_RETRIES`); still empty →
   `RunExecutionError` → FAILED. `files_touched` is parsed from the diff text, never
   taken from LLM self-report (derive-from-state). Forbidden-path hits in the diff fail
   the run.
4. **Minimal stop-condition enforcement (audit #1, coder half).**
   `CoderRunner._check_stop_limit` refuses a microtask whose
   `attempts >= (Microtask.max_attempts or Constraints.max_microtask_attempts)` —
   kills the runaway coder↔reflector loop. Router-level `max_total_iterations` remains open.
5. **CoderRunner selects the current microtask** (reuse in-flight `current_microtask_id`,
   else first PENDING by `idx` with finished dependencies; none left → provisional DONE).
   No new state field; selection derived from existing microtask statuses.

## Alternatives considered

- **Two-phase planner-style coder (investigate → edit):** rejected — extra latency, loses
  the tight read-modify-write loop, and there is no structured output forcing the split.
- **Pure ReAct (model must read files itself):** rejected — grounding becomes a prompt
  suggestion the model can skip; exactly the hallucination path the user wants closed.
- **LLM-reported `files_touched` / success:** rejected — violates grounded-verification;
  the diff is the only trusted record of what changed.
- **Separate microtask-selector graph node:** rejected for now — topology churn with no
  benefit while the reviewer/tester are stubs; revisit when the Tester drives the loop.

## Consequences

- Coder prompt is large (full file contents inline) — costs tokens, buys grounding.
  Acceptable for microtask-scale edits; revisit if target files grow huge (chunking /
  Context Engine).
- Until the real Tester marks microtasks COMPLETED, the run codes one microtask and ends
  (tester stub). The microtask loop activates when the Tester lands.
- Gateway command allowlist is global, so the Coder *can* technically run `pytest`
  (prompt-forbidden only). Per-role sandbox command allowlists are a known TODO.
- New env knobs: `CODER_MAX_TOOL_STEPS` (16), `CODER_MAX_EMPTY_DIFF_RETRIES` (2),
  `CODER_TIMEOUT_SECONDS` (120).
