"""
api/routers/messages.py — streaming chat endpoint.

Route
-----
POST /sessions/{session_id}/messages

Accepts a user message, runs the RAG retrieval pipeline, then streams the
LLM response back as Server-Sent Events (SSE).

SSE format (one event per token):
    data: <token text>\n\n

Final event signals completion:
    data: [DONE]\n\n

The frontend reads this with EventSource or fetch + ReadableStream.
"""

from __future__ import annotations

import json
import logging
import asyncio
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.schemas import SendMessageRequest
from core.llm.gemini import gemini_provider
from core.memory.extractor import MemoryExtractor
from core.memory.jobs import extract_turn_memories
from db.repository_factory import repositories
from core.retrieval.engine import RAG_SYSTEM_PROMPT, RetrievalResult, build_rag_prompt, retrieve
from core.retrieval.memory import build_memory_context
memory_store = repositories.memories
session_store = repositories.sessions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages")
async def send_message(
    session_id: str,
    body: SendMessageRequest,
) -> StreamingResponse:
    """
    Receive a user message, run RAG, stream the assistant reply via SSE.

    Steps
    -----
    1. Validate session exists.
    2. Store the user message in session history.
    3. Retrieve relevant chunks from Qdrant.
    4. Build the augmented user prompt.
    5. Stream the LLM response token by token.
    6. Store the complete assistant reply after streaming finishes.
    """
    session = session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    # 1. Persist the user message
    trace_id = str(uuid4())
    user_message_id = session_store.append_message(
        session_id,
        role="user",
        content=body.content,
        attachments=[attachment.model_dump(mode="json") for attachment in body.attachments],
        trace_id=trace_id,
    )

    # 2. Build context
    #    If the user attached a file inline, its parsed text comes in as inline_context.
    #    That content is the primary source for this turn — prepend it before retrieval results.
    inline = body.inline_context.strip()
    if body.use_knowledge_base:
        retrieval_result = retrieve(query=body.content)
    else:
        retrieval_result = RetrievalResult(context="", chunks=[])
        logger.info("Knowledge-base retrieval disabled for query %r", body.content[:80])

    if inline:
        if retrieval_result.context:
            combined_context = f"[Attached file]\n{inline}\n\n{retrieval_result.context}"
        else:
            combined_context = f"[Attached file]\n{inline}"
    else:
        combined_context = retrieval_result.context

    memory_result = await build_memory_context(
        query=body.content,
        owner_id="default",
        session_id=session_id,
        project_id=session.get("project_id"),
        project_scope=session.get("project_scope"),
        store=memory_store,
        planner_provider=gemini_provider,
    )
    if memory_result.context:
        memory_context = "CONFIRMED MEMORY CONTEXT\n" + memory_result.context
        combined_context = f"{combined_context}\n\n{memory_context}" if combined_context else memory_context

    for rank, memory in enumerate(memory_result.memories, start=1):
        logger.info(
            "Memory trace %s: %s=%s rank=%d score=%.4f scope=%s source=%s relation=%s",
            trace_id, memory.get("prompt_label", "memory"), memory["memory_id"],
            rank, float(memory.get("score") or 0.0), memory.get("scope"),
            memory.get("selection_source"), memory.get("selection_relation"),
        )
        memory_store.record_access_event(
            trace_id=trace_id,
            session_id=session_id,
            message_id=user_message_id,
            memory_id=memory["memory_id"],
            event_type="retrieved",
            prompt_label=memory.get("prompt_label"),
            rank=rank,
            score=memory.get("score"),
            details={"candidate_source": memory.get("candidate_source"),
                     "selection_source": memory.get("selection_source"),
                     "selection_relation": memory.get("selection_relation"),
                     "selection_confidence": memory.get("selection_confidence"),
                     "planner_status": memory_result.planner_status},
        )
        memory_store.record_access_event(
            trace_id=trace_id,
            session_id=session_id,
            message_id=user_message_id,
            memory_id=memory["memory_id"],
            event_type="injected",
            prompt_label=memory.get("prompt_label"),
            rank=rank,
            score=memory.get("score"),
            details={"candidate_source": memory.get("candidate_source"),
                     "selection_source": memory.get("selection_source"),
                     "selection_relation": memory.get("selection_relation"),
                     "selection_confidence": memory.get("selection_confidence"),
                     "planner_status": memory_result.planner_status},
        )
    if memory_result.memories:
        logger.info("Memory trace %s: injected %d memories via %s", trace_id,
                    len(memory_result.memories), memory_result.planner_status)

    # Log what was retrieved so retrieval quality is visible in the terminal
    if inline:
        logger.info("Inline attachment context (%d chars) prepended for query %r", len(inline), body.content[:80])
    if retrieval_result.chunks:
        logger.info(
            "Hybrid+rerank: %d chunks for query %r:",
            len(retrieval_result.chunks),
            body.content[:80],
        )
        for i, c in enumerate(retrieval_result.chunks, 1):
            snippet = c["content"][:120].replace("\n", " ")
            rerank_score = c.get("rerank_score", c.get("score", 0.0))
            logger.info("  [%d] rerank=%.4f | %s", i, rerank_score, snippet)
    else:
        logger.info("No chunks retrieved for query %r", body.content[:80])

    # 3. Build the augmented prompt for this turn
    augmented_query = build_rag_prompt(body.content, combined_context)

    # 4. Build message history for multi-turn context
    history = session_store.get_messages(session_id)
    messages = []
    for msg in history[:-1]:   # all but the last (which we just stored)
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": augmented_query})

    sources = [
        {
            "source_index": index,
            "evidence_id": chunk.get("evidence_id", chunk.get("chunk_id")),
            "doc_id": chunk.get("doc_id"),
            "filename": chunk.get("source_name"),
            "modality": chunk.get("modality"),
            "representation": chunk.get("representation", "text"),
            "page": chunk.get("page"),
            "time_range": chunk.get("time_range"),
            "cell_range": chunk.get("cell_range"),
            "bbox": chunk.get("bbox"),
            "raw_file_uri": chunk.get("raw_file_uri"),
            "source_url": (
                chunk.get("raw_file_uri")
                if str(chunk.get("raw_file_uri", "")).startswith("http")
                else f"/api/documents/{chunk.get('doc_id')}/source"
            ),
        }
        for index, chunk in enumerate(retrieval_result.chunks, start=1)
    ]

    return StreamingResponse(
        _stream_response(
            session_id=session_id,
            project_id=session.get("project_id"),
            project_scope=session.get("project_scope"),
            messages=messages,
            sources=sources,
            memories=memory_result.memories,
            memory_planner={"status": memory_result.planner_status, "rationale": memory_result.rationale},
            reconciliation_hints=memory_result.reconciliation_hints or [],
            user_content=body.content,
            trace_id=trace_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_response(
    session_id: str,
    project_id: str | None,
    project_scope: str | None,
    messages: list[dict],
    sources: list[dict],
    memories: list[dict],
    memory_planner: dict,
    reconciliation_hints: list[dict],
    user_content: str,
    trace_id: str,
):
    """
    Generator that yields SSE-formatted tokens and accumulates the full reply.
    """
    full_reply: list[str] = []

    try:
        # Send structured provenance before token generation. Existing clients
        # can ignore this event and continue consuming token data events.
        yield f"event: sources\ndata: {json.dumps({'type': 'sources', 'trace_id': trace_id, 'sources': sources})}\n\n"
        yield f"event: memories\ndata: {json.dumps({'type': 'memories', 'trace_id': trace_id, 'memories': memories, 'planner': memory_planner})}\n\n"
        async for token in gemini_provider.chat_stream(
            messages=messages,
            system_prompt=RAG_SYSTEM_PROMPT,
        ):
            full_reply.append(token)
            # SSE format: "data: <payload>\n\n"
            yield f"data: {json.dumps(token)}\n\n"

    except Exception as exc:
        logger.exception("Error during LLM stream for session %s", session_id)
        error_payload = json.dumps({"error": str(exc)})
        yield f"data: {error_payload}\n\n"
        return

    # Persist the complete assistant reply
    complete_reply = "".join(full_reply)
    assistant_turn_id = session_store.append_message(
        session_id, role="assistant", content=complete_reply, trace_id=trace_id,
    )
    evidence_refs = [source["evidence_id"] for source in sources if source.get("evidence_id")]
    asyncio.create_task(extract_turn_memories(
        store=memory_store,
        extractor=MemoryExtractor(gemini_provider),
        source_turn_id=assistant_turn_id,
        session_id=session_id,
        project_id=project_id,
        project_scope=project_scope,
        owner_id="default",
        user_content=user_content,
        assistant_content=complete_reply,
        evidence_refs=evidence_refs,
        reconciliation_hints=reconciliation_hints,
    ))

    # Signal end of stream
    yield "data: [DONE]\n\n"
