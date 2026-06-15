"""pgvector-backed persistence + retrieval for the Context Engine.

Read/write layer over the `code_chunks` table. Owns no commit boundary of its own
beyond `db.session_scope()` (commit-on-success / rollback-on-error), matching the
rest of the data plane (CLAUDE.md). Embedding is delegated to db.embedder.Embedder.

Search is read-only — chunks are an index for navigation, never a source for
reconstructing the repo (the Sandbox is the source of truth, ARCHITECTURE §2.3).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from db.db import db
from db.semanticMemory.embedder import Embedder
from db.models import CodeChunk

BASELINE = "baseline"
WORKSPACE = "workspace"


class SemanticStore:
    def __init__(self, embedder: Optional[Embedder] = None):
        self.embedder = embedder or Embedder.instance()

    # ---- writes ----

    def upsert_file(
        self,
        filepath: str,
        chunks: Sequence[Any],
        *,
        namespace: str,
        repo_url: str,
        git_sha: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> int:
        """Replace all chunks for one file in one namespace (delete-then-insert), so
        re-ingesting an edited file is idempotent. `chunks` are chunker.Chunk objects.
        Returns the number of rows written."""
        if not chunks:
            self._delete_file(namespace=namespace, repo_url=repo_url, run_id=run_id, filepath=filepath)
            return 0

        embeddings = self.embedder.embed_texts([c.chunk_text for c in chunks])
        now = datetime.now(timezone.utc)
        with db.session_scope() as session:
            self._delete_file(
                namespace=namespace, repo_url=repo_url, run_id=run_id,
                filepath=filepath, session=session,
            )
            for chunk, embedding in zip(chunks, embeddings):
                session.add(CodeChunk(
                    chunk_id=self._chunk_id(namespace, run_id, filepath, chunk),
                    namespace=namespace,
                    run_id=run_id,
                    repo_url=repo_url,
                    git_sha=git_sha,
                    filepath=filepath,
                    symbol=chunk.symbol,
                    chunk_type=chunk.chunk_type,
                    chunk_text=chunk.chunk_text,
                    embedding=embedding,
                    metadata_json={
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                    },
                    created_at=now,
                ))
        return len(chunks)

    def delete_workspace(self, run_id: str) -> int:
        """Purge all workspace chunks for a run (called on sandbox teardown)."""
        with db.session_scope() as session:
            return (
                session.query(CodeChunk)
                .filter(CodeChunk.namespace == WORKSPACE, CodeChunk.run_id == run_id)
                .delete(synchronize_session=False)
            )

    # ---- reads ----

    def has_baseline(self, repo_url: str, git_sha: Optional[str]) -> bool:
        """True if baseline chunks already exist for (repo_url, git_sha) — drives
        idempotent baseline ingestion across resumes / repeat runs."""
        with db.session_scope() as session:
            q = session.query(CodeChunk.chunk_id).filter(
                CodeChunk.namespace == BASELINE,
                CodeChunk.repo_url == repo_url,
            )
            q = q.filter(CodeChunk.git_sha == git_sha) if git_sha else q.filter(CodeChunk.git_sha.is_(None))
            return session.query(q.exists()).scalar() or False

    def search(
        self,
        query: str,
        *,
        namespace: str,
        repo_url: str,
        run_id: Optional[str] = None,
        top_k: int = 8,
        chunk_types: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Cosine-similarity search within one namespace, scoped by repo (and run for
        workspace). Returns detached plain dicts: filepath, symbol, chunk_type,
        chunk_text, score (1 - cosine_distance, higher = closer)."""
        qvec = self.embedder.embed_query(query)
        distance = CodeChunk.embedding.cosine_distance(qvec)
        with db.session_scope() as session:
            stmt = (
                session.query(CodeChunk, distance.label("distance"))
                .filter(CodeChunk.namespace == namespace, CodeChunk.repo_url == repo_url)
            )
            if namespace == WORKSPACE and run_id is not None:
                stmt = stmt.filter(CodeChunk.run_id == run_id)
            if chunk_types:
                stmt = stmt.filter(CodeChunk.chunk_type.in_(list(chunk_types)))
            rows = stmt.order_by(distance).limit(top_k).all()
            return [self._to_hit(chunk, dist) for chunk, dist in rows]

    # ---- internals ----

    @staticmethod
    def _to_hit(chunk: CodeChunk, distance: float) -> Dict[str, Any]:
        return {
            "filepath": chunk.filepath,
            "symbol": chunk.symbol,
            "chunk_type": chunk.chunk_type,
            "chunk_text": chunk.chunk_text,
            "score": round(1.0 - float(distance), 4),
        }

    @staticmethod
    def _chunk_id(namespace: str, run_id: Optional[str], filepath: str, chunk: Any) -> str:
        key = f"{namespace}|{run_id or '-'}|{filepath}|{chunk.symbol or '-'}|{chunk.start_line}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    def _delete_file(self, *, namespace, repo_url, run_id, filepath, session=None) -> None:
        def _run(s):
            q = s.query(CodeChunk).filter(
                CodeChunk.namespace == namespace,
                CodeChunk.repo_url == repo_url,
                CodeChunk.filepath == filepath,
            )
            q = q.filter(CodeChunk.run_id == run_id) if run_id is not None else q.filter(CodeChunk.run_id.is_(None))
            q.delete(synchronize_session=False)

        if session is not None:
            _run(session)
        else:
            with db.session_scope() as s:
                _run(s)
