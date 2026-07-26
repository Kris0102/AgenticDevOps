# src/context_engine/ — Code Explanation

Two files, ~279 lines. Semantic code memory (ADR 0010): chunk → embed → store →
search. The storage/embedding halves live in `db/semanticMemory/` (see db.md);
this package is the chunking + orchestration.

---

## chunker.py (144 lines)

### Configuration tables (17–41)
- `LANGUAGE_BY_EXT`: 13 extensions → tree-sitter grammar names (py, js/jsx, ts,
  tsx, go, java, rb, rs, c/h, cpp/hpp).
- `_DEF_KEYWORDS = ("function", "class", "method", "struct", "interface", "impl")`
  — matched by **substring** against node types, deliberately tolerant across
  grammar versions (a `function_definition` in Python and a
  `function_declaration` in JS both contain "function").
- Skip rules: `MAX_FILE_BYTES = 200_000`, `SKIP_SUFFIXES` (lockfiles, minified
  js, images, archives, fonts), and a crude binary guard (`"\x00" in source`).

### `Chunk` dataclass (44–50)
`symbol, chunk_type ("function"|"class"|"file"), chunk_text, start_line, end_line`
— exactly what the store persists and what search returns.

### `chunk_file(filepath, source)` (71–85) — total by design
1. `should_skip` → `[]` (file simply not indexed).
2. Known language → try tree-sitter; success → AST chunks.
3. ANY failure (unsupported language, parse error, no top-level defs) → fall back
   to ONE whole-file chunk. Docstring: "the chunker is always total and never
   drops a file." An exception downgrade, not a crash — indexing must never kill
   a run.

### `_chunk_with_tree_sitter(lang, source)` (88–117)
- Parses `source` bytes, then walks **top-level named children** of the root:
  every def-node is emitted as a chunk (text sliced by byte offsets
  `start_byte:end_byte`, 1-based line numbers from `start_point`).
- One extra level: for class/impl nodes, `_body_defs` descends into the body
  node (`block`, `class_body`, `declaration_list`, `impl_item`) and emits
  **methods** as their own chunks too — so a search for "validate email" can hit
  the method, not the whole class.
- `_symbol_name` uses tree-sitter's `child_by_field_name("name")` — grammar-
  agnostic way to get the identifier.

## engine.py (137 lines) — `ContextEngine`

Composes chunker + `Embedder` + `SemanticStore`. Constructor note: `sandbox_tools`
is optional — only *ingestion* needs to read the repo; search-only callers (the
gateway wiring for agents) omit it.

Docstring states a scoping decision: **synchronous by design** — no filesystem
watcher; the two callers (BootStrapper, coder node) drive syncs explicitly.
Simplest viable; a watcher is a future optimization.

### `ingest_baseline(...)` (37–58)
- **Idempotent**: `store.has_baseline(repo_url, base_sha)` → skip (a re-run or a
  second run on the same repo@sha reuses the index — embedding a large repo isn't
  free).
- File list = `git ls-files` in the sandbox (`_list_tracked_files`) — tracked
  files only; node_modules etc. never get indexed because git doesn't track them.
- Each file → `_index_file(namespace=BASELINE, git_sha=base_sha, run_id=None)`.
  Baseline chunks are keyed by repo+sha, shared across runs.

### `sync_workspace(...)` (60–73)
Called by the coder node after each diff with the **changed files only** — no
full re-index. Chunks go to `namespace=WORKSPACE` keyed by `run_id` (run-local).
Inside `_index_file` (121–136): if the file read fails (deleted/unreadable) →
`upsert_file(rel, [])` — an **empty upsert deletes stale chunks**, so a deleted
file disappears from search instead of haunting it.

### `search(query, ...)` (77–105) — the overlay merge
1. If `run_id` given → search WORKSPACE (freshest, run-local) first.
2. Always search BASELINE.
3. Merge: sort all hits by score desc, dedupe by `(filepath, symbol)` — because
   workspace hits were added first at equal-or-any score order? No: dedupe keeps
   the FIRST occurrence in the sorted order, and a workspace re-index of a changed
   file gives it its own (fresher) score; the invariant that matters is
   **one entry per symbol**, with workspace content available the moment a file
   changed. Truncate to `top_k`.
4. Structured log line records query, hit count, top score + top filepath — this
   is what `LOG_LEVEL_SEMANTIC=DEBUG` lights up.

Workspace chunks are purged with the sandbox
(`SemanticStore.delete_workspace(run_id)` in `sandbox_destroy`) — run-local memory
never outlives the run.

---

## Relationships

```
BootStrapper ──► ingest_baseline (after clone)        [namespace=baseline, key=repo+sha]
coder node ────► sync_workspace (after each diff)     [namespace=workspace, key=run_id]
sandbox_destroy ► SemanticStore.delete_workspace      (purge run-local chunks)
agents ────────► ToolGateway(SEMANTIC_SEARCH) ──► ContextEngine.search
                                                    │
                                    chunker.chunk_file ─► SemanticStore.upsert_file
                                                    │           (db/semanticMemory/)
                                              Embedder (MiniLM, local, 384-dim)
```

Exam sentences: "Chunks follow AST boundaries because a function is the unit an
agent asks about — fixed-size windows cut definitions in half. The two-namespace
overlay is what keeps retrieval honest mid-run: baseline is the repo at clone
time, workspace overrides it file-by-file as the coder edits."
