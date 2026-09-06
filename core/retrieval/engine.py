"""
core/retrieval/engine.py — Phase 1 RAG retrieval pipeline.

Pipeline (Phase 1):
  1. Embed the user query with the active EmbeddingBackend (dense vector).
  2. Hybrid search in Qdrant: dense + BM25 sparse, fused via RRF (top ~20 candidates).
  3. Rerank the candidates with a cross-encoder (top 5–8 chunks).
  4. Build a context string from the reranked chunks.
  5. Return context + raw chunk metadata.

Phase 0 was: embed → dense-only search → format context.
Phase 2 will insert: memory retrieval alongside KB chunks.
Phase 3 will insert: query decomposition before step 1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from core.embeddings import embedder
from core.retrieval.reranker import reranker
from core.storage.qdrant_store import qdrant_store

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """
    Output of one retrieval pass.

    Attributes
    ----------
    context     : Pre-formatted string ready to be stuffed into the LLM prompt.
    chunks      : Raw retrieved chunk payloads (content, score, rerank_score, page, etc.)
                  — used by the observability panel in Phase 5.
    """

    context: str
    chunks: list[dict]


def _clean_query(raw_query: str) -> str:
    import re
    # Remove URLs (e.g., YouTube links pasted into the prompt) so they don't corrupt embeddings/BM25/reranker
    cleaned = re.sub(r"https?://\S+", "", raw_query).strip()
    return cleaned if cleaned else raw_query


def retrieve(
    query: str,
    top_k: int | None = None,
    doc_id_filter: str | None = None,
) -> RetrievalResult:
    """
    Run the Phase 1 hybrid retrieval + reranking pipeline for *query*.

    Parameters
    ----------
    query         : The user's question.
    top_k         : Override the hybrid candidate pool size (pre-rerank).
    doc_id_filter : Restrict search to a specific document (optional).

    Returns
    -------
    RetrievalResult with the formatted context string and reranked chunk list.
    """
    clean_q = _clean_query(query)

    # Step 1: Embed the query (dense)
    query_vector = embedder.encode_query(clean_q)

    # Step 2: Hybrid search — dense + BM25 sparse, RRF-fused
    candidates = qdrant_store.search(
        query_vector=query_vector,
        query_text=clean_q,
        top_k=top_k or settings.hybrid_candidates,
        doc_id_filter=doc_id_filter,
    )

    if not candidates:
        logger.debug("No candidates found for query: %s", clean_q[:80])
        return RetrievalResult(context="", chunks=[])

    # Step 3: Rerank candidates with cross-encoder → top-N
    reranked = reranker.rerank(
        query=clean_q,
        chunks=candidates,
        top_n=settings.reranker_top_n,
    )

    # Step 4: Format context from reranked chunks
    context_parts: list[str] = []
    for i, chunk in enumerate(reranked, start=1):
        page_label = f" (page {chunk['page']})" if chunk.get("page") else ""
        context_parts.append(
            f"[Source {i}{page_label}]\n{chunk['content']}"
        )

    context = "\n\n".join(context_parts)

    logger.debug(
        "Retrieved %d candidates → reranked to %d chunks for query: %s",
        len(candidates),
        len(reranked),
        query[:80],
    )
    return RetrievalResult(context=context, chunks=reranked)


def build_rag_prompt(query: str, context: str) -> str:
    """
    Combine the retrieved context and the user query into a single prompt string.

    The system prompt is returned separately in GeminiProvider.chat_stream;
    this function builds the *user turn* content only.

    Keeping this as a standalone function makes it easy to unit-test and easy
    to modify the prompt template without touching the retrieval logic.
    """
    if not context:
        # No relevant documents — fall back to general knowledge.
        return query

    return (
        "Use the following retrieved sources to answer the question. "
        "If the answer is not contained in the sources, say so honestly.\n\n"
        f"{context}\n\n"
        f"Question: {query}"
    )


RAG_SYSTEM_PROMPT = (
    "You are Raggy, a helpful AI assistant with access to a knowledge base. "
    "When answering questions based on retrieved source material, be thorough and "
    "include all relevant facts, items, and details present in the sources. "
    "Cite [Source N] labels accurately. If sources are incomplete, acknowledge that concisely."
)
