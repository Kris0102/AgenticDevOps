# MAS Patterns & Design Choices — Why This System Is Built the Way It Is

> **What this document is.** A catalogue of every multi-agent-system pattern and major
> design choice used in AgenticDevOps — what each one is, where it lives in the code,
> why it was chosen, and what it costs. Written to still make sense years from now.
> Companion docs: `ARCHITECTURE.md` (the as-built system), `docs/decisions/` (dated
> ADRs with the full reasoning per decision), `CLAUDE.md` (current status).
>
> The yardstick behind every choice here is the same four properties:
> **auditability, safety, reproducibility, human-in-the-loop.** A pattern that is
> elegant but weakens one of these was rejected; several patterns below exist *only*
> because of one of these properties.

---

## Quick index

| # | Pattern | One-liner |
|---|---------|-----------|
| 1 | Distinct specialized roles | Five narrow agents instead of one generalist |
| 2 | Actor–Critic (generator/critic split) | The reviewer critiques plans it didn't write |
| 3 | Blackboard architecture | One shared state, one owner, no side channels |
| 4 | Star topology | All communication mediated by the orchestrator |
| 5 | Status-driven state machine | The `status` enum IS the inter-agent protocol |
| 6 | Plan-and-execute | Plan frozen before coding; no re-planning mid-flight |
| 7 | Structured task decomposition | Microtasks as typed contracts, not prose |
| 8 | Two-phase agents (investigate → synthesize) | Ground first, generate second |
| 9 | ReAct tool loops | Agents read the real repo, never trust recall |
| 10 | Grounded verification | Deterministic checks decide; the LLM never self-certifies |
| 11 | Independent test generation | Tester writes tests from the spec, not from the code |
| 12 | Baseline-delta testing | Pre-existing failures don't block; new ones do |
| 13 | Evidence-gated progress | Empty diff = no work happened, regardless of claims |
| 14 | Failure-only reflection | Diagnosis runs on red only; green costs zero LLM calls |
| 15 | Self-diagnosis routing (`fix_target`) | The system can conclude its own test was wrong |
| 16 | Deny-by-default tool gateway | Capability control per role, every call audited |
| 17 | Sandbox-per-run isolation | Blast radius = one container |
| 18 | Sandbox as source of truth | Memory is for retrieval, never reconstruction |
| 19 | Bounded autonomy (caps & budgets) | Every loop has a ceiling; runaway is impossible |
| 20 | Human-in-the-loop gates | Suspend-and-resume approval, reject requires a note |
| 21 | Layered memory | Working / episodic / semantic / lessons — four stores, four jobs |
| 22 | Confirmation-gated learning | Lessons written only on validated outcomes |
| 23 | Human-curated memory | Operator rules: pinned, never lose a relevance race |
| 24 | Checkpointed execution | Crash-anywhere, resume-anywhere |
| 25 | Stall, don't fail | Recoverable errors never throw away paid-for progress |
| 26 | Idempotent bootstrap outside the graph | Environment readiness re-verified on every entry |
| 27 | Resolve-once configuration | A run's config is immutable after creation |
| 28 | Pure routers, effectful nodes | Side effects live in exactly one place |
| 29 | Calibrated failure handling | Hard-block secrets, degrade PR failures — per-risk, not uniform |

---

## A. Coordination patterns — how agents talk

### 1. Distinct specialized roles
**What:** Five agents with narrow jobs: Planner (decompose), Reviewer (critique plans),
Coder (implement one microtask), Tester (verdict), Reflector (diagnose failures). Plus
two non-agent actors: deploy (GitOps) and bootstrap (environment).
**Where:** `src/agents/**`, roles enumerated in `tool_gateway/Roles.py`.
**Why it's good:** Three independent payoffs. (a) *Prompt focus* — each agent's prompt
describes one job, so a small model can do it well; per-agent model selection
(GH #71) exploits this: cheap models investigate, the strong model codes. (b)
*Permission granularity* — a role is a security boundary: the Coder is
gateway-blocked from running tests, the Reviewer is read-only. One generalist agent
would need the union of all permissions. (c) *Auditability* — every event carries an
actor; "who did what" is a query, not forensics.
**Cost:** More hand-offs, more state plumbing, more LLM calls than one agent with one
long conversation.

### 2. Actor–Critic (generator/critic separation)
**What:** Work is never accepted by the mind that produced it. Planner ↔ Reviewer for
plans; Coder ↔ Tester for code; Tester ↔ Reflector for failure diagnosis.
**Where:** planning loop in `orchestrator.py` (`planner → plan_reviewer`), ADR 0011.
**Why it's good:** An LLM asked "is your own output good?" says yes. An independent
critic with its own investigation tools and its own context genuinely disagrees — the
reviewer rejects real plans, which is the entire point. Convergence needed tuning
(ADR 0022): the planner sees only the *latest* verdict and structured `blockers`, not
a stack of prose reject history, or the loop diverges into arguing.
**Cost:** Doubled LLM spend on planning; a loop that must be capped (pattern 19).

### 3. Blackboard architecture
**What:** One shared state object (`BlackboardState`, `orchestrator/state.py`) owned
exclusively by the Orchestrator. Agents read it and return *changed keys only*; nobody
mutates in place; there is no other channel.
**Where:** `orchestrator/state.py`; convention enforced across all `*_node.py`.
**Why it's good:** Every inter-agent hand-off is a visible, diffable, checkpointable
state transition. Reproducibility falls out for free: snapshot the blackboard and you
have the entire run. Debugging a MAS where agents whisper to each other directly is
archaeology; debugging this one is reading a sequence of state diffs.
**Cost:** The state model grows (it carries every agent's channels: feedback notes,
resynth counters, evidence). Discipline required — one in-place mutation silently
breaks checkpointing.

### 4. Star topology (orchestrator-mediated communication)
**What:** No agent ever calls another agent. All routing decisions are made by the
orchestrator graph based on blackboard state.
**Where:** `orchestrator/orchestrator.py` — the only file with edges.
**Why it's good:** The whole state machine reads top-to-bottom in one file. There is
exactly one place to look to answer "what happens after X". Direct agent-to-agent
calls would create hidden control flow that no audit trail captures.
**Cost:** Everything routes through status transitions, so adding a new interaction
means touching the shared state model — deliberate friction.

### 5. Status-driven state machine routing
**What:** One enum field (`RunStatus`) expresses every agent's verdict:
`NEEDS_PLAN`, `NEEDS_REVIEW`, `NEEDS_CODE`, `NEEDS_TEST`, `REFLECT`, `DONE`… Routers
are pure functions over `status`. Each edge has an *allowlist* of expected statuses
(`_status_gate`); an unexpected status halts at END instead of advancing.
**Where:** `orchestrator/orchestrator.py` routers; `RunStatus` in `state.py`.
**Why it's good:** The protocol between agents is a typed enum, not implicit
convention. The allowlist detail matters: it derives *the legal next phase from the
status* rather than merely ruling out one bad value — an agent that leaves a stale or
inconsistent status stops the run instead of silently entering the wrong phase.
**Cost:** Status semantics must stay disciplined; two phases reusing one status value
need care (see the DONE disambiguation in `_hitl_router`).

### 6. Plan-and-execute (frozen plan)
**What:** Planning and execution are separate phases. The plan is investigated,
synthesized, critiqued, and *frozen* before the first line of code. Execution
failures are handled by fixing code or tests — never by silently re-planning.
**Where:** graph topology — no edge from execution loop back to planner (except a
human plan-gate reject).
**Why it's good:** A frozen plan is what makes bounded autonomy provable: with a fixed
microtask list, the per-microtask attempt cap bounds *total* work (this is why a
run-wide loop cap was deliberately rejected, ADR 0031). It also makes the plan a
reviewable artifact for the human gate — you can't approve a plan that keeps changing.
**Cost:** A genuinely wrong plan can't self-heal mid-run; it fails and a human
re-triggers with better framing. Accepted: cheap runs make retry cheaper than
re-planning complexity.

### 7. Structured task decomposition (microtasks as contracts)
**What:** The Planner's output is not prose — it's typed `Microtask` objects:
description (what/where), approach (how), `target_files` declared *before* coding,
`acceptance_criteria` as structured `AcceptanceCriterion` + concrete `IOExample`s.
A deterministic quality gate (`quality.py`) rejects malformed microtasks and feeds
the validation error back into the next synthesis attempt.
**Where:** `state.py::Microtask`, `agents/Planning/Planner/quality.py`.
**Why it's good:** The microtask is the *contract between three agents*: Coder
implements it, Tester derives tests from its criteria, Reflector diagnoses against
it. Structure is what lets each of them consume it deterministically. The
`IOExample`s specifically exist because an LLM cannot reliably compute expected
values — planner-supplied data is the strongest grounding against tautological tests
(ADR 0022).
**Cost:** Schema churn: every improvement to the contract touches planner prompts,
quality gate, tester synthesis, and a migration.

---

## B. Grounding patterns — how the system avoids believing itself

### 8. Two-phase agents: investigate → synthesize
**What:** Planner and Reviewer both run a read-only tool-using investigation phase
first, then a separate synthesis/verdict phase over what was found.
**Where:** `agents/Planning/Planner/planner.py`, `Reviewer/reviewer.py`; ADR 0007
(investigation stays unstructured — findings are free-form, output is structured).
**Why it's good:** Separating "look" from "decide" stops the model from generating a
plan out of its training-data memory of what repos usually look like. The reviewer
investigating *independently* (own tools, own context) is what makes its critique
worth anything.
**Cost:** Two LLM phases per agent per iteration.

### 9. ReAct tool loops (never trust recall)
**What:** Every agent that needs facts about the repo gets them through gateway-routed
tools (read file, exec, semantic search) in a reason-act loop, bounded by a
per-agent `max_tool_steps` knob.
**Where:** `*_tools.py` per agent; step caps in `Knobs` (`state.py`).
**Why it's good:** The sandbox is the reality; the loop forces the agent to touch
reality before acting. Bounded steps make a wandering investigation fail fast and
recoverable instead of spinning.
**Cost:** Token-hungry; the step cap is a quality/cost dial that needs tuning per
model.

### 10. Grounded verification (the load-bearing pattern)
**What:** Nothing is accepted on an LLM's word. The Tester's PASS/FAIL is computed
from exit codes: the repo's existing suite, build, typecheck, and synthesized tests
(Track A + Track B, ADR 0015/0016). There is deliberately **no** LLM code-review
acceptance step anywhere in the loop.
**Where:** `agents/Implementing/Tester/phases/` pipeline; verdict from
`evidence.checks`.
**Why it's good:** This is the single most important choice in the system. Every
other pattern (reflection, lessons, HITL) sits *downstream* of a verdict that is
actually true. An LLM-judged acceptance would poison all of them: reflections would
diagnose phantom failures, lessons would encode wrong conclusions, humans would
approve unverified work. Deterministic ground truth is what makes autonomy
compounding instead of compounding error.
**Cost:** Needs a runnable repo (build/test infrastructure in the sandbox image);
quality degrades on repos where nothing is executable — mitigated by pattern 11.

### 11. Independent test generation (don't grade your own homework)
**What:** Tests are synthesized by the *Tester* from the *Planner's* acceptance
criteria — the Coder's implementation is not an input. A deterministic AST/regex
guard (`grounding.py`) rejects tautological or self-computed assertions; without a
planner-supplied `IOExample`, tests may assert structure only, never a value the LLM
computed itself.
**Where:** Tester `SynthesisPhase`; guard in `grounding.py`; ADR 0016/0022.
**Why it's good:** Three-way separation (spec author ≠ implementer ≠ test author)
recreates the review discipline of a human team. The "structure-only without
examples" rule encodes a hard truth about LLMs — they can't reliably compute
expected values — as a mechanical rule instead of a hope.
**Cost:** Test quality is bounded by planner criteria quality; vague criteria produce
weak tests.

### 12. Baseline-delta testing
**What:** The number of failing tests in the repo's *pristine* suite is captured once
per run (`suite_baseline_failures`); the suite check gates only on failures **beyond**
that baseline.
**Where:** `state.py::suite_baseline_failures`; Tester deterministic-checks phase
(GH #28/#72).
**Why it's good:** Real repos ship red tests. Without the baseline, every microtask
on such a repo fails through no fault of the agent; with it, the agent breaking
*other* existing tests is still caught. Grounding calibrated to reality, not to an
idealized repo.
**Cost:** A pre-existing failure that the task was *supposed* to fix is invisible to
this check (the synthesized tests cover that case instead).

### 13. Evidence-gated progress
**What:** Claims of work are verified mechanically at each seam: after the Coder's
loop, a deterministic `repo.diff` must be non-empty (empty → re-prompt, capped); the
target files must compile (`py_compile` / `node --check`, warm in-loop retry, capped)
before hand-off to the Tester.
**Where:** `orchestrator/coder_node.py`; ADR 0008, GH #27.
**Why it's good:** LLMs happily report success without acting. "Show me the diff" is
cheaper than a full test cycle and catches the failure at the moment it happened. The
compile gate keeps trivial syntax errors from burning a full tester round-trip.
**Cost:** None worth mentioning — this pattern is nearly free and pays every run.

---

## C. Self-correction patterns — what happens on red

### 14. Failure-only reflection
**What:** The Reflector runs **only** when the Tester fails a microtask. On green
there is no reflection, no LLM review, no approval step — the Tester's deterministic
verdict advances the run directly.
**Where:** `_tester_router` (REFLECT → reflector); ADR 0016.
**Why it's good:** Two reasons. *Economics:* a green pass costs zero extra LLM calls
— at 91% first-pass yield that's the common path. *Epistemics:* an LLM "approving" a
green result adds no information (the checks already passed) but adds a chance to
hallucinate an objection. Diagnosis effort goes exactly where there is something to
diagnose, with real artifacts (test output) to ground it.
**Cost:** No opportunistic "this passed but smells bad" review — deliberately traded
away; the human CODE gate exists for that.

### 15. Self-diagnosis routing (`fix_target`)
**What:** The Reflector's diagnosis routes the retry: `fix_target=code` → the Coder
fixes the implementation; `fix_target=test` → the *Tester re-synthesizes the test*,
because sometimes the synthesized test is what's wrong. The test path is bounded
(`REFLECTOR_MAX_TEST_RESYNTH`), then forced back to code.
**Where:** `_reflector_router`; `test_resynth_count` in state; GH #33, ADR 0022.
**Why it's good:** A system that can conclude "my own test was wrong" without a human
is rare and valuable — without this, one bad synthesized assertion dead-loops the
coder against an unsatisfiable target. The diagnosis prompt injects the acceptance
criteria + the failing test source so the call is grounded, not vibes (ADR 0022).
**Cost:** A new loop to bound (hence the cap and the coder-run reset).

### 16–20 → see Safety section below; recovery continues at pattern 24.

---

## D. Safety patterns — why autonomy is allowed at all

### 16. Deny-by-default tool gateway
**What:** Every environment action by an LLM agent passes through `ToolGateway`:
per-role tool allowlists + per-role sandbox *command* allowlists (argv[0] checked;
commands must be argv lists — no shell strings). Anything not allowlisted is denied
and the denial is logged. Every call emits `tool.executed`/`tool.denied`.
**Where:** `tool_gateway/gateway.py`, `permissions.py`; ADR 0009.
**Why it's good:** Prompts are suggestions; the gateway is physics. "The Coder must
not run tests" is enforced by permission, not by asking nicely — prompt injection in
repo content can talk the model into *wanting* something, but not into *doing* what
its role can't. Denials being logged makes the fence observable (a dashboard stat).
Non-agents (GitOps, Control API) deliberately bypass it — the gateway is a control on
*LLM* behaviour, not on trusted deterministic code (ADR 0012/0013).
**Cost:** Every new tool/command touches the permissions matrix — again, deliberate
friction.

### 17. Sandbox-per-run isolation
**What:** Each run gets its own Docker container; all file edits and command
executions happen inside it; container IDs live in the DB (survive restarts).
**Where:** `sandbox/sandbox_tools.py`, `sandboxes` table.
**Why it's good:** Worst case for a misbehaving run is one disposable container —
not the host, not another run. Also gives reproducibility (clean environment per
run) and makes hard-stop safe (kill the container).
**Cost:** Docker dependency, image maintenance, per-run startup latency.

### 18. Sandbox as source of truth
**What:** Semantic memory (`code_chunks`) is used for *search only*. Any agent that
needs file content reads it from the sandbox at that moment; nothing is ever
reconstructed from embeddings or from earlier conversation context.
**Where:** `db/semantic_store.py` docstrings; convention in all agent tools; ADR 0010.
**Why it's good:** Retrieval indexes go stale the moment the Coder writes a file.
One authoritative copy means no split-brain between what the index believes and what
the code is. The workspace namespace is re-synced after each diff *for search
freshness*, not for truth.
**Cost:** More sandbox reads (cheap, local).

### 19. Bounded autonomy — every loop has a ceiling
**What:** Planner↔Reviewer capped (`max_planner_reviewer_iterations` → run FAILED);
coder↔tester↔reflector capped **per microtask** (`max_microtask_attempts` → FAILED);
test-resynth sub-loop capped; tool steps capped per agent; run-wide **token budget**
(`max_total_tokens`) checked at the top of every node. Caps are enforced *in nodes*
(which can write DB state and emit events), never in routers.
**Where:** `reviewer_node.py`, `coder_node.py::_check_stop_limit`,
`orchestrator/token_budget.py`; ADR 0025/0031.
**Why it's good:** Autonomous loops + LLM nondeterminism = unbounded spend unless
every cycle provably terminates. The design insight (ADR 0031): with a frozen plan,
per-microtask caps bound the whole run — no global loop counter needed. The token
budget is the money-denominated backstop for everything the iteration caps don't see.
**Cost:** A cap that's too tight kills a run that one more attempt would have saved —
tunable per run for that reason.

### 20. Human-in-the-loop gates (suspend, don't poll)
**What:** Three optional gates (plan / code / deploy) configured per run. A gated
phase's advance status is rewritten to `NEEDS_HITL_APPROVAL`; the graph suspends via
LangGraph's native `interrupt()` and resumes with the human's decision. Reject
**requires a note**; the note is routed into the exact retry channel (plan feedback /
seeded `Reflection`) — a rejection without actionable content re-suspends.
**Where:** `orchestrator/hitl.py`, `_hitl` node; ADR 0006.
**Why it's good:** The gate is *in* the state machine, not bolted on: the pause
survives process restarts (it's a checkpoint), resume advances exactly one gate, and
the human's note enters the same feedback machinery agents use — human correction and
agent self-correction share one pipeline. Reject-requires-note encodes a philosophy:
un-actionable input never routes.
**Cost:** Wall-clock stalls on human latency (measured: ~150 s median gate wait —
visible on the dashboard, which is itself the argument for keeping gates optional).

---

## E. Memory patterns — how the system remembers

### 21. Layered memory (four stores, four jobs)
**What:**
| Layer | Store | Lifetime | Job |
|---|---|---|---|
| Working | blackboard → `checkpoints` | one run | live state machine |
| Episodic | `events`/`artifacts`/`microtasks` | forever | audit: what happened |
| Semantic | `code_chunks` (pgvector) | per repo/run | find relevant code |
| Lessons | `lessons` (pgvector) | per repo, cross-run | don't repeat mistakes |
**Why it's good:** Each layer has different truth semantics — conflating them is the
classic MAS memory mistake. Working state is authoritative-now; episodic is
append-only history; semantic is a *hint* (see pattern 18); lessons are *advice*.
Because each is labeled with its own reliability, an agent can be given all four
without confusing "what is" with "what was" or "what might help".
**Cost:** Four write paths to maintain; pgvector dependency.

### 22. Confirmation-gated learning
**What:** Lessons are written only when an *outcome validates them*: plan lessons on
reviewer approval, code lessons on tester PASS (and only when the reflection was
code-targeted and flagged storage-worthy). Test-scoped lessons are a deliberate
no-op. Retrieval is top-k semantic, always excluding the current run, injected as
clearly-advisory prompt blocks.
**Where:** `db/lessons.py`; write sites in reviewer/tester nodes; ADR 0018.
**Why it's good:** The failure mode of naive agent memory is *confidently wrong
experience* compounding across runs. Gating writes on deterministic confirmation
(pattern 10 again — grounding upstream of memory) means the store accumulates only
conclusions reality agreed with. Measured effect on this repo set: runs starting
with repo memory outperform first-contact runs on success rate, attempts, and
first-pass yield.
**Cost:** Slow accumulation (~0.3 lessons/run) — by design. No causal effectiveness
tracking yet (known open item).

### 23. Human-curated memory (operator rules)
**What:** A human can type rules into the lesson store (`agent="human"`). These are
**pinned**: always injected (unranked, newest-N cap), framed as "follow these" above
the advisory lessons — deliberately *not* vector-searched. Human-touched lessons are
immune to bulk retraction; displaced rules are visibly marked inert in the UI.
**Where:** `db/lessons.py::find_operator_rules`; ADR 0034.
**Why it's good:** A fresh human instruction must never silently lose a top-k
relevance race to old agent lessons — if the operator said it, the agents see it,
deterministically. The "stored-but-inert must look inert" rule reflects a deeper
principle used throughout: **the UI must never imply an effect the system isn't
having.**
**Cost:** Pinned injection spends prompt tokens on every run whether relevant or not
(hence the cap).

---

## F. Execution-engineering patterns — the substrate

### 24. Checkpointed execution (crash-anywhere, resume-anywhere)
**What:** The full blackboard is checkpointed to Postgres after every graph superstep
(LangGraph PostgresSaver, `thread_id = run_id`). HITL pauses, crashes, budget stalls,
and process restarts all resume from the last checkpoint. Replaced a hand-rolled
snapshot table (ADR 0005).
**Why it's good:** Reproducibility as a property of the substrate, not of careful
application code. Every recovery feature in the system (stall/continue, HITL resume,
stop semantics) is a thin layer over this one mechanism. Buying it from the framework
instead of hand-rolling eliminated a whole class of "state written at the wrong
moment" bugs.
**Cost:** Checkpoint blobs are opaque-ish; hot fields the dashboard needs are
denormalized onto `runs` (tokens, caps) rather than deserialized per poll.

### 25. Stall, don't fail (recoverable-error taxonomy)
**What:** Errors are classified by recoverability. Policy violations (loop caps,
secrets, attempts exhausted) → **FAILED**, terminal. Transient LLM errors, crashes,
token-budget exhaustion → **STALLED**, non-terminal: checkpoint intact, sandbox kept
alive, human continues via `/continue` (optionally adding budget). Human stop →
**STOPPED**.
**Where:** `Service._drive` exception handling; `TokenBudgetExceededError` vs
`RunExecutionError`; ADR 0021/0023/0025.
**Why it's good:** An autonomous run that has burned ten minutes and $0.30 of
progress should not be destroyed by a 429. Making *recoverable* the default for
anything non-culpable, and pairing it with checkpoints (24), produced a measured 100%
stall-recovery rate. The taxonomy also keeps FAILED meaningful: it always means "the
system decided this must not continue", never "bad luck".
**Cost:** Stalled runs hold a live container until resolved (teardown deliberately
skips them).

### 26. Idempotent bootstrap outside the graph
**What:** Environment readiness (Run row, sandbox running, repo cloned, baseline
ingested) is a directly-called, idempotent step before every `graph.invoke()` — *not*
a graph node.
**Where:** `orchestrator/bootstrap_node.py`; ADR 0005.
**Why it's good:** Checkpoint resume continues from the last completed node, so a
bootstrap *node* would be skipped exactly when re-verification matters most (resume
after a crash, when the sandbox may be gone). Rule of thumb encoded here:
**checkpointable work goes in the graph; environment invariants that must re-verify
on every entry go outside it.**
**Cost:** Bootstrap effects aren't part of graph history (they emit events instead).

### 27. Resolve-once configuration
**What:** A run resolves its entire config exactly once at creation — LLM providers/
models/knobs into `RunConfig`, `auto_create_pr`, commit message, push branch — and
carries it on the blackboard. Agents take config as a **mandatory argument**; the
`or resolve()` fallback lives only at the node boundary. Mid-run settings changes
never touch a live run.
**Where:** `resolve_run_config`, `state["config"]`; ADR 0020/0035.
**Why it's good:** Two invariants. *Reproducibility:* the checkpoint fully describes
the run — replaying it can't pick up different settings. *Safety coherence:* run-start
preflights (provider health, GitHub push/PR rights, ADR 0029) validated *specific*
values; acting later on freshly-read values would deploy under conditions never
checked. Deeper principle: **validate and act on the same snapshot.**
**Cost:** Changing a live run's model requires an explicit, evented mechanism instead
of an ambient settings read — that's the feature.

### 28. Pure routers, effectful nodes
**What:** Routers only read `status` and return an edge name. All side effects — DB
writes, `Run.status` updates, event emission, cap enforcement — happen in nodes.
**Where:** convention across `orchestrator.py`; caps enforced in `*_node.py`.
**Why it's good:** LangGraph can re-evaluate routing during resume; a side effect in
a router would fire twice. Effects-in-nodes keeps every effect attached to a
checkpointed superstep — the audit trail and the state machine can't drift.
**Cost:** Occasionally awkward (a cap is *checked* where the loop is entered, not
where it's routed) — worth it.

### 29. Calibrated failure handling (hard-block vs degrade)
**What:** Failure severity is chosen per consequence, not uniformly. A gitleaks
finding before push **hard-blocks** (run FAILED — a leaked secret is irreversible).
A PR-creation failure after a successful push **degrades** (branch exists, error
recorded, run still DONE — a missing PR is a one-click human fix). Same node, two
policies.
**Where:** `gitops/` deploy path; ADR 0019/0027.
**Why it's good:** "Fail-closed everywhere" sounds rigorous but teaches operators to
ignore failures; "fail-open everywhere" is negligent. Matching the response to the
*irreversibility of the consequence* is the mature version — and it generalizes: the
push-branch guard (default branch refused at four independent layers, ADR 0035) is
the hard-block end of the same dial.
**Cost:** Each seam needs an explicit severity decision — which is exactly what the
ADRs record.

---

## G. What was deliberately NOT done (anti-patterns avoided)

These absences are choices, documented so future-you doesn't "fix" them:

- **No LLM-as-judge acceptance.** Considered repeatedly (reflector-as-judge, GH #26);
  the acceptance seam stays deterministic. See pattern 10 for why everything depends
  on this.
- **No direct agent-to-agent communication.** Star topology is a stated
  non-negotiable; "simplifying" it destroys auditability.
- **No re-planning mid-execution.** Frozen plan is what makes termination provable
  (pattern 6 / ADR 0031).
- **No run-wide execution-loop cap.** Redundant given per-microtask caps + frozen
  plan; a dead counter field remains as a reminder (ADR 0031).
- **No SaaS-scale infrastructure.** Single-user self-hosted deployment → events-table
  polling + in-memory SSE log bus, no broker, no LISTEN/NOTIFY, no resumable streams
  (ADR 0013). Right-sizing is a design value in this codebase: reuse the existing
  logger/tables over net-new moving parts.
- **No memory-as-truth.** Embeddings never reconstruct code (pattern 18); lessons are
  advisory, never binding (except explicit human rules, pattern 23).
- **No silent FAILED on crashes.** Recoverability is the default for non-culpable
  errors (pattern 25).

---

## H. How the patterns compose (the short story)

Roles (1) with independent critics (2, 11) propose work; a deterministic verdict (10,
12, 13) decides; failure routes through grounded diagnosis (14, 15) inside hard
ceilings (19); everything communicates through one observable state (3, 4, 5) that is
checkpointed every step (24), so any interruption — crash, budget, human gate (20),
stop — is a resumable pause (25). What reality confirms becomes memory (21, 22, 23),
so the next run on the same repo starts smarter. The gateway (16) and sandbox (17)
mean the worst possible outcome of all of the above is a denied call or a discarded
container — which is why the autonomy is acceptable in the first place.

Confidence comes from the verification patterns; safety from the containment
patterns; improvement from the memory patterns; and trust — the human kind — from the
audit trail that all of them write to.
