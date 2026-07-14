# 0034 — Lesson store: human curation, operator rules, and closing the learning loop

Status: accepted
Date: 2026-07-14

## Context

ADR 0018 gave the system per-repo cross-run memory: when a failure diagnosis is confirmed
by an outcome (reviewer approves the revised plan / tester passes the retried microtask),
the lesson is embedded into `lessons` and retrieved semantically (top-k) into future runs'
prompts as advisory context.

That memory was **write-only from the human's point of view**. It had no UI beyond a
read-only per-run panel, no way to correct a wrong lesson, and no way for the operator to
contribute knowledge they already had. Three concrete gaps:

- **A wrong lesson is unfixable.** ADR 0018 documents its own known limitation — a green
  retry proves the failure went away, not that the diagnosis was causal, and run
  `ba3a937c` stored a confidently-wrong lesson whose guidance would actively worsen the
  bug. Nothing could remove it.
- **The operator cannot teach the system anything.** Facts a human knows on day one
  ("this repo uses `fetch`, never add `axios`") could only enter the memory by the agents
  failing their way to them.
- **Provenance was invisible.** The panel showed lesson text with no indication of which
  run produced it, what failure triggered it, or why it was trusted — so a human could
  not judge a lesson even if they could edit it.

Separately, an audit while building this surfaced a **fourth gap that predates it**: ADR
0018's read path lists only "Reviewer, Coder/Reflector". The **planner** — the agent that
writes the plans these lessons are about — was never a consumer, with no rationale
recorded anywhere. The system remembered its planning mistakes only at the point of
*catching* them, never at the point of *avoiding* them.

## Decision

### 1. Provenance is derived, never stored twice

The "why was this lesson added" narrative is computed from the columns that already exist
(`dashboard/src/lessonInfo.ts`), not persisted alongside them. This is possible only
because of ADR 0018's write-on-confirmation rule: `agent` + `scope` uniquely identify
which confirming step wrote the row, so the story is a pure function of the data.

Two nullable columns were added — but only for facts that are **not** derivable, and both
are load-bearing rather than descriptive (migration `d7e8f9a0b1c2`):

| column | why it cannot be derived |
|---|---|
| `microtask_id` | existed only inside the `lesson_id` string (`lesson_<run>_code_<mt>`); parsing that in the UI is fragile. Backfilled from the id. |
| `updated_at` | records a human edit — a new fact, and the key to retraction immunity (§3) |

`run_id` became nullable (a human rule has no originating run). **Authorship reuses the
existing `agent` column** (`"human"`) rather than adding a `source` flag — no new
parallel field.

**The one place `agent` is NOT rendered directly is the card's author badge.** `agent`
records the node that *confirmed and wrote* the row, which for a code lesson is the
**tester**, not the reflector whose diagnosis the row actually contains. Showing it raw
credits the tester for the reflector's work, so `originLabel` derives the author from
`scope` instead (plan → reviewer, code → reflector — each scope has exactly one
diagnosing agent). Caught by reading live data, not by reading the schema.

### 2. Operator rules are pinned, not ranked

A human-authored lesson (`agent="human"`) is **always injected**, capped per scope at
`LessonStore.OPERATOR_RULE_LIMIT` (15), in its own prompt block framed as instructions —
while agent-learned lessons keep ADR 0018's advisory framing ("may be stale, not rules"):

```
## Operator rules for this repo (written by a human — follow these)
## Lessons from past runs on this repo (advisory — may be stale, not rules)
```

`find_operator_rules` is deliberately **not** a vector search. Making a typed rule compete
for a top-k slot against learned lessons would let a freshly-typed rule silently lose the
cosine race and appear to do nothing — the failure mode is invisible and indistinguishable
from a bug. A rule the operator typed is an instruction; ranking it is a category error.
Rules render first so they survive any downstream truncation.

### 3. A human edit makes a lesson immune to retraction

`retract_run` (ADR 0018) deletes every lesson a run wrote when that run FAILS. It now
skips lessons with `updated_at` set, or `agent="human"`.

The rationale is precisely ADR 0018's own: retraction exists because the **agent's**
phase-boundary self-confirmation may be confounded. A human who read and edited the lesson
is a *strictly stronger* endorsement than the heuristic being distrusted — so the guard
should not fire against them. Immunity is derived from `updated_at`, not a separate flag.

### 4. The planner joins the read path

The planner now retrieves plan-scope lessons + operator rules (`obj_text` as the query —
the same query the reviewer uses) and injects them into its synthesis prompt, next to the
investigation findings.

This closes the loop at **prevention** instead of detection. Previously a repo's known
planning mistake was re-made every run, rejected by the reviewer, then revised — burning a
full planner↔reviewer cycle per known mistake, per run, forever. It also fixes an
absurdity this ADR's own feature introduced: a `plan`-scope operator rule was visible to
the reviewer but not to the planner, so an explicit human instruction could only be
enforced by rejecting the plan that violated it.

Per ADR 0018, the injected block is written to the planner's debug output file
(`planner_iteration_N.txt`) like every other consumer's.

**Accepted risk — actor-critic coupling.** Planner and reviewer are an actor-critic pair;
sharing a prior makes the critic less independent, and both could converge on "what was
approved before" rather than "what is right now" (premature convergence). Accepted
because: lessons stay advisory rather than gates, the reviewer still investigates the repo
independently, retrieval is bounded (top-3, semantically filtered by objective), and ADR
0018 already accepted this exact coupling for the coder↔reflector pair. Revisit if plan
quality flattens across runs on a repo. Distinct from ADR 0022's convergence fix, which
concerned *within-run stacked rejections*; this is bounded cross-run retrieval, and the
current run is excluded (`exclude_run_id`) so a revision's live verdict is not doubled.

### 5. Surface

A full-page route (`view === 'lessons'`, top bar → Lessons) per the ReposPage pattern:
repo picker, operator rules and learned lessons in separate sections, each card carrying
its derived "why", a link to the originating run, edit/delete. The run-page panel imports
the same derivation module, so the two views cannot drift.

```
GET    /repos/{repo_id}/lessons    POST /repos/{repo_id}/lessons
PUT    /lessons/{lesson_id}        DELETE /lessons/{lesson_id}
```

The human CRUD path **raises** where the agent path swallows (ADR 0018's "never break the
run"): a human typing a rule must be told when it did not stick. It opens its own
`session_scope()` (no ambient session at the API boundary), unlike the `record_*` path.
Editing re-embeds when the summary changed — a stale vector would make a lesson unfindable
by its own text.

## Alternatives considered

- **`origin` JSON column snapshotting the confirming moment** (failing test output, fixing
  diff, event id). Richest "why", rejected: it denormalizes the `events`/`artifacts`
  tables into a parallel field that drifts, to render a narrative that is derivable.
- **Zero schema change** — derive everything, no new columns. Rejected: human rules need
  `run_id` nullable regardless, and the microtask link would have to be parsed out of the
  `lesson_id` string.
- **A `pinned` boolean on every lesson** (pin agent lessons too). More flexible, rejected
  as YAGNI: a stored flag where authorship already implies the answer.
- **Retraction always wins over human edits.** Keeps ADR 0018's guarantee absolute, but
  silently deletes human work — the worst property a demo can have.
- **Soft-delete tombstones for retracted lessons.** Extra state to carry for a case §3 now
  prevents.

## Consequences

- The known limitation ADR 0018 accepted (confidently-wrong lessons are permanent) now has
  a remedy: a human can fix or delete one.
- Expect **First-Pass Yield** (Grafana) to move — §4 converts known mistakes from
  detected-and-revised into avoided. That is the measurable claim of this ADR.
- `lessons` has no prune/cap hygiene yet (ADR 0018 open item). Learned lessons accumulate
  unbounded; retrieval stays O(top_k) so this is a storage concern, not a prompt-size one.

### Amendment 2026-07-14 — the rule cap must not drop rules silently

`OPERATOR_RULE_LIMIT` (15/scope) exists because pinned rules are unranked: without a bound a
growing list would eventually crowd out the task itself. But the cap keeps the NEWEST rules,
so adding rule 16 silently evicts rule 1 — and the store listed all 16 as though they
applied. That is precisely the failure §2 claims pinning prevents ("a rule the human typed
must never silently do nothing"), reintroduced one layer up.

`LessonView.injected` now carries the answer: `Service._injected_rule_ids` re-derives, from
the same constant and the same newest-first ordering as `find_operator_rules`, which rules
actually reach a prompt. Rules past the cut-off are badged "not sent to the agents" in red
on both the store page and the run panel, plus a section warning. The cap number is NOT
duplicated in the frontend — the UI counts the rules the server marked as injected, which
*is* the cap whenever anything was dropped.

Rejected: raising the cap alone (moves the cliff, doesn't remove it); a token budget instead
of a count (better-modelled, still silent); refusing to store rule 16 (loudest, but blocks
the user mid-flow to enforce a limit only the prompt cares about).
- Rules are repo-scoped and scope-scoped. There is no global rule ("for all repos") — add
  one per repo. Deliberate: `repo_url` is the memory key throughout (ADR 0018/0024).
