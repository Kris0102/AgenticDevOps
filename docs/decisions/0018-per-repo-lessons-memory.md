# 0018 — Per-repo lessons memory (cross-run failure learning)

Date: 2026-06-27
Status: Implemented (write + read paths). `lessons` table (migration `b2c3d4e5f6a7`) +
`db/lessons.py` `LessonStore`. Write: `record_plan_lessons` (reviewer APPROVE),
`record_code_lessons` (tester PASS); `record_test_lessons` is a deliberate no-op.
Read: `find_lessons` (pgvector top-k, repo+scope filtered) + `render_block`, injected
advisory into the reviewer / coder / reflector prompts. Open: per-repo prune/cap hygiene;
v2 retrieval-usefulness reinforcement.

## Context
Rejections and failures are currently per-run and ephemeral. The Plan Reviewer rejects a
plan for a reason; the Reflector diagnoses a failed microtask — but that knowledge dies
with the run. The next run on the same repo re-learns the same lessons from scratch.

The codebase is already primed to fix this:
- `ReflectionOutput.should_store_in_semantic_memory` already flags reusable anti-patterns
  (the reflector decides whether a lesson is worth keeping) — just not wired to storage.
- pgvector + `Embedder` (MiniLM, 384-dim) + `SemanticStore` + `ContextEngine` already exist
  for code chunks (ADR 0010) — the same infra can store/retrieve lesson embeddings.
- The `events` table already records `plan.reviewed` rejections and `reflection.created`,
  so failures are durably logged — just not curated, embedded, or repo-scoped for reuse.
- CLAUDE.md known-bug #7 ("`reflections` table missing — no queryable cross-run memory")
  is a subset of this.

## Decision
Add a durable, **per-repo, semantically-retrievable lessons memory** that the agents read
before acting and write to after a (quality-gated) failure.

### Schema — a typed `lessons` table
`{ lesson_id, repo_url, agent (reviewer|reflector), scope (plan|microtask),
   failure_type, summary, guidance, source_run_id, created_at, embedding Vector(384) }`
Per-repo isolation is by `repo_url` (a column filter, and/or a `lessons:<repo>` namespace in
`SemanticStore`). Reuse `Embedder` + `SemanticStore` — no new vector infra.

### Write path — quality-gated (the make-or-break)
Store a lesson ONLY when it is a real, reusable signal:
- Reflector: `fix_target == "code"` AND `should_store_in_semantic_memory == True` (a genuine
  code bug the model is confident about). **Never** store `test`-target diagnoses, ERRORED
  (harness) failures, "unverified" verdicts, or low-confidence guesses.
- Reviewer: substantive plan rejections (not trivial nits).
Store the **distilled** lesson (`root_cause` + `guidance`), not raw test output.

Rationale: the Reflector can misdiagnose (observed: a run blamed correct code for a wrong
synthesized test). Storing misdiagnoses poisons future runs — so writes are gated hard.

### Read path — advisory, top-k, repo-scoped
Before acting, retrieve the top-k (≈3) most semantically-similar lessons **for this repo**:
- Reviewer, before reviewing a plan → "plans here were previously rejected for X."
- Coder/Reflector, before a microtask → match on description/target files → "prior pitfalls."
Inject as **advisory** prompt context ("past notes, may be stale") — **never a hard gate**.
A lesson from an old code version can be wrong after a refactor.

**Exclude the current run** (`find_lessons(exclude_run_id=...)`): lessons are the CROSS-run
layer; the within-run layer is `state.reflections` (already microtask-scoped — both
`coder_prompt` and `reflector_output` filter to the current microtask, so reflections never
bloat a prompt). A same-run lesson points at code still changing in THIS run (higher
staleness risk) and re-injecting it creates a within-run self-reinforcement loop, so the two
layers are kept separate. NB: this is a deliberate trade-off — it disables within-run
cross-microtask learning (mt-2 reusing mt-1's just-confirmed lesson). Accepted because
microtasks are distinct units, the lesson is advisory only, and the planner already
decomposed the work. Revisit if cross-microtask transfer proves valuable.

The retrieved block is also written into each agent's debug output file (reviewer/coder/
reflector) so a run is auditable: you can see exactly which past lessons were injected.

### Hygiene
- Timestamp every lesson; cap per repo (keep last N / most-retrieved); prune.
- (v2) Track whether a retrieved lesson actually helped → reinforce useful ones, drop dead
  weight.

## Consequences
- Cross-run learning: repeated mistakes on a repo are surfaced to the agents instead of
  re-discovered. Subsumes TODO #7 (queryable reflections) and extends ADR 0010.
- New `lessons` table + Alembic migration; write hooks in `reviewer_node` / `reflector_node`
  (gated); read hooks in the reviewer and coder/reflector prompt builders.
- Lean prompts preserved (top-k only); blackboard unaffected (this is durable DB memory, not
  run state).

## Rejected / deferred alternatives
- **Store every rejection verbatim** — rejected: noise + poisoning from misdiagnoses; gate
  on quality + distill instead.
- **Make a retrieved lesson block an action (hard gate)** — rejected: staleness makes
  lessons sometimes wrong; advisory only.
- **Query the `events` table directly at prompt time** — insufficient: not embedded (no
  semantic relevance) and not curated; the `lessons` table is the curated layer on top.
- **Global (cross-repo) lessons** — deferred: start per-repo (isolation, relevance). A
  global tier could come later behind stronger gating.
