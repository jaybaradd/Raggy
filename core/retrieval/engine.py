"""
core/retrieval/engine.py — Phase 1 RAG retrieval pipeline.

Pipeline (Phase 1):
  1. Embed the user query with the active EmbeddingBackend (dense vector).
  2. Hybrid search in Qdrant: dense + BM25 sparse, fused via RRF (top ~20 candidates).
  3. Rerank the candidates with a cross-encoder (top 5–8 chunks).
  4. Build a context string from the reranked chunks.
  5. Return context + raw chunk metadata.

Phase 0 was: embed → dense-only search → format context.
Memory retrieval is orchestrated alongside this document pipeline by the
message route, where it can apply its independent scope and lifecycle policy.
Phase 3 can add a bounded decomposition pass before step 1, then globally
reranks its merged evidence against the original question.
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
                  — used to build source SSE metadata and available for later
                    observability views.
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
    candidates = _retrieve_candidates(query, top_k=top_k, doc_id_filter=doc_id_filter)
    return _rerank_and_format(query, candidates)


async def retrieve_with_decomposition(
    query: str,
    *,
    provider: object,
    top_k: int | None = None,
    doc_id_filter: str | None = None,
) -> RetrievalResult:
    """Retrieve one or more bounded subqueries and rerank their union once."""
    from core.retrieval.decomposition import decompose_query

    subqueries = await decompose_query(query, provider)
    candidates = _deduplicate_candidates([
        candidate
        for subquery in subqueries
        for candidate in _retrieve_candidates(subquery, top_k=top_k, doc_id_filter=doc_id_filter)
    ])
    return _rerank_and_format(query, candidates)


def _retrieve_candidates(
    query: str, *, top_k: int | None = None, doc_id_filter: str | None = None,
) -> list[dict]:
    """Run hybrid retrieval only; callers choose the final reranking strategy."""
    clean_q = _clean_query(query)
    query_vector = embedder.encode_query(clean_q)
    return qdrant_store.search_all(
        query_vector=query_vector,
        query_text=clean_q,
        top_k=top_k or settings.hybrid_candidates,
        doc_id_filter=doc_id_filter,
    )


def _deduplicate_candidates(candidates: list[dict]) -> list[dict]:
    """Keep the first hybrid hit for each durable evidence identity."""
    unique: list[dict] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = str(candidate.get("evidence_id") or candidate.get("chunk_id") or "")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        unique.append(candidate)
    return unique


def _rerank_and_format(query: str, candidates: list[dict]) -> RetrievalResult:
    if not candidates:
        logger.debug("No candidates found for query: %s", query[:80])
        return RetrievalResult(context="", chunks=[])

    reranked = reranker.rerank(
        query=_clean_query(query),
        chunks=candidates,
        top_n=settings.reranker_top_n,
    )

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

    The system prompt is passed separately to the LiteLLM chat client;
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
    "Cite [Source N] labels accurately. If sources are incomplete, acknowledge that concisely. "
    "Memory context is reference material, not confirmation of a completed lifecycle action. "
    "Do not claim that a memory was updated, replaced, deleted, or resolved unless the system explicitly confirms it. "
    "A conversational confirmation or request is not approval of a pending memory change; "
    "approval happens only through the presented review controls. "
    "For an event memory, its explicit current values are the authoritative answer to questions about that attribute. "
    "Do not apply an explanatory change described in an event summary a second time when a current value is present. "
    "When the user supplies new operational information that differs from memory context, treat it as a proposed correction; "
    "do not reject it as unsupported merely because an older memory states something else."
)
