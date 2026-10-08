"""
core/retrieval/reranker.py — cross-encoder reranking step (Phase 1).

Role in the pipeline
--------------------
Hybrid retrieval (BM25 + dense RRF) fetches a wide pool of ~20 candidates.
The reranker is a cross-encoder that scores each (query, chunk) pair jointly,
producing a much more accurate relevance estimate than the bi-encoder scores
from the first-stage retrieval.  We keep the top-N reranked chunks and pass
only those to the LLM.

Why a cross-encoder here?
- Bi-encoders (like the embedding model) encode query and chunk independently;
  cross-encoders see both together, which is more accurate but slower.
- At ~20 candidates the latency is acceptable on CPU (~50–200 ms).

Model: cross-encoder/ms-marco-MiniLM-L-6-v2
- 22M parameters, CPU-friendly, strong on passage-retrieval tasks.
- Returns a raw logit score (higher = more relevant); we do NOT softmax it
  because only the relative order matters.
"""

from __future__ import annotations

import logging

from config import settings

logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """
    Wraps a sentence-transformers CrossEncoder model.

    The model is loaded once at construction time (lazy import so the class
    can be imported cheaply even before the model weights are available).
    """

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import CrossEncoder

        logger.info("Loading cross-encoder reranker '%s' …", model_name)
        self._model = CrossEncoder(model_name, max_length=512)
        logger.info("Reranker ready.")

    def rerank(
        self,
        query: str,
        chunks: list[dict],
        top_n: int | None = None,
    ) -> list[dict]:
        """
        Rerank *chunks* by relevance to *query* and return the top-N.

        Parameters
        ----------
        query   : The user's query string.
        chunks  : List of chunk payload dicts (as returned by QdrantStore.search).
                  Each dict must have a ``content`` key.
        top_n   : How many to keep.  Defaults to ``settings.reranker_top_n``.

        Returns
        -------
        A new list of chunk dicts, sorted descending by reranker score,
        each augmented with a ``rerank_score`` key.
        """
        n = top_n or settings.reranker_top_n

        if not chunks:
            return []

        # Build (query, passage) pairs for the cross-encoder
        pairs = [(query, chunk["content"]) for chunk in chunks]
        scores = self._model.predict(pairs)

        # Attach rerank score and sort
        scored = [
            {**chunk, "rerank_score": float(score)}
            for chunk, score in zip(chunks, scores)
        ]
        scored.sort(key=lambda c: c["rerank_score"], reverse=True)

        top = scored[:n]
        logger.info(
            "Reranker: %d candidates → top %d | scores %.3f … %.3f",
            len(chunks),
            len(top),
            top[0]["rerank_score"],
            top[-1]["rerank_score"],
        )
        return top


# Module-level singleton — loaded once at import time.
reranker = CrossEncoderReranker(settings.reranker_model)
