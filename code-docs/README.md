# code-explanation/ — File-by-File Code Walkthroughs

Gitignored study folder: every `src/` package explained at code level — each
file's classes/functions walked through with line references, the WHY behind
non-obvious lines (usually quoting the in-code comments), and a relationship
map at the end of every document.

## Reading order (bottom-up — each layer builds on the previous)

| # | File | Covers | Lines of code |
|---|------|--------|---------------|
| 1 | [exceptions.md](exceptions.md) | `src/Exceptions/` — the failure taxonomy as a class hierarchy | ~105 |
| 2 | [sandbox.md](sandbox.md) | `src/sandbox/` — Docker mechanism, exec watchdog, diff filter, path validation | ~426 |
| 3 | [tool_gateway.md](tool_gateway.md) | `src/tool_gateway/` — roles, per-role policy, enforcement + audit | ~281 |
| 4 | [db.md](db.md) | `src/db/` — session discipline, models, EventLogger, LessonStore, semantic store, settings, secrets, repo registry | ~1600 |
| 5 | [context_engine.md](context_engine.md) | `src/context_engine/` — AST chunker + baseline/workspace engine | ~279 |
| 6 | [providers.md](providers.md) | `src/providers/` — LLM factory, provider quirks, token usage, pricing | ~355 |
| 7 | [observability.md](observability.md) | `src/observability/` — structlog pipeline, category filter, log bus | ~338 |
| 8 | [gitops.md](gitops.md) | `src/gitops/` — push/PR/bundle-export, gitleaks gate, GitHub probes, deterministic messages | ~769 |
| 9 | [orchestrator-core.md](orchestrator-core.md) | graph, state.py, HitlGate, bootstrap, checkpointer, token budget, stop/failure | ~2000 |
| 10 | [orchestrator-nodes.md](orchestrator-nodes.md) | the six Runner classes (shared shape + per-runner specifics) | ~1700 |
| 11 | [agents-planning.md](agents-planning.md) | Planner (+ quality gate) and Reviewer | ~1600 |
| 12 | [agents-coder.md](agents-coder.md) | Coder: empty-diff loop, warm compile gate, tools, prompt | ~780 |
| 13 | [agents-tester.md](agents-tester.md) | Tester: pipeline, 6 phases, grounding guard, verdict | ~1600 |
| 14 | [agents-reflector.md](agents-reflector.md) | Reflector: schema-as-policy, oscillation self-check, honest evidence | ~800 |
| 15 | [api.md](api.md) | routes, Service, _drive exception ladder, concurrency, token top-up | ~2855 |
| 16 | [grafana.md](grafana.md) | provisioned image + the cross-run SQL dashboard | config |

## How to study with these

- Each doc is written to be read NEXT TO the source file — line references are
  clickable in the IDE.
- The **Relationships** diagram at the bottom of each doc is the 30-second
  refresher version; the exam sentences are pre-phrased answers.
- Not covered: `src/main.py` (legacy CLI, being deleted), `src/output/` (debug
  dumps), `db/migrations/versions/` (schema history — read `db.md`'s models
  section instead), `dashboard/` (React frontend), prompt files beyond their
  role (long, read them raw if asked).
