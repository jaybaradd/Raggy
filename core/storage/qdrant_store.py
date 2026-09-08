"""
core/storage/qdrant_store.py — thin wrapper around the Qdrant client.

Phase 1 upgrade: Hybrid retrieval (dense + BM25 sparse, fused via RRF).

Responsibilities
----------------
- Create the collection on first use (idempotent) with both dense and sparse
  vector configs.
- Upsert vectors + payloads (ParsedChunk metadata) — dense embedding from the
  embedding backend, BM25 sparse vector from fastembed.
- Search: hybrid prefetch (dense sub-query + sparse BM25 sub-query) fused via
  Qdrant's native Reciprocal Rank Fusion, returning the top-k candidates for
  the reranker.

The client operates in two modes:
  - In-memory  (QDRANT_URL=\":memory:\")  — no server required, data lost on restart.
  - Server     (QDRANT_URL=\"http://…\") — persistent, suitable for production.
  - Local disk (QDRANT_URL=\"./qdrant_data\") — persistent without a server.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config import settings
from core.ingestion.parser import ParsedChunk
from core.memory.models import MemoryRecord
from core.memory.projection import memory_text

if TYPE_CHECKING:
    pass

from core.embeddings import embedder

logger = logging.getLogger(__name__)

# Name used for the sparse (BM25) vector field inside Qdrant
_SPARSE_VECTOR_NAME = "bm25"


def _build_bm25_encoder():
    """
    Lazily construct the fastembed BM25 encoder.

    fastembed's SparseTextEmbedding wraps a fast Rust tokeniser;
    we use the 'Qdrant/bm25' model which is a vocabulary-free,
    position-independent BM25 implementation suited for keyword retrieval.
    """
    from fastembed import SparseTextEmbedding

    logger.info("Loading BM25 sparse encoder (fastembed) …")
    enc = SparseTextEmbedding(model_name="Qdrant/bm25")
    logger.info("BM25 encoder ready.")
    return enc


class QdrantStore:
    """
    Manages a single Qdrant collection for text chunks.

    Phase 1: collection now stores both dense and sparse (BM25) vectors,
    and search uses Qdrant's hybrid prefetch + RRF fusion.

    Parameters
    ----------
    collection_name : Name of the Qdrant collection.  Defaults to the value in
                      settings so callers rarely need to override it.
    """

    def __init__(
        self,
        collection_name: str | None = None,
        *,
        client: QdrantClient | None = None,
        bm25_encoder=None,
    ) -> None:
        self._collection = collection_name or settings.qdrant_collection
        self._dim = embedder.dimension
        self._bm25 = bm25_encoder or _build_bm25_encoder()

        if client is not None:
            self._client = client
        elif settings.qdrant_url == ":memory:":
            self._client = QdrantClient(":memory:")
        elif settings.qdrant_url.startswith("http://") or settings.qdrant_url.startswith("https://"):
            self._client = QdrantClient(url=settings.qdrant_url)
        else:
            self._client = QdrantClient(path=settings.qdrant_url)

        self._ensure_collection()

    # ── Private helpers ───────────────────────────────────────────────────────

    def _ensure_collection(self) -> None:
        """Create the collection with dense + sparse configs if it does not exist."""
        existing = {c.name for c in self._client.get_collections().collections}
        if self._collection not in existing:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    "dense": qmodels.VectorParams(
                        size=self._dim,
                        distance=qmodels.Distance.COSINE,
                    ),
                },
                sparse_vectors_config={
                    _SPARSE_VECTOR_NAME: qmodels.SparseVectorParams(
                        index=qmodels.SparseIndexParams(on_disk=False),
                    ),
                },
            )
            logger.info(
                "Created Qdrant collection '%s' (dense=%d-d + sparse BM25)",
                self._collection,
                self._dim,
            )

    def _encode_sparse(self, texts: list[str]) -> list[qmodels.SparseVector]:
        """Return BM25 SparseVector objects for a batch of texts."""
        results = list(self._bm25.embed(texts))
        sparse_vectors = []
        for r in results:
            indices = r.indices.tolist()
            values = r.values.tolist()
            sparse_vectors.append(
                qmodels.SparseVector(indices=indices, values=values)
            )
        return sparse_vectors

    # ── Public API ────────────────────────────────────────────────────────────

    def _collection_store(self, modality: str) -> "QdrantStore":
        """Return a lazily-created collection sharing this store's client."""
        collection = {
            "text": settings.qdrant_collection,
            "table": "kb_table_chunks",
            "image": "kb_image_chunks",
            "video_segment": "kb_video_segments",
            "audio_segment": "kb_video_segments",
        }.get(modality, settings.qdrant_collection)
        if collection == self._collection:
            return self
        stores = getattr(self, "_modality_stores", {})
        if collection not in stores:
            stores[collection] = QdrantStore(
                collection,
                client=self._client,
                bm25_encoder=self._bm25,
            )
            self._modality_stores = stores
        return stores[collection]

    def upsert(
        self,
        chunks: list[ParsedChunk],
        vectors: list[list[float]],
    ) -> None:
        """
        Upsert *chunks* with both dense and sparse (BM25) vectors.

        Parameters
        ----------
        chunks  : List of ParsedChunks — their metadata becomes the Qdrant payload.
        vectors : Parallel list of dense embedding vectors (same length as chunks).
        """
        if len(chunks) != len(vectors):
            raise ValueError(
                f"chunks ({len(chunks)}) and vectors ({len(vectors)}) must be same length"
            )

        # Route each modality to its own collection while retaining the
        # existing text collection name for backwards compatibility.
        groups: dict[str, tuple[list[ParsedChunk], list[list[float]]]] = {}
        for chunk, vector in zip(chunks, vectors):
            group = groups.setdefault(chunk.modality, ([], []))
            group[0].append(chunk)
            group[1].append(vector)
        if len(groups) > 1 or (groups and next(iter(groups)) != "text"):
            for modality, (group_chunks, group_vectors) in groups.items():
                self._collection_store(modality)._upsert_single(group_chunks, group_vectors)
            return

        self._upsert_single(chunks, vectors)

    def _upsert_single(
        self,
        chunks: list[ParsedChunk],
        vectors: list[list[float]],
    ) -> None:
        """Upsert chunks known to belong to this collection."""

        # Compute BM25 sparse vectors for all chunk texts in one batch
        texts = [c.content for c in chunks]
        sparse_vecs = self._encode_sparse(texts)

        points = [
            qmodels.PointStruct(
                id=_chunk_id_to_int(chunk.chunk_id),
                vector={
                    "dense": dense_vector,
                    _SPARSE_VECTOR_NAME: sparse_vec,
                },
                payload={
                    "chunk_id": chunk.chunk_id,
                    "evidence_id": chunk.evidence_id or chunk.chunk_id,
                    "doc_id": chunk.doc_id,
                    "modality": chunk.modality,
                    "representation": chunk.representation,
                    "content": chunk.content,
                    "context_prefix": chunk.context_prefix,
                    "source_name": chunk.source_name,
                    "page": chunk.source_locator.page,
                    "time_range": chunk.source_locator.time_range,
                    "cell_range": chunk.source_locator.cell_range,
                    "bbox": chunk.source_locator.bbox,
                    "raw_file_uri": chunk.raw_file_uri,
                    "parser_backend": chunk.parser_backend,
                    "embedding_model": chunk.embedding_model,
                    "created_at": chunk.created_at.isoformat(),
                },
            )
            for chunk, dense_vector, sparse_vec in zip(chunks, vectors, sparse_vecs)
        ]

        self._client.upsert(collection_name=self._collection, points=points)
        logger.info("Upserted %d chunks into '%s'", len(points), self._collection)

    def search_all(
        self,
        query_vector: list[float],
        query_text: str,
        top_k: int | None = None,
        doc_id_filter: str | None = None,
    ) -> list[dict]:
        """Search all modality collections and apply lightweight modality weights."""
        k = top_k or settings.hybrid_candidates
        weights = {"text": 1.0, "table": 1.0, "image": 0.8, "video_segment": 0.7, "audio_segment": 0.7}
        results: list[dict] = []
        for modality in ("text", "table", "image", "video_segment"):
            store = self._collection_store(modality)
            for result in store.search(query_vector, query_text, k, doc_id_filter):
                result["score"] = float(result.get("score", 0.0)) * weights.get(modality, 1.0)
                result["collection"] = store._collection
                results.append(result)
        results.sort(key=lambda item: item.get("score", 0.0), reverse=True)
        return results[:k]

    def _memory_store(self) -> "QdrantStore":
        store = getattr(self, "_memory_collection_store", None)
        if store is None:
            store = QdrantStore(settings.memory_collection, client=self._client, bm25_encoder=self._bm25)
            self._memory_collection_store = store
        return store

    def upsert_memory(self, record: MemoryRecord) -> None:
        """Upsert an active confirmed memory projection; remove ineligible records."""
        store = self._memory_store()
        if record.status != "active" or not record.user_confirmed:
            store.delete_memory(record.memory_id)
            return
        text = memory_text(record)
        vector = embedder.encode([text])[0]
        sparse = store._encode_sparse([text])[0]
        store._client.upsert(collection_name=store._collection, points=[qmodels.PointStruct(
            id=_chunk_id_to_int(record.memory_id),
            vector={"dense": vector, _SPARSE_VECTOR_NAME: sparse},
            payload={
                "memory_id": record.memory_id, "memory_text": text, "kind": record.kind,
                "owner_id": record.owner_id, "scope": record.scope,
                "session_id": record.session_id, "project_scope": record.project_scope,
                "status": record.status, "confidence": record.confidence,
                "user_confirmed": record.user_confirmed, "source_turn_id": record.source_turn_id,
                "evidence_refs": record.evidence_refs, "valid_from": record.valid_from.isoformat(),
                "valid_to": record.valid_to.isoformat() if record.valid_to else None,
            },
        )])

    def delete_memory(self, memory_id: str) -> None:
        self._client.delete(collection_name=self._collection, points_selector=qmodels.PointIdsList(points=[_chunk_id_to_int(memory_id)]))

    def search_memories(self, *, query_vector: list[float], query_text: str, owner_id: str,
                        session_id: str, project_scope: str | None = None,
                        top_k: int = 5) -> list[dict]:
        """Search only active memories in scopes applicable to the current session."""
        store = self._memory_store()
        scope_filters = [("session", session_id), ("user", owner_id)]
        if project_scope:
            scope_filters.append(("project", project_scope))
        hits: list[dict] = []
        for scope, scope_value in scope_filters:
            must = [
                qmodels.FieldCondition(key="owner_id", match=qmodels.MatchValue(value=owner_id)),
                qmodels.FieldCondition(key="status", match=qmodels.MatchValue(value="active")),
                qmodels.FieldCondition(key="user_confirmed", match=qmodels.MatchValue(value=True)),
                qmodels.FieldCondition(key="scope", match=qmodels.MatchValue(value=scope)),
            ]
            if scope == "session":
                must.append(qmodels.FieldCondition(key="session_id", match=qmodels.MatchValue(value=scope_value)))
            elif scope == "project":
                must.append(qmodels.FieldCondition(key="project_scope", match=qmodels.MatchValue(value=scope_value)))
            query_filter = qmodels.Filter(must=must)
            hits.extend(store.search(query_vector, query_text, top_k * 2, query_filter=query_filter))
        unique = {hit["memory_id"]: hit for hit in hits}
        now = datetime.now(timezone.utc)
        eligible = []
        for hit in unique.values():
            try:
                valid_from = datetime.fromisoformat(hit["valid_from"])
                valid_to = datetime.fromisoformat(hit["valid_to"]) if hit.get("valid_to") else None
                if valid_from > now or (valid_to is not None and valid_to <= now):
                    continue
            except (KeyError, ValueError):
                logger.warning("Skipping memory with invalid validity metadata: %s", hit.get("memory_id"))
                continue
            eligible.append(hit)
        return sorted(eligible, key=lambda hit: hit.get("score", 0.0), reverse=True)[:top_k]

    def search(
        self,
        query_vector: list[float],
        query_text: str,
        top_k: int | None = None,
        doc_id_filter: str | None = None,
        query_filter: qmodels.Filter | None = None,
    ) -> list[dict]:
        """
        Hybrid search: dense + BM25 sparse, fused via Qdrant native RRF.

        Parameters
        ----------
        query_vector    : Dense embedding of the user's query.
        query_text      : Raw query string, used to build the BM25 sparse query.
        top_k           : Number of fused results to return (pre-rerank pool size).
                          Defaults to ``settings.hybrid_candidates``.
        doc_id_filter   : If provided, restrict results to chunks from this document.

        Returns
        -------
        List of payload dicts sorted by descending RRF score, each with a
        ``score`` key (the fused RRF score).
        """
        k = top_k or settings.hybrid_candidates

        if query_filter is None and doc_id_filter:
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="doc_id",
                        match=qmodels.MatchValue(value=doc_id_filter),
                    )
                ]
            )

        # Build BM25 sparse query vector for the query text
        sparse_query = self._encode_sparse([query_text])[0]

        # ── Dense sub-query ───────────────────────────────────────────────────
        dense_hits = self._client.search(
            collection_name=self._collection,
            query_vector=qmodels.NamedVector(name="dense", vector=query_vector),
            limit=k,
            query_filter=query_filter,
            with_payload=True,
        )

        # ── Sparse BM25 sub-query ─────────────────────────────────────────────
        sparse_hits = self._client.search(
            collection_name=self._collection,
            query_vector=qmodels.NamedSparseVector(
                name=_SPARSE_VECTOR_NAME,
                vector=qmodels.SparseVector(
                    indices=sparse_query.indices,
                    values=sparse_query.values,
                ),
            ),
            limit=k,
            query_filter=query_filter,
            with_payload=True,
        )

        # ── Manual Reciprocal Rank Fusion (RRF) ───────────────────────────────
        # RRF score = sum of 1 / (RRF_K + rank) across all ranked lists.
        # Standard constant RRF_K=60 (from the original RRF paper).
        RRF_K = 60
        rrf_scores: dict[int | str, float] = {}
        payloads: dict[int | str, dict] = {}

        for rank, hit in enumerate(dense_hits, start=1):
            pid = hit.id
            rrf_scores[pid] = rrf_scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
            payloads[pid] = dict(hit.payload or {})

        for rank, hit in enumerate(sparse_hits, start=1):
            pid = hit.id
            rrf_scores[pid] = rrf_scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
            if pid not in payloads:
                payloads[pid] = dict(hit.payload or {})

        # Sort by fused RRF score descending, take top-k
        sorted_ids = sorted(rrf_scores, key=lambda p: rrf_scores[p], reverse=True)

        results = []
        for pid in sorted_ids[:k]:
            payload = payloads[pid]
            payload["score"] = rrf_scores[pid]
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
    Qdrant point IDs must be unsigned 64-bit integers or UUID strings.
    We convert the UUID hex to an int that fits in 63 bits.
    """
    return int(chunk_id.replace("-", ""), 16) % (2**63)


# Module-level singleton
qdrant_store = QdrantStore()
