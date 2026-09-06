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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from api.schemas import SendMessageRequest
from core.llm.gemini import gemini_provider
from core.retrieval.engine import RAG_SYSTEM_PROMPT, build_rag_prompt, retrieve
from db.session_store import session_store

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
    session_store.append_message(session_id, role="user", content=body.content)

    # 2. Build context
    #    If the user attached a file inline, its parsed text comes in as inline_context.
    #    That content is the primary source for this turn — prepend it before retrieval results.
    inline = body.inline_context.strip()
    retrieval_result = retrieve(query=body.content)

    if inline:
        if retrieval_result.context:
            combined_context = f"[Attached file]\n{inline}\n\n{retrieval_result.context}"
        else:
            combined_context = f"[Attached file]\n{inline}"
    else:
        combined_context = retrieval_result.context

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

    return StreamingResponse(
        _stream_response(session_id, messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _stream_response(session_id: str, messages: list[dict]):
    """
    Generator that yields SSE-formatted tokens and accumulates the full reply.
    """
    full_reply: list[str] = []

    try:
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
    session_store.append_message(session_id, role="assistant", content=complete_reply)

    # Signal end of stream
    yield "data: [DONE]\n\n"
