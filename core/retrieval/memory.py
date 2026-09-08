"""Scoped semantic retrieval of active memory projections."""

from __future__ import annotations

from dataclasses import dataclass

from core.embeddings import embedder
from core.storage.qdrant_store import qdrant_store


@dataclass
class MemoryRetrievalResult:
    context: str
    memories: list[dict]


def retrieve_memories(query: str, *, owner_id: str = "default", session_id: str,
                      project_scope: str | None = None, top_k: int = 5) -> MemoryRetrievalResult:
    hits = qdrant_store.search_memories(
        query_vector=embedder.encode_query(query), query_text=query, owner_id=owner_id,
        session_id=session_id, project_scope=project_scope, top_k=top_k,
    )
    if not hits:
        return MemoryRetrievalResult(context="", memories=[])
    parts = []
    for index, memory in enumerate(hits, start=1):
        parts.append(f"[Memory {index} | {memory.get('kind')} | {memory.get('scope')}]\n{memory.get('memory_text', '')}")
    return MemoryRetrievalResult(context="\n\n".join(parts), memories=hits)
