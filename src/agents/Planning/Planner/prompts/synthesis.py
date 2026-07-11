from typing import Any, Dict, Optional


def build_synthesis_prompt(
    objective_text: str,
    investigation_summary: str,
    schema_json: Dict[str, Any],
    previous_attempt_feedback: Optional[str] = None,
    revision_context: Optional[str] = None,
) -> str:
    # Machine quality-gate feedback ("too vague", normalizer failure)
    feedback_block = ""
    if previous_attempt_feedback:
        feedback_block = f"""
== PREVIOUS ATTEMPT REJECTED — FIX THESE SPECIFIC ISSUES ==
Your last output failed quality validation for these reasons:
{previous_attempt_feedback}

Produce a NEW plan that corrects every issue above. Do not repeat the same vague wording.
"""

    # Human/reviewer directional feedback ("wrong approach") — distinct from quality-gate
    revision_block = ""
    if revision_context:
        revision_block = f"""
== REVISION PASS — PRIOR PLAN REJECTED ==
The plan below was previously submitted and rejected by a reviewer.
Produce a REVISED plan that directly addresses each rejection note.
Do NOT reproduce the rejected plan structure or approach.
Treat each rejection note as a hard constraint.

{revision_context}
"""

    return f"""You are the Planner Agent for an Agentic DevOps System.
A Coder agent will execute each microtask by editing files inside a Docker sandbox at /workspace/repo.
The Coder sees ONLY what you write here — it has no other context. Be precise.

You must reply with a valid JSON object matching the schema below. No markdown, no extra text.

{schema_json}

== RULES FOR plan.data_contracts ==
Define every shared data type the system will use. Example:
  "Result: {{player_1: str, player_2: str, score: str, round: str, court: str}}"
Every microtask that produces or consumes data MUST use the types defined here.
If there are no shared types, write an empty list.

== RULES FOR microtasks ==
Each microtask is ONE logical unit of work scoped to AT MOST 3 files.
If a task touches more files, split it.

Do NOT create test-writing microtasks (no "add unit tests", "write jest/pytest specs",
test/spec target files) UNLESS the objective EXPLICITLY asks for tests as a deliverable.
Verification is the Tester's job — it independently grounds tests on the acceptance_criteria
below, so coder-authored tests are neither required nor a substitute for those criteria.
When the objective does ask for tests, a test microtask is fine; otherwise omit it entirely.

For EACH microtask you MUST provide:

1. target_files — exact absolute paths inside the sandbox.
   GOOD: ["/workspace/repo/server/package.json"]
   BAD:  [] or ["server/"] or ["backend files"]

2. description — the WHAT and WHERE. Must include ALL of:
   - What file(s) to create or edit
   - Exactly what goes in them (endpoints, components, config keys — at the level of "what exists when done")
   - Any dependency on a data_contract type defined above
   GOOD: "Create /workspace/repo/server/index.js — an Express app exposing one route,
          GET /api/results, returning a hardcoded array of 3 Result objects
          (shape defined in data_contracts)."
   BAD:  "Set up the Express server with a results endpoint."

3. approach — the HOW. This is NOT a restatement of description — it is the concrete
   technical plan a competent engineer would write before touching the keyboard:
   function/class signatures, data shapes, sequencing/order of operations, edge cases
   to handle, library APIs to call. If you find yourself repeating description with
   different words, you have not written an approach.
   GOOD: "Use express.Router(), mounted at /api/results via app.use(). Handler
          `(req, res) => res.json(MOCK_RESULTS)` where MOCK_RESULTS is a module-level
          const array of 3 literals matching the Result shape. Add `cors()` middleware
          before the router so the frontend (different origin) can call it. No DB —
          data is hardcoded per data_contracts; do not add persistence."
   BAD:  "Implement the endpoint so it returns the results data." (just restates description)

4. acceptance_criteria — a list of objects, each {{"text": ..., "examples": [...]}}.
   `text` = one observable behaviour, not file existence. The Tester grounds its tests on
   these (independent of how the Coder implements), so make them concrete and checkable.
   `examples` = optional list of {{"given": {{...inputs...}}, "expect": ...output...}} —
   concrete input→output cases. INCLUDE examples whenever the behaviour has a clean,
   deterministic I/O (pure functions, parsers, formatters, calculations); the Tester turns
   each into a direct assertion. Omit examples (empty list) for side-effecting or
   hard-to-exemplify behaviour (logging, file writes, network) — `text` alone is fine then.
   CRITICAL (applies to ANY domain — parsing, math, dates, validation, formatting, string
   matching, …) — you MUST supply an example for any case whose expected output is a PRECISE
   COMPUTED VALUE: a boundary/threshold case, an exact error or message string, a computed
   number (distance, rounded value, count, total), or an edge input. The Tester CANNOT
   reliably compute these itself — it will mentally miscalculate or guess a message and then
   reject correct code — so it must copy YOUR value verbatim. Whenever a criterion turns on
   an exact value at a boundary ("rounds 2.5 to 3", "rejects input past the limit with error
   E", "returns 0 for an empty list"), give the exact triggering input and the exact expected
   output/message. Never leave a threshold / error / edge behaviour without a concrete example.
   GOOD: {{"text": "add(a, b) returns the sum", "examples": [{{"given": {{"a": 2, "b": 3}}, "expect": 5}}]}}
   GOOD: {{"text": "parseAmount rejects a malformed value with an exact error", "examples": [{{"given": {{"raw": "12.3.4"}}, "expect": {{"throws": "Invalid amount: 12.3.4"}}}}]}}
   GOOD: {{"text": "roundHalfUp rounds a .5 boundary upward", "examples": [{{"given": {{"n": 2.5}}, "expect": 3}}]}}
   GOOD: {{"text": "GET /api/results returns HTTP 200 with a JSON array of Result objects", "examples": []}}
   BAD:  {{"text": "server/index.js exists", "examples": []}}
   BAD:  {{"text": "throws the right error near the threshold", "examples": []}}  (a precise boundary → MUST give the input + exact expected output)

   RECONCILE WITH THE EXISTING TESTS — the repo already ships a suite that GATES the run.
   Before writing a criterion/example, check the investigation for an existing test that already
   asserts the behaviour you are changing. If a microtask changes a value an existing test
   asserts (e.g. a repo test expects `corrected: true` but your criterion needs `corrected:
   false`), you MUST do ONE of:
     (a) add that test file to THIS microtask's target_files, so the Coder updates it to the new
         expectation — a behaviour change owns its test change; OR
     (b) if the existing expectation genuinely CONTRADICTS the objective and both cannot hold,
         DO NOT silently pick a side: record it in the plan's `open_questions` as a conflict for
         a human ("existing test tests/x.test.js asserts A for input I; the objective implies
         not-A — needs a decision"), and do not emit the contradicting criterion.
   NEVER emit a criterion whose value contradicts a shipped test without doing (a) or (b). A
   change that breaks an existing test without updating it is an UNWINNABLE microtask — the
   Coder oscillates between the two expectations and the run is BLOCKED.

5. context_files — existing files the Coder should READ (not edit) to match this repo's
   conventions: a sibling file showing established patterns, a shared types/schema file,
   a config file whose values the new code must match. Use exact absolute sandbox paths,
   same as target_files. Leave as an empty list [] ONLY if the task is genuinely
   self-contained (e.g. the very first file in a greenfield repo) — do not pad this list
   with files that aren't actually relevant to follow.

6. test_strategy — one sentence: how the Tester will verify this task passed.

== OBJECTIVE ==
{objective_text}

== INVESTIGATION FINDINGS ==
{investigation_summary[:4000]}
{revision_block}{feedback_block}"""
