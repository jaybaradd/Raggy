"""
core/embeddings.py — local embedding backend using sentence-transformers.

Always uses local sentence-transformers (default: all-MiniLM-L6-v2, 384 dimensions).
No external API calls required for embeddings.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from config import settings

logger = logging.getLogger(__name__)


class EmbeddingBackend(ABC):
    """Common interface for embedding backends."""

    @abstractmethod
    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts (used at ingestion time)."""
        ...

    @abstractmethod
    def encode_query(self, query: str) -> list[float]:
        """Embed a single query string (used at retrieval time)."""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Return vector dimension."""
        ...


class LocalEmbeddingBackend(EmbeddingBackend):
    """
    Uses sentence-transformers to embed text entirely on-device.

    Model is loaded once at construction time. Supports Matryoshka dimension truncation.
    """

    def __init__(self, model_name: str, truncate_dim: int | None = None) -> None:
        import torch
        from sentence_transformers import SentenceTransformer

        logger.info("Loading local embedding model '%s' …", model_name)
        device = torch.device("cpu")
        self._model = SentenceTransformer(
            model_name,
            device=device,
            trust_remote_code=True,
        )
        
        dim_to_use = truncate_dim or settings.local_embed_dim
        if dim_to_use:
            self._model.truncate_dim = dim_to_use
            self._dim = dim_to_use
        else:
            self._dim = self._model.get_sentence_embedding_dimension()
            
        logger.info("Local embedding model ready (dimension=%d).", self._dim)

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, convert_to_numpy=True)
        return [v.tolist() for v in vectors]

    def encode_query(self, query: str) -> list[float]:
        return self.encode([query])[0]

    @property
    def dimension(self) -> int:
        return self._dim


# Module-level singleton — constructed once at import time.
embedder: EmbeddingBackend = LocalEmbeddingBackend(
    settings.local_embed_model,
    truncate_dim=settings.local_embed_dim,
)
