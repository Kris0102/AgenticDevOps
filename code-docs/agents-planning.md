# src/agents/Planning/ — Code Explanation (Planner + Reviewer)

The actor–critic pair. Both are two-phase agents (investigate with tools, then
produce structured output), both read the repo's lesson memory, both are wrapped
by their orchestrator Runner (see orchestrator-nodes.md).

---

## Planner/planner.py (413 lines) — `Planner`

### Output models (120–144)
- `PlanDraft` = `Plan` MINUS `review_feedback`. The comment explains why: the LLM
  once echoed prompt labels ("Automated reviewer") into that system-managed
  channel's `source` enum → ValidationError. Solution: the LLM never authors the
  channel — `PlannerOutput` lifts the draft into a full `Plan` with
  `review_feedback=[]`, and prior feedback is re-attached afterwards.
- `PlannerDraft` = plan + microtasks — the structured-output schema.

### `__init__` (147–193)
Config is a **mandatory** argument ("fallback only in the nodes!" — the project's
dependency rule). Builds:
- the chat LLM (`build_chat_llm` with planner knobs),
- the structured LLM: `with_structured_output(PlannerDraft,
  method=structured_output_method(provider), include_raw=True)` — `include_raw`
  matters: the invoke returns `{"raw","parsing_error","parsed"}` and `raw`
  carries `usage_metadata` for token accounting,
- the investigator: `create_agent(llm, tools, INVESTIGATOR_SYSTEM_PROMPT)` — a
  LangChain ReAct agent, only if tools exist.

### The grammar-compile fallback (51–57, 201–221)
Anthropic's `json_schema` mode compiles the big PlannerDraft schema into a
sampling grammar server-side; on a large plan that compile can 400 ("Grammar
compilation timed out"). `_invoke_structured` catches exactly that message
(`_is_grammar_compile_error` — it has no exception type), rebuilds the
structured LLM with `function_calling` (no grammar compile) and retries ONCE;
the swap is permanent per instance (`_grammar_fallback_used` guards a loop).
function_calling is looser (fields can be omitted) — acceptable because the
quality gate + retry already guard that.

### `invoke(state)` — the full flow
1. **Revision detection** (236–244): prior plan with `review_feedback` present →
   revision pass. `_build_revision_context` (60–112) is GH #43 distilled:
   - Only the **LATEST** review verdict is rendered (each review is a complete
     re-assessment; stacking all of them "drowned the planner in duplicate,
     partly-stale '(UNRESOLVED)' notes").
   - The **prior microtasks are rendered in FULL** — without them the planner
     revised blind, regenerated everything, dropped microtasks the reviewer then
     demanded back. A note like "change 'Hamburg' to 'hamburg' in M2 AC1" is
     only actionable if M2 AC1 is visible.
   - Ends with the surgical-edit instruction: change exactly what the notes
     flag, keep ids/count/unflagged criteria UNCHANGED.
2. **Investigation** (251–305): ReAct loop, recursion-limited from
   `planner_max_tool_steps`. ALWAYS re-investigates on a revision ("a plan is
   usually rejected because it MISREAD the repo — it must re-read real files,
   not revise off its own hallucinated prior"). Error triage:
   - `GraphRecursionError` → SOFT: plan from the objective (budget exhausted is
     not an error; downstream grounding catches problems).
   - transient LLM error → `RetriableError` (STALL).
   - genuine error → `RunExecutionError` (FAIL — "synthesizing off a broken
     investigation wastes the planner↔reviewer budget on a doomed plan").
3. **Lessons** (315–320): `find_lessons(scope="plan", exclude_run_id=self.run_id)`
   + `find_operator_rules` → one block, computed once (constant across retries).
4. **Synthesis retry loop** (328–389): up to `max_synthesis_attempts`; each pass
   builds the prompt with `previous_attempt_feedback` (the quality gate's error
   text). On success: `_normalize_output` (the gate) → break; on
   `BadMicroTaskException` → feed the message into the next attempt; last
   attempt → re-raise (run fails). ValidationError does NOT retry — with
   server-side schema constraint it means a schema/model mismatch bug,
   deterministic ("don't retry the same deterministic failure").
5. Output file dumped to `src/output/` per attempt (debug artifact), plan
   artifact appended, returns `{status: NEEDS_REVIEW, plan, microtasks,
   artifacts_index}`.

## Planner/quality.py (188 lines) — the deterministic gate

The most exam-quotable file in Planning. Checks per microtask (`_check_microtask`
→ `_MicrotaskFlags`, shared by the informational `_quality_report` and the
raising `_normalize_output`):

| Check | Threshold | Why |
|---|---|---|
| acceptance_criteria present, non-empty text | — | Tester has nothing to ground otherwise |
| target_files non-empty | ≤3 files | no file contract / too broad → split |
| description length | ≥80 chars | "file path + what goes there" specificity |
| approach length | ≥40 chars | must say HOW, not restate description |
| **not all criteria ungroundable** | — | see below |
| no hollow examples | — | `given={}, expect=None` is "worse than none" |

**The tautology regex** (`_TAUTOLOGICAL_RE`, 19–28): rejects "all tests pass",
"passes all tests", "it works", "works correctly", "no errors", "compiles
successfully", "behaves as expected"… — the content-free criteria class behind
the recurring value-grounding failures (the weather findCity saga): with no
observable behavior, the Tester fell back to existing tests carrying a wrong
self-computed value → unwinnable microtask. A criterion WITH a useful IOExample
is always allowed (`_has_useful_example` requires actual data — non-empty
`given` OR non-null `expect`).

On any error: `BadMicroTaskException` with a bullet list of specific,
actionable failures — that text IS the retry prompt feedback. Also normalizes:
re-indexes microtasks (`idx`) and stamps `status=PENDING`.

## Planner support files
- `planner_tools.py` (84) — `PlannerTools.build_tools()`: gateway-routed
  read-only tools (read file, run command, semantic search), outputs capped via
  `truncate_for_llm`.
- `investigation_support.py` (68) — `_investigation_recursion_limit(steps)`
  (2·steps+padding — a ReAct round is 2 graph steps), `_last_ai_text`,
  `_log_investigation_steps`.
- `plan_output.py` (163) — debug dump of each synthesis attempt to
  `src/output/<ts>_<run>/` (investigation, plan, criteria, token usage, lessons).
- `prompts/investigator.py` + `prompts/synthesis.py` — the two prompts;
  synthesis carries the microtask-quality rules (incl. "reconcile with existing
  tests" — a criterion that changes an asserted value must own the test update
  or become an open_question).

## Reviewer/reviewer.py (301 lines) — `Reviewer`

The critic. Same skeleton as the Planner (two-phase, structured output, lessons)
with these differences:

### `ReviewerOutput` (51–71) — the GH #43 fix in a model
```python
decision: FeedbackDecision   # approve | reject
notes: str                   # one-line verdict summary
blockers: List[str]          # concrete defects, each "what's wrong + how to fix"
```
`to_feedback_notes()` forwards **blockers as a checklist** (falling back to
notes) — because the model leaked investigation reasoning ("let me verify the
codebase…") into a single notes blob the planner couldn't act on. Structured
blockers = actionable feedback = convergence.

### `_build_review_context` (86–97)
Dumps plan+microtasks as JSON for the prompt, with `review_feedback` trimmed to
the latest verdict — the mirror of the planner-side trim ("dumping all of it
made the reviewer fixate on and re-assert the entire backlog with '(UNRESOLVED)'
tags each round").

### `invoke(state)`
1. No plan on state → `{status: NEEDS_PLAN}` (defensive).
2. **Independent investigation**: its own ReAct loop over read-only tools —
   the critic re-derives facts from the repo, it does NOT trust the planner's
   summary. Same three-way error triage as the planner.
3. Lessons block (plan scope) prepended to the review prompt.
4. Structured verdict → `ReviewFeedback(source=REVIEWER,
   notes=to_feedback_notes())` appended to `plan.review_feedback`.
5. `status = NEEDS_CODE` on approve, `NEEDS_PLAN` on reject. Review file dumped
   for debugging; review artifact appended.

Reviewer's tool set (`reviewer_tools.py`, 84 lines) is the strictest: read/exec/
semantic-search only, and its gateway role allows only grep/ls/cat/find.

---

## Relationships

```
planner_node (Runner) ─► Planner.invoke
   Planner ─► investigator ReAct (gateway read-only tools) ─► synthesis (structured)
          ─► quality._normalize_output ⇄ BadMicroTaskException retry loop
          ─► LessonStore.find_lessons/find_operator_rules (scope=plan)
reviewer_node (Runner) ─► Reviewer.invoke  (cap enforced in the node, not here)
   Reviewer ─► OWN investigation ─► structured ReviewerOutput
           ─► plan.review_feedback (same channel HITL plan-reject writes)
approve → NEEDS_CODE → (plan gate) → coder     reject → NEEDS_PLAN → planner revision
```

Exam sentences: "The planning loop converges because three things were fixed
together: the planner sees only the latest verdict, it sees its own prior
microtasks in full, and the reviewer returns structured blockers instead of
prose. Before that trio (GH #43) the loop looped the same rejection verbatim
until the cap."
