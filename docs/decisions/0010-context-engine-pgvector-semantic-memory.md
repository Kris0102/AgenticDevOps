# 0010 — Context Engine: pgvector semantic memory for code (baseline + workspace)

Status: accepted
Date: 2026-06-15

## Context

`CLAUDE.md` listed "Context Engine (pgvector)" as Not started, and ADR 0001 (finding #8)
deliberately deferred it until core infra (stop conditions, transaction boundaries,
checkpointing) was fixed — that work is now done. ARCHITECTURE_preview.md §3.3/§4.2 specs
a semantic retrieval layer so agents can *find* code by meaning instead of brute-force
re-reading the whole sandbox on every plan. The `feat-semantic-memory` branch carried an
empty `SemanticStore` stub as the placeholder.

Non-negotiable in play (ARCHITECTURE §2.3): the **Sandbox is the source of truth for
code**. Semantic memory is search/retrieval only — never a substitute for reading the repo
or a means to reconstruct it.

## Decision

Build the **code** Context Engine (not reflection memory — that stays in the checkpointed
blackboard for now) as a read-only pgvector index.

- **Storage:** new `code_chunks` table in Postgres with a `Vector(384)` column. Postgres
  image switched to `pgvector/pgvector:pg15` (matches the existing pg15 data volume);
  a migration runs `CREATE EXTENSION vector`,
  creates the table, an HNSW cosine index, and scope btree indexes.
- **Embeddings:** local `sentence-transformers` `all-MiniLM-L6-v2` (384-dim), lazy-loaded
  singleton in `db/embedder.py`. In-process, offline, free — no dependency on the LLM
  cluster (which serves chat, not embeddings).
- **Chunking:** tree-sitter AST-aligned (`context_engine/chunker.py`) into function/class
  chunks; unsupported languages or parse failures fall back to a single whole-file chunk so
  the chunker is always total.
- **Two namespaces** (per spec): `baseline` (repo @ `base_sha`, ingested once at run start,
  keyed by repo_url+git_sha) and `workspace` (run-local edits, keyed by run_id, purged on
  sandbox teardown). `ContextEngine.search` overlays workspace on baseline, deduped by
  (filepath, symbol), workspace winning.
- **Ingestion triggers (synchronous — no filesystem watcher):**
  - baseline: in `BootStrapper` right after clone, idempotent (`has_baseline`), non-fatal.
  - workspace: in the coder node after a successful diff, re-chunking only `files_touched`,
    non-fatal.
- **Access:** new `semantic.search` gateway tool, granted to the Planner only. Flows through
  the same deny-by-default allowlist + mandatory `tool.executed` audit logging as every
  other tool; exposed to the planner's ReAct loop as a `semantic_search` StructuredTool.

## Alternatives considered

- **Reflection semantic memory first** (cross-run lessons via the inert
  `should_store_in_semantic_memory` flag). Smaller, but the user chose the code engine as
  the higher-value navigation win; reflection memory remains a clean follow-up reusing the
  same pgvector + embedder plumbing.
- **Cluster/OpenAI-compatible embeddings endpoint.** Rejected: the cluster serves chat only;
  a local model removes a network dependency and is deterministic for tests.
- **Filesystem watcher (inotify/watchdog) for workspace sync.** Rejected for now (ADR 0001
  "simplest viable"): orchestrator-driven synchronous re-chunk of `files_touched` is enough
  and avoids a background thread inside the run. A watcher is a future optimization.
- **Indexing only at the file level (no AST).** Rejected: function/class granularity gives
  far better retrieval precision; whole-file is kept only as the fallback.

## Consequences

- The Planner can locate code by meaning, reducing brute-force sandbox reads and tool-step
  budget. New agents (Coder/Reviewer) can be granted `semantic.search` later with one line
  in `permissions.py`.
- New runtime deps: `pgvector`, `sentence-transformers`, `tree-sitter`,
  `tree-sitter-languages`. First embed call downloads/loads the model (~80 MB).
- The Postgres image change requires a `docker compose pull`/recreate; existing volumes are
  compatible (pgvector is a postgres superset).
- Baseline search is currently scoped by repo_url only, not base_sha — acceptable while one
  repo is one sha per run; tighten to git_sha if multiple baselines per repo accumulate.
- Reflection memory and an MCP `semantic.search_reflections` tool remain unbuilt (future).
