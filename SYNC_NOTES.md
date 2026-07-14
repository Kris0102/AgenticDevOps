# Sync notes — progress since last meeting

Last sync was **before the dashboard** and **before the phase-based Tester** (≈ 2026-06-24).
Everything below landed since. Owner tags: **K** = Kristian, **S** = Stefan.
Detail lives in `docs/decisions/` (ADRs) — refs in brackets.

---

## Verification / agents

- **Phase-based Tester** — ordered `TestPhase` pipeline (detect → deterministic checks →
  synthesis → guard → execute → verdict). Two-track grounding: runs the repo's own suite +
  build/typecheck/lint, AND LLM-synthesizes tests from structured `acceptance_criteria`,
  guarded by a deterministic assertion check. Tester owns its PASS/FAIL verdict. [ADR 0016] **(K)**
- **Failure-only Reflector** — only runs on a tester FAIL; diagnoses, routes `fix_target`
  code→coder or test→tester (re-synthesize wrong test, bounded). No LLM call on a green pass.
  [ADR 0016, GH #33] **(K)**
- **Coder compile gate** — after the diff, compiles target files (`py_compile`/`node --check`)
  and re-prompts warm on failure. [GH #27] **(K)**
- **Value-grounding fixes** — synthesis asserts structure-only when the Planner gave no
  IOExample; reflector diagnosis now sees the criteria + failing test → correct test/code
  routing. Stops unwinnable coder↔tester↔reflector loops. [ADR 0022, GH #44] **(K)**
- **Planner↔reviewer convergence** — planner sees only the latest review verdict; structured
  `blockers` forwarded instead of free-text notes. [ADR 0022, GH #43] **(K)**

## Dashboard + Control API (the big one)

- **FastAPI control plane** — trigger runs, HITL approve/reject, live logs (SSE), durable
  event history, artifacts/microtasks/lessons feeds. One `graph.invoke()` per gate. [ADR 0013] **(K)**
- **React dashboard** — run-history home, live SVG pipeline graph, status/gate panels,
  plan + green/red diff review at HITL gates, export/import, settings + repos pages. **(K)**

## Settings + providers + secrets

- **Settings hub** — DB-backed providers/model/tuning knobs, per-run override, UI-managed;
  `.env` is seed/fallback only. Provider preflight refuses to start on a dead/mis-keyed
  provider. [ADR 0020] **(K)**
- **Provider abstraction** — OpenAI-compatible + Anthropic via one factory. **(K)**
- **Secrets at rest** — Fernet-encrypted provider keys + GitHub token, masked in the API.
  [ADR 0021] **(K)**

## Memory

- **Per-repo lessons** — cross-run failure memory (pgvector), written on confirmation
  (reviewer approve / tester pass), injected advisory into reviewer/coder/reflector. [ADR 0018] **(K)**
- **Repo-context artifacts** + baseline semantic index reuse. **(K)**

## Deploy / GitOps

- **Deploy hygiene** — seeded `.gitignore`, push-time pathspec excludes, **gitleaks** secret
  scan hard-blocks a push on any finding. [ADR 0019] **(K)**

## Reliability

- **STALLED run recovery** — crash/worker-death leaves a run continuable via
  `POST /runs/{id}/continue`, not FAILED. **(K)**
- **Transient LLM classification** — 429 "overloaded" / 5xx / timeouts → STALL (recoverable),
  no more degrading past a failed investigation. [ADR 0023] **(K)**

## Newest (this week)

- **Repo registry** — repos registered/validated/picked separately from run creation;
  canonical URLs, per-repo encrypted token, live branch picker, base-branch selection,
  per-repo push override with default-branch push hard-refused. [ADR 0024, GH #41] **(K)**
- **Token usage limit** — end-to-end token accounting per agent + per microtask, a run-wide
  budget that STALLS (not fails) on exhaustion and is raise-and-continue recoverable; live
  dashboard tiles + per-agent/per-microtask breakdown. [ADR 0025, PR #50] **(S)**
- **Duration fix** — stalled idle time no longer inflates run duration / Gantt. [ADR 0026, PR #51] **(S)**

## In progress / next

- **Guided task-description builder** — optional step-by-step interview (repo-grounded via the
  Context Engine) that fills the structured Objective before a run, to fix under-specified
  tasks. Designed + filed, not yet built. [GH #49] **(K)**

---

*Migration note: `alembic upgrade head` needed locally — 2 pending (token columns).*
