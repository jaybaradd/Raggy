"""Review and lifecycle APIs for candidate and durable memories."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException

from api.schemas import MemoryEditRequest, MemoryListResponse, MemoryPromotionRequest, MemorySupersedeRequest
from core.storage.memory_store import memory_store
from core.storage.qdrant_store import qdrant_store

router = APIRouter(prefix="/memories", tags=["memories"])
logger = logging.getLogger(__name__)


def _owner(owner_id: str) -> str:
    return owner_id or "default"


def _sync_projection(record) -> None:
    """Projection failure must not roll back the authoritative memory change."""
    try:
        qdrant_store.upsert_memory(record)
    except Exception:
        logger.exception("Memory projection sync failed for %s", record.memory_id)


@router.get("/candidates", response_model=MemoryListResponse)
def list_candidates(owner_id: str = Header(default="default", alias="X-Owner-ID")) -> MemoryListResponse:
    records = memory_store.list(owner_id=_owner(owner_id), status="candidate")
    return MemoryListResponse(memories=[record.model_dump(mode="json") for record in records])


@router.get("", response_model=MemoryListResponse)
def list_memories(owner_id: str = Header(default="default", alias="X-Owner-ID"),
                  scope: str | None = None, status: str | None = "active",
                  session_id: str | None = None, project_scope: str | None = None,
                  kind: str | None = None) -> MemoryListResponse:
    records = memory_store.list(owner_id=_owner(owner_id), scope=scope, status=status,
                                session_id=session_id, project_scope=project_scope, kind=kind)
    return MemoryListResponse(memories=[record.model_dump(mode="json") for record in records])


def _get(memory_id: str, owner_id: str):
    record = memory_store.get(memory_id)
    if record is None or record.owner_id != _owner(owner_id):
        raise HTTPException(status_code=404, detail="Memory not found")
    return record


@router.post("/{memory_id}/confirm")
def confirm_memory(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    try:
        record = memory_store.review(memory_id, "confirm", actor_id=_owner(owner_id))
        _sync_projection(record)
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{memory_id}/reject")
def reject_memory(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    try:
        record = memory_store.review(memory_id, "reject", actor_id=_owner(owner_id))
        _sync_projection(record)
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{memory_id}/expire")
def expire_memory(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    try:
        record = memory_store.review(memory_id, "expire", actor_id=_owner(owner_id))
        _sync_projection(record)
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{memory_id}/promote")
def promote_memory(memory_id: str, body: MemoryPromotionRequest,
                   owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    try:
        record = memory_store.promote(memory_id, scope=body.scope, project_scope=body.project_scope,
                                      actor_id=_owner(owner_id))
        _sync_projection(record)
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/{memory_id}")
def edit_memory(memory_id: str, body: MemoryEditRequest,
                owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    try:
        record = memory_store.edit(memory_id, body.payload, actor_id=_owner(owner_id))
        _sync_projection(record)
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/{memory_id}/supersede")
def supersede_memory(memory_id: str, body: MemorySupersedeRequest,
                     owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    _get(body.replacement_memory_id, owner_id)
    try:
        record = memory_store.supersede(memory_id, body.replacement_memory_id,
                                        actor_id=_owner(owner_id))
        _sync_projection(record)
        replacement = memory_store.get(body.replacement_memory_id)
        if replacement:
            _sync_projection(replacement)
        return record.model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
