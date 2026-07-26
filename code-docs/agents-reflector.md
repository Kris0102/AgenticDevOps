# src/agents/Implementing/Reflector/ — Code Explanation

~800 lines (reflector.py 432, prompt 250+, tools 110, output 193). Failure-only
root-cause diagnosis; the node-side BLOCKED machinery lives in
`orchestrator-nodes.md` — this is the agent.

---

## reflector.py — `Reflector`

### `ReflectionOutput` (30–82) — the schema IS the policy
The pydantic Field descriptions are instructions to the LLM (they land in the
structured-output schema), and they encode the hardest-won rules:

- `decision: "approve" | "improve" | "blocked"` — `blocked` is defined
  precisely: "NO code change can succeed because two requirements/checks
  contradict each other … or your own prior diagnoses oscillate (a fix that
  undoes the previous fix). Stops the retry loop and surfaces the conflict to
  the human instead of burning the remaining attempts (GH #61)."
- `fix_target: "code" | "test"` — the routing decision, judged **against the
  ACCEPTANCE CRITERIA, not against what the test asserts**. The description
  names the classic wrong-test signal: "a self-computed expected value the
  Planner never supplied" (a hand-computed distance / rounded number / exact
  error string). "Prefer 'code' when genuinely unsure."
- Plus `failure_type` (category), `root_cause`, `fix_instructions`
  (step-by-step, file+function specific — this text becomes the Coder's
  retry context), `should_store_in_semantic_memory` (the lesson-worthiness
  flag the tester-PASS later confirms).

### `_render_prior_diagnoses` (104–129) — GH #61 layer 1
Renders the reflector's OWN earlier reflections for this microtask into the
assessment prompt, with an explicit self-check: "If your new fix would UNDO a
fix you prescribed earlier, you are OSCILLATING — the checks themselves likely
conflict. Do NOT issue another opposite code fix; set decision='blocked'."
Comment records the mechanical cause it fixed: "attempt 2 re-inserted exactly
what attempt 1 removed" — the reflector was reasoning blind to its own past.

### `invoke(state, microtask, test_passed, test_output)` (183–300)
Boundary note in the docstring: `test_output` is fetched by the RUNNER from the
durable artifacts ("the node owns DB access, the agent just reasons over the
text"). Flow:
1. `get_repo_diff()` — the current diff, via gateway.
2. **Phase 1 — investigation**: ReAct loop (read files, diffs, semantic search;
   read-only role) → summary.
3. Lessons block (code scope) + operator rules.
4. `_read_synth_test` — the synthesized test SOURCE, so the code-vs-test call
   judges the actual assertion, not just the failure output (empty on a
   Track-A-only failure — the repo's own suite is never "regenerated").
5. Prior-diagnoses block (above).
6. **Phase 2 — assessment**: structured `ReflectionOutput` via
   `build_diagnosis_prompt` (which injects microtask criteria + synth test +
   prior diagnoses + lessons).
7. Builds the `Reflection` state model (fix_target="escalate" when blocked) and
   returns: `reflections` appended, `_reflector_approved` / `_fix_target` /
   `_reflector_blocked` as **internal keys the Runner strips** before LangGraph
   sees the dict, debug output path, and `status=NEEDS_CODE` on a plain
   improve (the Runner overrides for test-resynth / blocked routes).

### prompt/reflector_prompt.py — honest evidence (ADR 0037)
`_elide_middle(text, limit)` — head+tail elision with a marker that **names
itself a display cut**. The history: the old `diff[:2000] + " [truncated]"` made
the reflector diagnose "truncated implementation" on COMPLETE code (run
27198e26) — the harness was lying to the diagnostician. Budgets: diff 4k in
investigation / 12k in diagnosis, test output 6k, synth test 4k. The diagnosis
prompt also carries a SCOPE paragraph stopping the reflector from demanding
integration work that belongs to later microtasks.

### reflector_tools.py / reflector_output.py
- Tools: read-only (read file, diff, run read-only commands, semantic search)
  under `Roles.REFLECTOR` — a diagnostician that cannot modify anything.
- Output: the per-microtask debug dump (investigation, reflection, verdict,
  prior reflections, lessons, tokens) — `last_reflection_output_path` is also
  surfaced in the code-gate HITL prompt.

---

## Relationships

```
tester FAIL (REFLECT) ─► reflector_node ─► Reflector.invoke
inputs: durable check artifacts (via runner) + live diff + synth test source
        + prior own diagnoses + repo lessons
output: Reflection (root_cause + fix_instructions) + fix_target
  fix_target=code ─► coder retry (reflection injected into coder prompt)
  fix_target=test ─► tester re-synthesis (test_rejection_note, bounded)
  blocked / diff-forensics backstop (node) ─► run BLOCKED, conflict named
tester PASS later ─► record_code_lessons confirms the diagnosis as a lesson
```

Exam sentence: "The Reflector's job is attribution: code wrong, test wrong, or
requirements contradictory. Each verdict has a different consequence — retry,
regenerate, or stop — and two of its inputs exist purely to keep it honest: its
own prior diagnoses (so it notices oscillation) and un-lied-to evidence (elision
markers that admit they're display cuts)."
