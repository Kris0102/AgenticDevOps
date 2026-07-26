# src/context_engine/ — Semantic Code Memory

**Role in the MAS:** gives every agent a way to *find* relevant code by meaning
instead of grepping blind. It turns the target repository into an embedded,
searchable index (pgvector) that the agents query through the shared
`semantic_search` tool — "where are the HTTP routes defined?" returns the actual
functions, with snippets. (ADR 0010.)

## Files

| File | What it does |
|---|---|
| `chunker.py` | tree-sitter **AST chunking** — splits source into function/class-level chunks (whole-file fallback for things the parser doesn't know). AST boundaries beat fixed-size windows: a chunk is a semantically complete unit |
| `engine.py` | `ContextEngine` — the three operations: `ingest_baseline` (index the freshly cloned repo), `sync_workspace` (re-index files the Coder changed), `search` |

## How it fits the run lifecycle

1. **Bootstrap:** right after clone, `ingest_baseline` chunks + embeds the repo into
   the `baseline` namespace of `code_chunks`.
2. **During the run:** after each Coder diff, the coder node calls `sync_workspace`
   so search reflects the *current* sandbox state (the `workspace` namespace
   overlays the baseline) — agents never reason over stale code.
3. **Teardown:** workspace chunks are purged with the sandbox.

Embeddings are **local** (sentence-transformers all-MiniLM-L6-v2, 384-dim) — no
per-query API cost, no code leaves the machine. Storage/search is `SemanticStore`
over pgvector (`src/db/semanticMemory/`), so it's the same Postgres, no extra
infra — right-sized for a self-hosted single-user deployment.

## Why it matters in the MAS

- The **Planner's investigation** is dramatically cheaper: locate-by-meaning first,
  read files second, instead of walking the tree.
- All five agents share the one tool (`agents/semantic_search.py`), gateway-routed
  and audited like everything else.
- The two-namespace design (baseline vs. workspace) is what keeps retrieval honest
  *mid-run*, while the agents are actively rewriting the code they search.

## Interview sound bites

- "RAG over the codebase, but chunked on AST boundaries and kept in sync with the
  sandbox as the agents edit."
- "Local embeddings + pgvector in the DB we already run — no new infrastructure for
  semantic memory."
