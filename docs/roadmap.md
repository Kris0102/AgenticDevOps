# AgenticDevOps — Roadmap & Future Work

> `docs/decisions/` (why). This is *what's next* — planned features, known
> scaling limits, and the ideas we'd pursue with more time. Ordered by horizon,
> each item tagged with rough **impact** and **effort**.

Legend — Impact: 🔴 high · 🟡 medium · ⚪ nice-to-have. Effort: S / M / L / XL.

---

## Horizon 1 — Near term (unblocks the next real use)

### 1.1 Run independent microtasks in parallel · 🔴 · L
**Today:** a run is single-threaded — microtasks execute strictly one after another,
even when they touch disjoint files and have no dependency between them. This is the
single biggest wall-clock cost.
**Plan:** the Planner already emits `deps` per microtask — treat the microtask list as
a **DAG** and schedule independent nodes concurrently. A microtask with no unmet
dependency can start as soon as a worker is free.
**The hard part (be honest about it):** ADR 0032 deliberately keeps *within-run*
execution single-threaded — one checkpoint chain, one sandbox as source of truth.
Parallel microtasks break that assumption:
- **Sandbox contention** — two coders editing the same working tree corrupt each
  other's diff. Options: (a) one sandbox, file-level locking + disjoint-file
  guarantee from the DAG; (b) per-microtask worktrees/branches merged back (cleaner
  isolation, needs a merge step + conflict handling).
- **Checkpoint model** — the blackboard assumes a linear superstep. Concurrent
  microtasks need either a fan-out/fan-in graph region or per-microtask sub-states
  merged on join.
- **Verdict ordering** — the Tester baseline + delta-gating (GH #28/#72) assumes
  serial mutation; concurrent changes need per-microtask isolated verification.
**Suggested first step:** DAG scheduling with **per-microtask git worktree** isolation
inside the one container, join = sequential fast-forward merge with a conflict →
fall back to serial. Bounded parallelism (2–3) to start.

### 1.2 SWE-bench evaluation harness · 🔴 · M
**Status:** started — `scripts/swebench/` + branch `feat-SWE-Benchmark-run`.
**Plan:** formalize a repeatable benchmark: run on SWE-bench Lite (and later
Verified), produce the standard `predictions.jsonl`, score **% resolved**, and track
it as the North-Star regression metric across changes. Wire the adapter so a benchmark
task maps cleanly onto a run (repo + base SHA + problem statement → objective).
**Why it matters:** turns "feels better" into a number. Every reliability fix in the
dossier should show up as a delta here. Also the strongest external credibility signal.
**Watch:** cost + time per instance (see 1.1 and Horizon 3 speed work); isolate the
harness so a benchmark sweep doesn't need the dashboard.

### 1.3 Extend the Tester to more languages · 🟡 · M
**Today:** Track A + Track B are solid for Python and JavaScript/TypeScript
(framework-aware: node:test / jest / vitest / mocha; py_compile / pytest; subproject
build). Other languages fall through to weak or no deterministic checks.
**Plan:** add first-class detection + build/test/typecheck runners per language:
- **Go** — `go build ./...`, `go test ./...`, `go vet`.
- **Rust** — `cargo build`, `cargo test`, `cargo clippy`.
- **Java/Kotlin** — Maven/Gradle detection → `mvn test` / `gradle test`.
- **Ruby** — `rspec` / `rake test`.
**Shape:** each language is a `LanguagePlugin` (detect → compile-check → suite → build),
so `DeterministicChecksPhase` dispatches by detected language instead of hard-coded
Python/JS branches. Track B's synth prompt + assertion guard already generalize; the
per-language assertion regex + import style is the main work.

---

## Horizon 2 — Mid term (opens the product up)

### 2.1 MCP tools on remote (mcpo) servers instead of local subprocess · 🟡 · M
**Today:** MCP tools run **locally, in a subprocess** of the app. Fine right now
**because users can't add their own tools** — the tool set is fixed and trusted.
**Plan:** move tool hosting to **mcpo servers** (MCP-over-HTTP), so tools run as
independent, network-addressable services rather than child processes.
**Why (the trigger):** the moment users can register their **own** tools, running
untrusted tool code in-process is a security and stability risk. Remote mcpo servers
give: process/network isolation per tool, independent scaling + restart, and a clean
allowlist boundary the Tool Gateway can enforce at the network edge.
**Sequencing:** not needed until user-supplied tools land — but design the Tool Gateway
seam now so the local→remote switch is a transport swap, not a rewrite.
**Ties into:** the Tool Gateway's deny-by-default model (ADR 0009) extends naturally to
per-tool network egress rules.

### 2.2 User-supplied / per-repo custom tools · 🟡 · L
The feature that *makes 2.1 necessary*. Let an operator register extra tools (linters,
project-specific scripts, internal APIs) scoped to a repo. Requires: the remote mcpo
hosting (2.1), a per-tool permission entry in the role allowlist, and sandboxed
execution with resource + egress limits.

### 2.3 GitHub webhook trigger + PR-review ingestion · 🟡 · L
**Today:** runs are triggered by API/dashboard; deploy pushes a branch and can open a
PR, but there's **no inbound webhook path** and no CI/merge relay (ADR 0012 open).
**Plan:** (a) trigger a run from a GitHub webhook (issue labelled, PR comment); (b)
ingest human PR-review comments back into the loop as structured feedback the Reflector
can act on; (c) relay CI status. Closes the loop from "proposes a PR" to "responds to
review."

### 2.4 Reflector-as-judge & lesson-retrieval reinforcement · ⚪ · M
- **Reflector-as-judge (GH #26, under discussion):** let the Reflector adjudicate
  borderline Track-B failures with the criteria in hand — carefully, without breaking
  the "no LLM-only verdict" rule (advisory, never gating).
- **Lesson usefulness feedback:** today lessons are retrieved top-k but there's no
  signal on whether an injected lesson actually helped. Reinforce retrieval with a
  did-this-prevent-a-failure signal; prune lessons that never help.

---

## Horizon 3 — Longer term / bigger bets

### 3.1 Speed — make runs fast, not just correct · 🔴 · L (cross-cutting)
Beyond 1.1's parallelism, the system is slow for several compounding reasons; each is
its own lever:
- **Model routing by difficulty** — use a cheap/fast model for trivial microtasks and a
  strong model only for hard reasoning (planning, tricky reflection). Per-agent model
  selection already exists (ADR 0030); add *automatic* routing by task signal.
- **Cache the investigation** — the Planner/Reviewer re-investigate from scratch; cache
  the baseline repo understanding (the Context Engine already indexes it) so
  investigation is retrieval, not re-reading.
- **Speculative / thinking-budget tuning** — thinking models spend minutes on synthesis
  (ADR-0023 timeout story); tune thinking budget per agent instead of a flat large cap.
- **Skip green work** — already partly done (no LLM call on a tester PASS); extend the
  principle wherever a deterministic answer exists.

### 3.2 Observability — traces, not just logs · 🟡 · M
Add OpenTelemetry spans per node/tool so a slow or failed run is a *trace*, not a log
scrape. Feeds the speed work (3.1) with real per-phase timing and the Grafana dashboards
already in place.

### 3.3 Monorepo / multi-package awareness · ⚪ · L
The subproject-build fix (ADR 0037) was the first step. Generalize: a change in a
monorepo should detect the affected package(s), run only their checks, and understand
cross-package dependencies.

### 3.4 Richer sandbox security · 🟡 · M
Network egress controls on the sandbox (deny-by-default outbound), resource quotas, and
a per-run policy — matters most once user tools (2.2) run arbitrary code.

### 3.5 Incremental re-planning · ⚪ · M
When a microtask reveals the plan was wrong, allow a **targeted** re-plan of the
affected subtree instead of failing or forcing a full planner↔reviewer cycle.

---

## Cross-cutting engineering debt (from the dossier's "honest edges")

- **Per-test-id baseline diffing** — the suite baseline compares failing *counts*; a
  swap (fix one, break another) slips through. Move to per-test-id.
- **Zombie-run relabel** — a run orphaned by a killed worker should derive STALLED and
  be continuable without a manual server restart.
- **Collapse dead iteration counters** — `global_iteration` / `global_iterations_used`
  are redundant.
- **Real worker for runs** — move off `BackgroundTasks` before any multi-user use.

---

## How to prioritize (suggested order)

1. **SWE-bench harness (1.2)** first — you need the metric before you can prove any of
   the rest helped.
2. **Speed via model routing + investigation cache (3.1)** — cheapest wins, no
   architectural risk.
3. **Parallel microtasks (1.1)** — biggest speedup, but the highest architectural risk;
   do it *after* the benchmark exists so you can measure the correctness trade-off.
4. **Multi-language tester (1.3)** — widens applicability, self-contained.
5. **Remote MCP + user tools (2.1 / 2.2)** — only when user-supplied tools are actually
   on the table.
