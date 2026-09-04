"""
core/storage/qdrant_store.py — thin wrapper around the Qdrant client.

Responsibilities
----------------
- Create the collection on first use (idempotent).
- Upsert vectors + payloads (ParsedChunk metadata).
- Search: embed a query and return the top-k chunks.

Phase 0 uses dense vectors only (no BM25 sparse, no reranking).
Phase 1 will add sparse vectors and multi-collection routing.

The client operates in two modes:
  - In-memory  (QDRANT_URL=":memory:")  — no server required, data lost on restart.
  - Server     (QDRANT_URL="http://…") — persistent, suitable for production.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config import settings
from core.ingestion.parser import ParsedChunk

if TYPE_CHECKING:
    pass

from core.embeddings import embedder

logger = logging.getLogger(__name__)


class QdrantStore:
    """
    Manages a single Qdrant collection for Phase 0 text chunks.

    Parameters
    ----------
    collection_name : Name of the Qdrant collection.  Defaults to the value in
                      settings so callers rarely need to override it.
    """

    def __init__(self, collection_name: str | None = None) -> None:
        self._collection = collection_name or settings.qdrant_collection
        self._dim = embedder.dimension

        if settings.qdrant_url == ":memory:":
            self._client = QdrantClient(":memory:")
        else:
            self._client = QdrantClient(url=settings.qdrant_url)

        self._ensure_collection()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _ensure_collection(self) -> None:
        """Create the collection if it does not already exist (idempotent)."""
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=qmodels.VectorParams(
                    size=self._dim,
                    distance=qmodels.Distance.COSINE,
                ),
            )
            logger.info("Created Qdrant collection '%s'", self._collection)

    # ── Public API ────────────────────────────────────────────────────────────

    def upsert(
        self,
        chunks: list[ParsedChunk],
        vectors: list[list[float]],
    ) -> None:
        """
        Upsert *chunks* into the collection using the provided *vectors*.

        Parameters
        ----------
        chunks  : List of ParsedChunks — their metadata becomes the Qdrant payload.
        vectors : Parallel list of embedding vectors (same length as chunks).
        """
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks ({len(chunks)}) and vectors ({len(vectors)}) must be same length"
            )

        points = [
            qmodels.PointStruct(
                id=_chunk_id_to_int(chunk.chunk_id),
                vector=vector,
                payload={
                    "chunk_id": chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "modality": chunk.modality,
                    "content": chunk.content,
                    "page": chunk.source_locator.page,
                    "raw_file_uri": chunk.raw_file_uri,
                    "parser_backend": chunk.parser_backend,
                    "embedding_model": chunk.embedding_model,
                    "created_at": chunk.created_at.isoformat(),
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]

        self._client.upsert(collection_name=self._collection, points=points)
        logger.info("Upserted %d chunks into '%s'", len(points), self._collection)

    def search(
        self,
        query_vector: list[float],
        top_k: int | None = None,
        doc_id_filter: str | None = None,
    ) -> list[dict]:
        """
        Dense vector search.

        Parameters
        ----------
        query_vector    : Embedding of the user's query.
        top_k           : Number of results to return.  Defaults to settings value.
        doc_id_filter   : If provided, restrict results to chunks from this document.

        Returns
        -------
        List of payload dicts sorted by descending score, each containing the
        chunk metadata plus a "score" key.
        """
        k = top_k or settings.retrieval_top_k

        query_filter: qmodels.Filter | None = None
        if doc_id_filter:
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="doc_id",
                        match=qmodels.MatchValue(value=doc_id_filter),
                    )
                ]
            )

        hits = self._client.search(
            collection_name=self._collection,
            query_vector=query_vector,
            limit=k,
            query_filter=query_filter,
            with_payload=True,
        )

        results = []
        for hit in hits:
            payload = dict(hit.payload or {})
            payload["score"] = hit.score
            results.append(payload)

        return results

    def collection_info(self) -> dict:
        """Return basic stats about the collection (useful for health checks)."""
        info = self._client.get_collection(self._collection)
        return {
            "collection": self._collection,
            "vectors_count": info.vectors_count,
            "status": str(info.status),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _chunk_id_to_int(chunk_id: str) -> int:
    """
    Qdrant point IDs must be unsigned 64-bit integers or UUIDs.
    We use the UUID directly as a string here — Qdrant accepts UUID strings.

    Actually Qdrant accepts both integer and UUID string IDs.  We pass the
    UUID string directly, but the PointStruct ``id`` field must be int or str.
    This function is kept for reference but not called; we pass the UUID string.
    """
    return int(chunk_id.replace("-", ""), 16) % (2**63)


# Module-level singleton
qdrant_store = QdrantStore()
