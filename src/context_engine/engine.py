"""Context Engine — the single entry point for semantic code memory.

Composes chunker + embedder + SemanticStore over the Sandbox (the source of truth):
  - ingest_baseline: index the repo @ base_sha once at run start (namespace=baseline)
  - sync_workspace : re-index only changed files during a run (namespace=workspace)
  - search         : retrieve relevant chunks, workspace overlaid on baseline

Synchronous by design — there is no filesystem watcher (ADR: simplest viable; a
watcher is a future optimization). Callers (BootStrapper, coder node) drive sync.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from context_engine.chunker import chunk_file
from db.semanticMemory.embedder import Embedder
from db.semanticMemory.semantic_store import BASELINE, WORKSPACE, SemanticStore
from sandbox.sandbox_tools import DockerSandboxTools, SandboxError


class ContextEngine:
    def __init__(
        self,
        sandbox_tools: Optional[DockerSandboxTools] = None,
        store: Optional[SemanticStore] = None,
    ):
        # sandbox_tools is only needed for ingestion (reading the repo); search-only
        # callers (e.g. the planner gateway) can omit it.
        self.sandbox_tools = sandbox_tools
        self.store = store or SemanticStore(Embedder.instance())

    # ---- ingestion ----

    def ingest_baseline(self, *, sandbox_id: str, repo_url: str, base_sha: Optional[str],
                        workspace_path: str) -> int:
        """Chunk + embed every tracked file in the cloned repo into the baseline
        namespace. Idempotent: a no-op if baseline already exists for (repo_url,
        base_sha). Returns total chunks written."""
        if self.sandbox_tools is None:
            raise RuntimeError("ContextEngine.ingest_baseline requires sandbox_tools")
        if self.store.has_baseline(repo_url, base_sha):
            print(f"[ContextEngine] Baseline already indexed for {repo_url}@{base_sha} — skipping.")
            return 0

        files = self._list_tracked_files(sandbox_id, workspace_path)
        print(f"[ContextEngine] Ingesting baseline: {len(files)} tracked files.")
        total = 0
        for rel in files:
            total += self._index_file(
                sandbox_id=sandbox_id, workspace_path=workspace_path, rel=rel,
                namespace=BASELINE, repo_url=repo_url, git_sha=base_sha, run_id=None,
            )
        print(f"[ContextEngine] Baseline ingest complete: {total} chunks.")
        return total

    def sync_workspace(self, *, sandbox_id: str, run_id: str, repo_url: str,
                       workspace_path: str, changed_files: Sequence[str]) -> int:
        """Re-index only the named (repo-relative) files into the workspace namespace
        for this run. Deleted/unreadable files are removed from the index."""
        if self.sandbox_tools is None:
            raise RuntimeError("ContextEngine.sync_workspace requires sandbox_tools")
        total = 0
        for rel in changed_files:
            total += self._index_file(
                sandbox_id=sandbox_id, workspace_path=workspace_path, rel=rel,
                namespace=WORKSPACE, repo_url=repo_url, git_sha=None, run_id=run_id,
            )
        return total

    # ---- retrieval ----

    def search(self, query: str, *, repo_url: str, run_id: Optional[str] = None,
               top_k: int = 8, chunk_types: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """Workspace (run-local, freshest) overlaid on baseline. Dedupe by
        (filepath, symbol) preferring the workspace hit; return top_k by score."""
        hits: List[Dict[str, Any]] = []
        if run_id is not None:
            hits += self.store.search(query, namespace=WORKSPACE, repo_url=repo_url,
                                      run_id=run_id, top_k=top_k, chunk_types=chunk_types)
        hits += self.store.search(query, namespace=BASELINE, repo_url=repo_url,
                                  top_k=top_k, chunk_types=chunk_types)

        seen = set()
        merged: List[Dict[str, Any]] = []
        for hit in sorted(hits, key=lambda h: h["score"], reverse=True):
            key = (hit["filepath"], hit["symbol"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
        return merged[:top_k]

    # ---- internals ----

    def _list_tracked_files(self, sandbox_id: str, workspace_path: str) -> List[str]:
        if self.sandbox_tools is None:
            raise RuntimeError("ContextEngine._list_tracked_files requires sandbox_tools")
        
        res = self.sandbox_tools.sandbox_exec(
            sandbox_id=sandbox_id, cmd=["git", "ls-files"], timeout_s=60, cwd=workspace_path,
        )
        if res.exit_code != 0:
            print(f"[ContextEngine] git ls-files failed: {res.stderr or res.stdout}")
            return []
        return [line for line in res.stdout.splitlines() if line.strip()]

    def _index_file(self, *, sandbox_id: str, workspace_path: str, rel: str,
                    namespace: str, repo_url: str, git_sha: Optional[str],
                    run_id: Optional[str]) -> int:
        abs_path = f"{workspace_path.rstrip('/')}/{rel}"
        if self.sandbox_tools is None:
            raise RuntimeError("ContextEngine._index_file requires sandbox_tools")
        try:
            source = self.sandbox_tools.fs_read_file(sandbox_id=sandbox_id, path=abs_path)
        except SandboxError:
            # Deleted or unreadable → ensure no stale chunks linger for this file.
            self.store.upsert_file(rel, [], namespace=namespace, repo_url=repo_url,
                                   git_sha=git_sha, run_id=run_id)
            return 0
        chunks = chunk_file(rel, source)
        return self.store.upsert_file(rel, chunks, namespace=namespace, repo_url=repo_url,
                                      git_sha=git_sha, run_id=run_id)
