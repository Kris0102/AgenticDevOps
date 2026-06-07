from typing import Any, Dict, Optional


def build_synthesis_prompt(
    objective_text: str,
    investigation_summary: str,
    schema_json: Dict[str, Any],
    previous_attempt_feedback: Optional[str] = None,
) -> str:
    feedback_block = ""
    if previous_attempt_feedback:
        feedback_block = f"""
== PREVIOUS ATTEMPT REJECTED — FIX THESE SPECIFIC ISSUES ==
Your last output failed quality validation for these reasons:
{previous_attempt_feedback}

Produce a NEW plan that corrects every issue above. Do not repeat the same vague wording.
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

4. acceptance_criteria — observable behaviour, not file existence.
   GOOD: "GET /api/results returns HTTP 200 with JSON array where each item has player_1, player_2, score, round, court"
   BAD:  "server/index.js exists"

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
{feedback_block}"""
