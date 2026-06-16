"""Local sentence-transformers embedder for the Context Engine.

Wraps all-MiniLM-L6-v2 (384-dim) — in-process, offline, free. The model is loaded
lazily on first use so importing this module stays cheap (and the orchestrator can
boot without paying the model load when semantic memory isn't touched).
"""
from __future__ import annotations

import os
from typing import List, Optional

from db.models import EMBEDDING_DIM
from observability import Category, get_logger

DEFAULT_MODEL = "all-MiniLM-L6-v2"
_log = get_logger(Category.SEMANTIC)


class Embedder:
    """Lazy singleton around a SentenceTransformer. Use `Embedder.instance()`."""

    _instance: Optional["Embedder"] = None

    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or os.getenv("EMBEDDING_MODEL", DEFAULT_MODEL)
        self.dim = EMBEDDING_DIM
        self._model = None  # loaded on first embed call

    @classmethod
    def instance(cls) -> "Embedder":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors = self._load().encode(texts, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]

    def _load(self):
        if self._model is None:
            # Imported here, not at module top, so the heavy dependency only loads
            # when embeddings are actually requested.
            from sentence_transformers import SentenceTransformer

            _log.info("embedder.loading_model", model=self.model_name, dim=self.dim)
            self._model = SentenceTransformer(self.model_name)
        return self._model
