# Patterns Cheat-Sheet — What We Used and Why

Interview-ready summary. Full catalogue with costs/trade-offs: [`docs/MAS_PATTERNS.md`](../MAS_PATTERNS.md).
Format per entry: **what → where → why → the one-liner you can say out loud.**

---

## Coordination — how agents talk

### Star topology (orchestrator-mediated)
- **Where:** `orchestrator/orchestrator.py` — all communication flows through the graph; agents never call each other.
- **Why:** N agents talking peer-to-peer is N² untraceable conversations. One hub means one place for routing, logging, caps, and gates.
- **Say:** "Agents are leaves; the orchestrator is the only thing with control flow."

### Blackboard architecture
- **Where:** `orchestrator/state.py` — one Pydantic `BlackboardState`; nodes return only changed keys.
- **Why:** shared memory instead of message passing → every agent sees the same world, every change is a diffable, checkpointable event.
- **Say:** "Agents are pure functions over a shared state object — that's what makes the whole run replayable."

### Distinct specialized roles
- **Where:** Planner / Reviewer / Coder / Tester / Reflector, each its own class, prompt, and tool allowlist.
- **Why:** narrow jobs → narrow prompts → better performance, and *structural* separation of duties (the Coder can't run tests even if prompted to).
- **Say:** "Role separation is enforced by the gateway, not by politeness in the prompt."

### Actor–Critic (Planner ↔ Plan Reviewer)
- **Where:** `agents/Planning/` + planning loop in the graph.
- **Why:** a generator reviewing its own plan rubber-stamps itself. The critic investigates the repo *independently* with read-only tools, then approves/rejects with structured blockers.
- **Say:** "The reviewer doesn't read the planner's summary and nod — it re-derives the facts from the repo."

### Status-driven state machine routing
- **Where:** routers in `orchestrator.py` gated by per-edge expected-status allowlists (`NEEDS_CODE`, `REFLECT`, `READY_TO_DEPLOY`…).
- **Why:** next hop is derived from state, not from an LLM's opinion about what should happen next. Deterministic, testable control flow.
- **Say:** "The LLM decides *content*; the state machine decides *flow*."

### Plan-and-execute with microtask contracts
- **Where:** Planner emits frozen `Microtask`s (target files, approach, structured acceptance criteria + IO examples).
- **Why:** small verifiable units bound blast radius; acceptance criteria written *before* coding become the Tester's independent spec.
- **Say:** "The plan is a contract — the Tester tests against the plan, not against the code."

---

## Grounding — how the system avoids believing itself

### Grounded verification (the load-bearing pattern)
- **Where:** Coder verified by `repo.diff` + compile gate; Tester verdict computed from lint/build/test exit codes.
- **Why:** LLMs confidently report success. Code is accepted only when deterministic checks pass — no LLM-only review anywhere on the accept path.
- **Say:** "An agent's claim is never evidence. Diffs, compiles, and exit codes are."

### Two-phase agents: investigate → synthesize
- **Where:** Planner and Reviewer both ReAct-investigate with tools before producing structured output.
- **Why:** never trust parametric recall about a repo the model hasn't read this run.
- **Say:** "Look first, then speak — with the looking audited through the gateway."

### Independent test generation
- **Where:** Tester Track B synthesizes tests from the Planner's acceptance criteria, never from the Coder's code; guarded by a deterministic assertion check, structure-only unless the plan gave a concrete IO example.
- **Why:** don't grade your own homework; and never let an LLM invent expected values it can't compute.
- **Say:** "Tests come from the spec, not from the implementation."

### Evidence-honesty details
- **Where:** head+tail elision instead of blind truncation (`_elide_middle`), capped tool output (`tool_output.py`), UNVERIFIED-skip instead of false FAIL (ADR 0037).
- **Why:** a reflector once diagnosed "truncated implementation" on complete code because *we* cut the diff. Dishonest evidence poisons diagnosis.
- **Say:** "Even our truncation markers admit they're display cuts — the diagnosing agent must never be lied to by the harness."

---

## Self-correction — what happens on red

### Failure-only Reflector
- **Where:** graph edge `tester → reflector` fires only on FAIL.
- **Why:** no LLM call on green (cost), no blind retry on red (quality). Every retry is directed by a root-cause diagnosis.
- **Say:** "Green is free; red buys a diagnosis before it buys a retry."

### Self-diagnosis routing (`fix_target`)
- **Where:** Reflector emits `code` → coder retry, or `test` → tester re-synthesizes the wrong test (bounded).
- **Why:** sometimes the *test* is wrong. Without this route the system loops forever "fixing" correct code.
- **Say:** "The system can conclude its own test was the bug — that's the difference between retrying and learning."

---

## Safety — why autonomy is allowed at all

### Deny-by-default Tool Gateway
- **Where:** `tool_gateway/` — per-role tool + shell-command allowlists, every call logged (`tool.executed` / `tool.denied`).
- **Why:** capability boundaries in code, not prompts; complete audit trail.
- **Say:** "Every environment action passes one chokepoint that can say no and always writes it down."

### Sandbox-per-run isolation
- **Where:** `sandbox/` — fresh Docker container per run, DB-backed registry, sandbox = source of truth for code.
- **Why:** worst case an agent trashes its own container. Independent layer from the gateway → defense in depth.
- **Say:** "*What* an agent may do (gateway) and *where* it may do it (sandbox) are enforced independently."

### Bounded autonomy — every loop has a ceiling
- **Where:** per-microtask attempt cap, planner↔reviewer iteration cap, test-resynth cap, per-agent tool-step caps, run-wide token budget.
- **Why:** autonomous loops without ceilings are unbounded bills. Caps are enforced *in nodes* (auditable events), not hidden in routers.
- **Say:** "There is no loop in this system without a number on it."

### Human-in-the-loop gates (suspend, don't poll)
- **Where:** shared `hitl` node, LangGraph `interrupt()`; gates at plan / code / deploy, configurable per run.
- **Why:** graduated trust — human approves the plan, the code, and *always* sees the final diff before anything ships. Reject requires a note, and the note feeds the same channels agents already use.
- **Say:** "The graph physically suspends at a gate — approval isn't a flag check, it's the only way the run continues."

### Deploy-boundary safety (git hygiene)
- **Where:** `gitops/` — push only to run branches (default-branch push refused at four layers), **gitleaks secret scan hard-blocks every push**, pathspec excludes keep deps/secrets/synth-tests out, PR body + commit message deterministic (no LLM paraphrase at the approval surface).
- **Why:** the deploy edge is where the MAS touches the real world; it gets the most rails.
- **Say:** "The agent can never push to main, and nothing leaves the sandbox without a secret scan that fails the run on a hit."

### Stop, stall, and crash taxonomy
- **Where:** cooperative stop registry checked at the top of every node (ADR 0033); crashes and token exhaustion → STALLED (continuable), never silent FAILED (ADR 0025).
- **Why:** a human can always bound a live run; recoverable problems stay recoverable.
- **Say:** "Failure states are typed: stopped by human, stalled-recoverable, failed-with-reason — never a zombie."

---

## Memory — how the system gets better

### Layered memory, four stores
- **Where:** checkpointed blackboard (within-run), `events` (audit), `code_chunks` (semantic code index), `lessons` (cross-run).
- **Why:** different lifetimes and different queries — one store would serve all of them badly.

### Confirmation-gated learning
- **Where:** `db/lessons.py` — plan lessons written on reviewer APPROVE, code lessons on tester PASS. Never on speculation.
- **Why:** memory of *validated* outcomes; otherwise the system remembers its own guesses.
- **Say:** "We only learn what a green check confirmed."

### Human-curated memory (operator rules)
- **Where:** dashboard lesson store — human rules always injected, unranked (never lose a vector-similarity race); human-touched lessons immune to retraction.
- **Say:** "Agents' lessons compete on relevance; the human's rules don't compete at all."

### Semantic code memory kept honest mid-run
- **Where:** `context_engine/` — AST-chunked pgvector index, `baseline` + `workspace` namespaces, re-synced after every coder diff.
- **Why:** retrieval over stale code misleads every downstream agent.

---

## Execution engineering — the substrate

### Checkpointed execution
- **Where:** LangGraph PostgresSaver, snapshot per superstep, `thread_id = run_id`, connection pool for concurrent runs.
- **Why:** crash-anywhere / resume-anywhere; a HITL pause is just a suspended checkpoint, not a parked process re-doing setup.

### Pure routers, effectful nodes
- **Where:** convention across `orchestrator/` — routers only read state; all DB writes/events happen in nodes.
- **Why:** deciders with side effects are undebuggable; this keeps the graph replayable.

### Resolve-once configuration
- **Where:** `RunConfig` + `DeployMode` resolved at run creation onto the blackboard; agents take config as a mandatory arg, fallback only at the node boundary.
- **Why:** a settings change mid-run must not change a running run; what was preflighted is what executes.

### Idempotent bootstrap outside the graph
- **Where:** `bootstrap_node.py`, called by the launcher before the graph.
- **Why:** side-effect-free routers demand setup happen exactly once, up front — upsert run, ensure sandbox, clone, ingest baseline.

### Calibrated failure handling
- **Where:** secret found → hard-block (run FAILED); PR creation fails → degrade (code already pushed, run still succeeds); local export fails → FAIL (the deliverable *is* the export).
- **Say:** "Each failure mode got a deliberate severity decision — nothing defaults to 'log and continue'."

---

## Deliberately NOT done (know these for "why didn't you…")

- **No free-form agent-to-agent chat** — conversations don't converge; state machines do.
- **No LLM-as-judge on the accept path** — deterministic checks decide; LLM judgment only *routes* (reflector).
- **No dynamic re-planning mid-execution** — frozen plan + bounded retries; re-planning is a new run with lessons carried over.
- **No SaaS-scale infra** (queues, LISTEN/NOTIFY, resumable SSE) — right-sized to self-hosted single-user; ring buffer + seq-cursor poll suffice.
- **No agent framework beyond LangGraph** — graph + checkpointer only; agents are plain classes, so nothing important is hidden.

---

## The 30-second composition story

Human states an objective → Planner investigates and decomposes into microtask
contracts → an independent critic approves the plan (gate) → per microtask: Coder
edits in an isolated sandbox through an audited deny-by-default gateway → Tester
verifies with deterministic checks plus tests synthesized from the *plan* → red goes
to a failure-only Reflector that diagnoses code-vs-test before any retry → every
loop capped, every token budgeted → the final diff always crosses a human gate →
ships to a run branch behind a secret scan, never to main → confirmed outcomes are
written to per-repo memory so the next run starts smarter.
