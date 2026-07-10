"""pgvector-backed persistence + retrieval for the Context Engine.

Read/write layer over the `code_chunks` table. Owns no commit boundary of its own
beyond `db.session_scope()` (commit-on-success / rollback-on-error), matching the
rest of the data plane (CLAUDE.md). Embedding is delegated to db.embedder.Embedder.

Search is read-only — chunks are an index for navigation, never a source for
reconstructing the repo (the Sandbox is the source of truth, ARCHITECTURE §2.3).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import or_, select

from db.db import db
from db.semanticMemory.embedder import Embedder
from db.models import CodeChunk

# A run whose status is one of these is finished — its workspace index is dead weight.
# Anything else (mid-pipeline / stalled) may still be resumed via /continue, so its
# workspace chunks must survive.
_TERMINAL_RUN_STATUSES = ("DONE", "FAILED")

BASELINE = "baseline"
WORKSPACE = "workspace"

# Reciprocal Rank Fusion constant — the standard 60. Dampens the tail so a hit ranked
# highly by EITHER arm (vector or lexical) surfaces, without one arm dominating.
_RRF_K = 60
# Query words too generic to be useful lexical signal (they'd match half the repo).
_STOPWORDS = {"the", "a", "an", "of", "to", "in", "is", "are", "where", "what", "how",
              "find", "code", "for", "and", "or", "get", "set", "does", "do", "with"}


def _query_tokens(query: str) -> List[str]:
    """Identifier-ish tokens for the lexical arm — split on non-word chars, drop short
    words and stopwords, dedupe (order-preserving)."""
    seen: set = set()
    out: List[str] = []
    for tok in re.split(r"[^A-Za-z0-9_]+", query.lower()):
        if len(tok) >= 3 and tok not in _STOPWORDS and tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


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
        """Purge all workspace chunks for a run."""
        with db.session_scope() as session:
            return (
                session.query(CodeChunk)
                .filter(CodeChunk.namespace == WORKSPACE, CodeChunk.run_id == run_id)
                .delete(synchronize_session=False)
            )

    def prune_orphans(self) -> Dict[str, int]:
        """Housekeeping: drop chunks no live run/repo needs (context-engine #6). Set-difference
        deletes against the runs + repos tables — safe to call any time (we run it
        opportunistically when a run finalizes, so no cron infra):

          - workspace: the run is TERMINAL (DONE/FAILED) or its run row is gone. A stalled /
            in-flight run's workspace is kept (it may resume via /continue).
          - baseline : the repo_url is neither in the registry NOR referenced by any run row —
            i.e. fully orphaned. Conservative on purpose: a legacy/unregistered repo that
            still has runs keeps its baseline (old-sha retention is a separate concern).

        Returns {'workspace': n, 'baseline': m} — the counts deleted."""
        # Local import to avoid a module-level cycle (models ← store ← models).
        from db.models import RepoRecord, Run

        with db.session_scope() as session:
            live_run_ids = select(Run.run_id).where(
                Run.status.notin_(_TERMINAL_RUN_STATUSES))
            workspace_deleted = (
                session.query(CodeChunk)
                .filter(
                    CodeChunk.namespace == WORKSPACE,
                    CodeChunk.run_id.isnot(None),
                    CodeChunk.run_id.notin_(live_run_ids),
                )
                .delete(synchronize_session=False)
            )

            # NOT IN (registry ∪ any run's repo_url) == NOT IN registry AND NOT IN runs.
            baseline_deleted = (
                session.query(CodeChunk)
                .filter(
                    CodeChunk.namespace == BASELINE,
                    CodeChunk.repo_url.notin_(select(RepoRecord.repo_url)),
                    CodeChunk.repo_url.notin_(select(Run.repo_url)),
                )
                .delete(synchronize_session=False)
            )
            return {"workspace": int(workspace_deleted), "baseline": int(baseline_deleted)}

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
        """HYBRID retrieval within one namespace, scoped by repo (and run for workspace):
        pgvector cosine (semantic) fused with a lexical identifier match (symbol/filepath
        ILIKE) via Reciprocal Rank Fusion. The vector arm catches conceptual queries
        ('where is auth handled'); the lexical arm catches exact names ('parse_config',
        'RunConfig') a general-text embedding under-ranks. Returns detached plain dicts:
        filepath, symbol, chunk_type, chunk_text, score (fused RRF, higher = better)."""
        qvec = self.embedder.embed_query(query)
        distance = CodeChunk.embedding.cosine_distance(qvec)
        # Pull a deeper candidate pool per arm than top_k so fusion has something to work
        # with — the two rankings only overlap partially.
        fetch = max(top_k * 3, 20)
        tokens = _query_tokens(query)

        with db.session_scope() as session:
            def scoped(q):
                q = q.filter(CodeChunk.namespace == namespace, CodeChunk.repo_url == repo_url)
                if namespace == WORKSPACE and run_id is not None:
                    q = q.filter(CodeChunk.run_id == run_id)
                if chunk_types:
                    q = q.filter(CodeChunk.chunk_type.in_(list(chunk_types)))
                return q

            # Arm 1 — vector, ranked by cosine distance.
            vector_ranked: List[CodeChunk] = (
                scoped(session.query(CodeChunk)).order_by(distance).limit(fetch).all()
            )

            # Arm 2 — lexical, any query token in symbol or filepath, ranked in Python
            # (exact symbol > substring symbol > filepath).
            lexical_ranked: List[CodeChunk] = []
            if tokens:
                conds = []
                for t in tokens:
                    like = f"%{t}%"
                    conds.append(CodeChunk.symbol.ilike(like))
                    conds.append(CodeChunk.filepath.ilike(like))
                candidates = scoped(session.query(CodeChunk)).filter(or_(*conds)).limit(fetch * 2).all()
                lexical_ranked = sorted(
                    candidates, key=lambda c: self._lexical_score(c, tokens), reverse=True
                )[:fetch]

            # Reciprocal Rank Fusion over the two rankings, keyed by chunk_id.
            scores: Dict[str, float] = {}
            objs: Dict[str, CodeChunk] = {}
            for ranking in (vector_ranked, lexical_ranked):
                for rank, chunk in enumerate(ranking):
                    scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
                    objs.setdefault(chunk.chunk_id, chunk)

            top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
            return [self._to_hit(objs[cid], score) for cid, score in top]

    # ---- internals ----

    @staticmethod
    def _lexical_score(chunk: CodeChunk, tokens: List[str]) -> int:
        """Crude lexical relevance for the ILIKE candidate pool: an exact symbol match is
        the strongest signal, a substring in the symbol next, a filepath hit weakest."""
        sym = (chunk.symbol or "").lower()
        fp = chunk.filepath.lower()
        score = 0
        for t in tokens:
            if sym == t:
                score += 5
            elif t in sym:
                score += 2
            if t in fp:
                score += 1
        return score

    @staticmethod
    def _to_hit(chunk: CodeChunk, score: float) -> Dict[str, Any]:
        return {
            "filepath": chunk.filepath,
            "symbol": chunk.symbol,
            "chunk_type": chunk.chunk_type,
            "chunk_text": chunk.chunk_text,
            "score": round(float(score), 4),
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
