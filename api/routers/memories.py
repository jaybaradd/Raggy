"""Review and lifecycle APIs for candidate and durable memories."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException

from api.schemas import (MemoryConflictResolutionRequest, MemoryEditRequest, MemoryListResponse,
                         MemoryPromotionRequest, MemorySupersedeRequest)
from core.memory.projections import sync_pending_projections
from db.repository_factory import repositories

graph_store = repositories.graph
memory_store = repositories.memories
session_store = repositories.sessions

router = APIRouter(prefix="/memories", tags=["memories"])
logger = logging.getLogger(__name__)


def _owner(owner_id: str) -> str:
    return owner_id or "default"


def _project(owner_id: str, project_id: str | None, project_scope: str | None) -> tuple[str | None, str | None]:
    """Validate a stable project ID and reject mismatched display-name input."""
    if not project_id:
        return None, project_scope
    project = session_store.get_project(project_id, _owner(owner_id))
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if project_scope and " ".join(project_scope.split()).casefold() != project["normalized_name"]:
        raise HTTPException(status_code=400, detail="project_id and project_scope do not match")
    return project["project_id"], project["name"]


def _sync_projection(record) -> None:
    """Projection failure must not roll back the authoritative memory change."""
    try:
        sync_pending_projections(store=memory_store, graph=graph_store)
    except Exception:
        logger.exception("Memory projection sync failed for %s", record.memory_id)


@router.get("/candidates", response_model=MemoryListResponse)
def list_candidates(owner_id: str = Header(default="default", alias="X-Owner-ID")) -> MemoryListResponse:
    records = memory_store.list(owner_id=_owner(owner_id), status="candidate")
    return MemoryListResponse(memories=[record.model_dump(mode="json") for record in records])


@router.get("", response_model=MemoryListResponse)
def list_memories(owner_id: str = Header(default="default", alias="X-Owner-ID"),
                  scope: str | None = None, status: str | None = "active",
                  session_id: str | None = None, project_id: str | None = None, project_scope: str | None = None,
                  kind: str | None = None, limit: int = 100) -> MemoryListResponse:
    if status == "all":
        status = None
    project_id, project_scope = _project(owner_id, project_id, project_scope)
    records = memory_store.list(owner_id=_owner(owner_id), scope=scope, status=status,
                                session_id=session_id, project_id=project_id, project_scope=project_scope, kind=kind,
                                limit=min(max(limit, 1), 200))
    return MemoryListResponse(memories=[record.model_dump(mode="json") for record in records])


def _get(memory_id: str, owner_id: str):
    record = memory_store.get_owned(memory_id, owner_id=_owner(owner_id))
    if record is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return record


@router.get("/graph/nodes")
def list_graph_nodes(owner_id: str = Header(default="default", alias="X-Owner-ID"),
                     scope: str | None = None, project_id: str | None = None, project_scope: str | None = None):
    """Inspect the rebuildable graph projection for the current owner."""
    project_id, project_scope = _project(owner_id, project_id, project_scope)
    return {"nodes": graph_store.list_nodes(owner_id=_owner(owner_id), scope=scope, project_id=project_id, project_scope=project_scope)}


@router.get("/graph/edges")
def list_graph_edges(owner_id: str = Header(default="default", alias="X-Owner-ID"),
                     scope: str | None = None, project_id: str | None = None, project_scope: str | None = None):
    """Inspect active, provenance-linked graph edges for the current owner."""
    project_id, project_scope = _project(owner_id, project_id, project_scope)
    return {"edges": graph_store.list_edges(owner_id=_owner(owner_id), scope=scope, project_id=project_id, project_scope=project_scope)}


@router.get("/projections")
def list_projection_jobs(owner_id: str = Header(default="default", alias="X-Owner-ID"),
                         status: str | None = None):
    return {"jobs": memory_store.list_projection_jobs(owner_id=_owner(owner_id), status=status)}


@router.post("/expiry-sweep")
def expire_due_memories(owner_id: str = Header(default="default", alias="X-Owner-ID")):
    """Run a scoped, idempotent expiry sweep for the current owner."""
    expired = memory_store.expire_due(owner_id=_owner(owner_id))
    if expired:
        try:
            sync_pending_projections(store=memory_store, graph=graph_store)
        except Exception:
            logger.exception("Memory projection sync failed after expiry sweep")
    return {"expired_count": len(expired), "memory_ids": [record.memory_id for record in expired]}


@router.get("/conflicts")
def list_conflicts(owner_id: str = Header(default="default", alias="X-Owner-ID"),
                   status: str | None = "open", project_id: str | None = None,
                   project_scope: str | None = None):
    project_id, project_scope = _project(owner_id, project_id, project_scope)
    conflicts = memory_store.list_conflicts(owner_id=_owner(owner_id), status=status)
    if project_id:
        conflicts = [item for item in conflicts if memory_store.get(item["incoming_memory_id"]).project_id == project_id]
    elif project_scope:
        conflicts = [item for item in conflicts if item.get("project_scope") == project_scope]
    return {"conflicts": conflicts}


@router.get("/conflicts/{conflict_id}")
def get_conflict(conflict_id: int, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    conflict = memory_store.get_conflict(conflict_id, owner_id=_owner(owner_id))
    if conflict is None:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return conflict


@router.post("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: int, body: MemoryConflictResolutionRequest,
                     owner_id: str = Header(default="default", alias="X-Owner-ID")):
    try:
        conflict = memory_store.resolve_conflict(conflict_id, action=body.action, actor_id=_owner(owner_id))
        for memory_id in (conflict["incoming_memory_id"], conflict["existing_memory_id"]):
            record = memory_store.get(memory_id)
            if record:
                _sync_projection(record)
        return conflict
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=404 if isinstance(exc, KeyError) else 422, detail=str(exc)) from exc


@router.get("/{memory_id}/audit")
def get_memory_audit(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    try:
        return {"events": memory_store.list_audit_events(memory_id, owner_id=_owner(owner_id))}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory not found") from None


@router.get("/{memory_id}/relationships")
def get_memory_relationships(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    """Return authoritative lifecycle/related links for one memory."""
    try:
        return {"relationships": memory_store.list_relationships(memory_id, owner_id=_owner(owner_id))}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory not found") from None


@router.get("/{memory_id}/access-events")
def get_memory_access_events(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    try:
        return {"events": memory_store.list_access_events(memory_id, owner_id=_owner(owner_id))}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory not found") from None


@router.get("/{memory_id}")
def get_memory(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    return _get(memory_id, owner_id).model_dump(mode="json")


@router.delete("/{memory_id}")
def forget_memory(memory_id: str, owner_id: str = Header(default="default", alias="X-Owner-ID")):
    _get(memory_id, owner_id)
    record = memory_store.forget(memory_id, actor_id=_owner(owner_id))
    _sync_projection(record)
    return record.model_dump(mode="json")


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
        project_id, project_scope = _project(owner_id, body.project_id, body.project_scope)
        record = memory_store.promote(memory_id, scope=body.scope, project_scope=project_scope,
                                      actor_id=_owner(owner_id))
        record.project_id = project_id
        memory_store.upsert(record, event_type="project_id_assigned", actor_id=_owner(owner_id))
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
