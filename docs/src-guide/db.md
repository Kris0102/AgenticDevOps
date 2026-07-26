# src/db/ — Persistence: Audit Trail, Memory, Settings

**Role in the MAS:** everything durable. Postgres holds the full audit trail of
every run, the planner's persisted output, the system's *cross-run memory*
(lessons + semantic code index via pgvector), the repo registry, and the
UI-managed settings hub. SQLAlchemy 2.0 style throughout (`Mapped[T]` +
`mapped_column()`).

## Key files

| File | What it does |
|---|---|
| `db.py` | Engine + `session_scope()` — commit ownership always sits with the scope opener |
| `models.py` | All ORM models: `Run`, `Event`, `Artifact`, `Sandbox`, `MicrotaskRecord`, `CodeChunk`, `Lesson`, `RepoRecord` |
| `event_log.py` | `EventLogger` — the *only* way to write an `Event` row. Handles id + timestamp, only calls `session.add()` (never commits) — keeps transaction boundaries with the caller |
| `lessons.py` | `LessonStore` — per-repo cross-run failure memory (ADR 0018/0034), see below |
| `semanticMemory/embedder.py` | Lazy sentence-transformers (all-MiniLM-L6-v2, 384-dim, local — no API cost) |
| `semanticMemory/semantic_store.py` | `SemanticStore` — pgvector upsert/search/delete over `code_chunks` |
| `settings_store.py` | `SettingsStore` — the UI-managed settings hub (ADR 0020): `.env` values only *seed* the DB; after first boot the dashboard is the source of truth, per-run overrides win |
| `secrets.py` | Fernet encryption-at-rest for API keys / GitHub tokens; masked on read |
| `repo_store.py` | `RepoStore` — repo registry (ADR 0024): canonical URL normalization (the memory key for lessons + code chunks), per-repo token, token resolution chain repo → settings → anonymous |
| `migrations/` | Alembic — the schema's full history |

## Tables at a glance

| Table | Purpose |
|---|---|
| `runs` | One row per run: status, deploy mode, token totals |
| `events` | Full audit trail with a monotonic `seq` — the dashboard's durable history cursor |
| `microtasks` / `artifacts` | Planner output + plan summaries, diffs, test logs — survive crashes |
| `sandboxes` | Container-id registry (restart-proof) |
| `code_chunks` | Semantic code index (pgvector, baseline/workspace namespaces) |
| `lessons` | Cross-run failure memory (pgvector) |
| `repos` | Repo registry |
| `checkpoints*` | LangGraph PostgresSaver — the blackboard snapshot per superstep |

## The lessons store — the MAS's long-term memory

The most interesting piece here. Lessons are written **only on confirmation** — a
plan lesson when the reviewer approves, a code lesson when the tester passes — so
the memory stores validated outcomes, not speculation. Retrieval is semantic
(pgvector top-k, filtered to the repo and scope, current run excluded) and injected
*advisory* into the planner/reviewer/coder/reflector prompts.

Human curation on top (ADR 0034): operators can type **rules** in the dashboard
(`agent="human"`, no run id). Rules are deliberately *not* vector-ranked — they are
always injected, unranked, in their own "follow these" block, so a fresh rule can't
lose a similarity race and silently do nothing. Human-touched lessons are immune to
bulk retraction.

## Interview sound bites

- "The DB is the system's memory *and* its conscience: pgvector for what it
  learned, the events table for everything it did."
- "Lessons are outcome-validated — we only remember what was confirmed by a
  reviewer approval or a passing test, and humans can pin rules that always apply."
- "One event-writing path (`EventLogger`), one commit owner (`session_scope`) —
  transaction discipline is a convention the whole codebase follows."
