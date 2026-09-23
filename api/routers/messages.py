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

from api.schemas import MemoryExtractionStatusResponse, SendMessageRequest
from core.llm.client import llm_client
from core.memory.extractor import MemoryExtractor
from core.memory.jobs import extract_turn_memories
from core.memory.update_context import resolve_update_context
from core.memory.planner import MemoryContextResult
from core.memory.projection import memory_text
from db.repository_factory import repositories
from core.retrieval.engine import (
    RAG_SYSTEM_PROMPT, RetrievalResult, build_rag_prompt, retrieve_with_decomposition,
)
from core.retrieval.memory import build_memory_context
memory_store = repositories.memories
session_store = repositories.sessions
graph_store = repositories.graph

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["messages"])


@router.get("/{session_id}/memory-extractions/{source_turn_id}", response_model=MemoryExtractionStatusResponse)
def memory_extraction_status(session_id: str, source_turn_id: str) -> MemoryExtractionStatusResponse:
    """Expose post-turn memory work without exposing another session's turn."""
    messages = session_store.get_messages(session_id)
    if not any(message["message_id"] == source_turn_id and message["role"] == "assistant" for message in messages):
        raise HTTPException(status_code=404, detail="Assistant turn not found")

    status = memory_store.get_extraction_status(source_turn_id, MemoryExtractor(llm_client).version)
    if status is None:
        return MemoryExtractionStatusResponse(status="queued", outcome="pending", message="Checking this turn for memories.")
    if status["status"] == "running":
        return MemoryExtractionStatusResponse(status="running", outcome="pending", message="Checking this turn for memories.")
    if status["status"] == "failed":
        return MemoryExtractionStatusResponse(status="failed", outcome="failed", message="Memory processing could not finish.")

    records = memory_store.list(owner_id="default", source_turn_id=source_turn_id, status=None, limit=50)
    memory_ids = [record.memory_id for record in records]
    open_conflicts = [
        conflict for conflict in memory_store.list_conflicts(owner_id="default", status="open")
        if conflict["incoming_memory_id"] in memory_ids
    ]
    if open_conflicts or any(record.status == "candidate" for record in records):
        return MemoryExtractionStatusResponse(
            status="completed", outcome="review_required", memory_ids=memory_ids,
            conflict_ids=[int(conflict["conflict_id"]) for conflict in open_conflicts],
            message="A memory change is ready for review.",
        )
    if records:
        return MemoryExtractionStatusResponse(
            status="completed", outcome="memory_saved", memory_ids=memory_ids,
            message="Saved durable memory from this turn.",
        )
    return MemoryExtractionStatusResponse(status="completed", outcome="no_memory", message="No durable memory was created from this turn.")


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
    history = session_store.get_messages(session_id)
    update_context = await resolve_update_context(
        user_content=body.content, messages=history[:-1], store=memory_store,
        provider=llm_client, owner_id="default", session_id=session_id,
        project_id=session.get("project_id"), project_scope=session.get("project_scope"),
    )

    # 2. Build context
    #    If the user attached a file inline, its parsed text comes in as inline_context.
    #    That content is the primary source for this turn — prepend it before retrieval results.
    inline = body.inline_context.strip()
    if body.use_knowledge_base:
        retrieval_result = await retrieve_with_decomposition(
            query=body.content, provider=llm_client,
        )
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

    # A unique event from the immediately preceding response is the only safe
    # continuity context for the next turn.  Do not fall back to project-wide
    # semantic retrieval and let a different event replace that referent.
    # The update classifier controls response wording, not target identity.
    if update_context.extraction_target is not None:
        target = update_context.extraction_target
        memory = {
            "memory_id": target.memory_id,
            "memory_text": memory_text(target),
            "kind": target.kind,
            "scope": target.scope,
            "confidence": target.confidence,
            "prompt_label": "M1",
            "candidate_source": "previous_response",
            "selection_source": "previous_response",
            "selection_relation": "continuity",
            "selection_confidence": 1.0,
        }
        memory_result = MemoryContextResult(
            context=f"[M1 | {target.kind} | {target.scope}]\n{memory['memory_text']}",
            memories=[memory], planner_status="selected",
            rationale="Bounded to the single event used in the preceding assistant response.",
            reconciliation_hints=[memory], candidate_counts={"previous_response": 1},
        )
    elif update_context.ambiguous:
        memory_result = MemoryContextResult(
            context="", memories=[], planner_status="no_selection",
            rationale="Potential update target is ambiguous or unavailable.",
            reconciliation_hints=[], candidate_counts={},
        )
    else:
        memory_result = await build_memory_context(
            query=body.content,
            owner_id="default",
            session_id=session_id,
            project_id=session.get("project_id"),
            project_scope=session.get("project_scope"),
            store=memory_store,
            graph=graph_store,
            planner_provider=llm_client,
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
                     "graph_seed_memory_id": memory.get("graph_seed_memory_id"),
                     "graph_relationship_id": memory.get("graph_relationship_id"),
                     "graph_relationship_type": memory.get("graph_relationship_type"),
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
                     "graph_seed_memory_id": memory.get("graph_seed_memory_id"),
                     "graph_relationship_id": memory.get("graph_relationship_id"),
                     "graph_relationship_type": memory.get("graph_relationship_type"),
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
    if update_context.target is not None:
        augmented_query += (
            "\n\nThe user has proposed a change to one remembered event. It is pending "
            "review in the UI. Do not ask for conversational confirmation or claim that the "
            "memory was updated, replaced, or confirmed."
        )
    elif update_context.ambiguous:
        augmented_query += (
            "\n\nThe user appears to be changing a remembered event, but no single target "
            "was resolved. Ask one concise clarification question; do not guess or claim an update."
        )

    # 4. Build message history for multi-turn context
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
            memory_planner={
                "status": memory_result.planner_status,
                "rationale": memory_result.rationale,
                "graph_status": memory_result.graph_status,
                "candidate_counts": memory_result.candidate_counts or {},
            },
            reconciliation_hints=memory_result.reconciliation_hints or [],
            user_content=body.content,
            trace_id=trace_id,
            extraction_target=update_context.extraction_target,
            recent_messages=update_context.recent_messages,
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
    extraction_target,
    recent_messages: list[dict[str, str]],
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
        async for token in llm_client.chat_stream(
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
        extractor=MemoryExtractor(llm_client),
        source_turn_id=assistant_turn_id,
        session_id=session_id,
        project_id=project_id,
        project_scope=project_scope,
        owner_id="default",
        user_content=user_content,
        assistant_content=complete_reply,
        evidence_refs=evidence_refs,
        reconciliation_hints=reconciliation_hints,
        # Constrain persistence to the trace-linked event even if reply-time
        # classification did not recognize an indirect update assertion.
        update_target=extraction_target,
        recent_messages=recent_messages,
        graph=graph_store,
    ))

    yield f"event: memory_processing\ndata: {json.dumps({'type': 'memory_processing', 'source_turn_id': assistant_turn_id})}\n\n"

    # Signal end of stream
    yield "data: [DONE]\n\n"
