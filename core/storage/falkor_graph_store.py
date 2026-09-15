"""FalkorDB implementation of Raggy's rebuildable memory graph contract."""

from __future__ import annotations

from typing import Any

from core.memory.graph_projection import GraphMemoryCandidate, MemoryGraphNode, MemoryGraphRelationship
from core.memory.models import MemoryRecord


class FalkorGraphStore:
    """Project durable memory records into one named FalkorDB graph.

    FalkorDB is derived state only. Every read requires an owner ID, and every
    write is idempotent so the durable projection outbox can retry safely.
    """

    def __init__(self, *, url: str, graph_name: str, client: Any | None = None) -> None:
        if not url:
            raise ValueError("FALKORDB_URL must not be blank")
        if not graph_name:
            raise ValueError("FALKORDB_GRAPH_NAME must not be blank")
        if client is None:
            try:
                from falkordb import FalkorDB
            except ImportError as error:
                raise RuntimeError(
                    "Falkor graph projection requires the FalkorDB Python package. "
                    "Install it with: uv pip install FalkorDB"
                ) from error
            client = FalkorDB.from_url(url)
        self._client = client
        self._graph = client.select_graph(graph_name)
        try:
            client.execute_command("PING")
        except Exception as error:
            raise RuntimeError(f"FalkorDB is unavailable at {url}") from error

    def sync_memory(self, record: MemoryRecord) -> None:
        node = MemoryGraphNode.from_record(record)
        self._graph.query("""
            MERGE (memory:Memory {memory_id: $memory_id})
            SET memory += $properties
        """, {"memory_id": node.memory_id, "properties": _node_properties(node)})
        if not node.is_active:
            self._graph.query("""
                MATCH (memory:Memory {memory_id: $memory_id})-[edge]-()
                DELETE edge
            """, {"memory_id": node.memory_id})

    def sync_relationship(self, relationship: MemoryGraphRelationship) -> None:
        if not relationship.is_projectable:
            self.remove_relationship(relationship.relationship_id)
            return
        self.sync_memory(relationship.from_record)
        self.sync_memory(relationship.to_record)
        relationship_label = _relationship_label(relationship.relationship_type)
        self._graph.query(f"""
            MATCH (source:Memory {{memory_id: $from_memory_id, owner_id: $owner_id}})
            MATCH (target:Memory {{memory_id: $to_memory_id, owner_id: $owner_id}})
            MERGE (source)-[edge:{relationship_label} {{relationship_id: $relationship_id}}]->(target)
            SET edge.relationship_type = $relationship_type,
                edge.created_at = $created_at,
                edge.projected_at = $projected_at
        """, {
            "relationship_id": relationship.relationship_id,
            "from_memory_id": relationship.from_record.memory_id,
            "to_memory_id": relationship.to_record.memory_id,
            "owner_id": relationship.from_record.owner_id,
            "relationship_type": relationship_label,
            "created_at": relationship.created_at.isoformat(),
            "projected_at": _utc_now(),
        })

    def remove_relationship(self, relationship_id: str) -> None:
        self._graph.query("""
            MATCH ()-[edge {relationship_id: $relationship_id}]-()
            DELETE edge
        """, {"relationship_id": relationship_id})

    def reset_projection(self) -> None:
        """Clear this named derived graph, never the authoritative database."""
        self._graph.query("MATCH (memory:Memory) DETACH DELETE memory", {})

    def list_nodes(self, *, owner_id: str, scope: str | None = None,
                   project_id: str | None = None, project_scope: str | None = None,
                   active: bool | None = True) -> list[dict]:
        clauses, params = _node_filters(
            owner_id=owner_id, scope=scope, project_id=project_id,
            project_scope=project_scope, active=active, alias="memory",
        )
        return _read_maps(
            self._graph, f"MATCH (memory:Memory) WHERE {' AND '.join(clauses)} RETURN properties(memory)", params,
        )

    def list_edges(self, *, owner_id: str, scope: str | None = None,
                   project_id: str | None = None, project_scope: str | None = None) -> list[dict]:
        clauses, params = _node_filters(
            owner_id=owner_id, scope=scope, project_id=project_id,
            project_scope=project_scope, active=None, alias="source",
        )
        clauses.extend([
            "target.owner_id = $owner_id", "target.is_active = true",
            "(source.is_active = true OR edge.relationship_type = 'SUPERSEDED_BY')",
        ])
        return _read_maps(self._graph, f"""
            MATCH (source:Memory)-[edge]->(target:Memory)
            WHERE {' AND '.join(clauses)}
            RETURN properties(edge)
        """, params)

    def expand_memory_candidates(self, *, seed_memory_ids: list[str], owner_id: str,
                                 session_id: str, project_id: str | None,
                                 project_scope: str | None, limit: int = 4) -> list[GraphMemoryCandidate]:
        """Return one-hop, scoped neighbours as IDs plus relationship provenance."""
        if not seed_memory_ids or limit < 1:
            return []
        clauses, params = _visibility_filters(
            alias="candidate", session_id=session_id, project_id=project_id, project_scope=project_scope,
        )
        params.update({"owner_id": owner_id, "seed_memory_ids": list(dict.fromkeys(seed_memory_ids))[:50], "limit": limit})
        rows = _read_rows(self._graph, f"""
            MATCH (seed:Memory)-[edge:RELATED]-(candidate:Memory)
            WHERE seed.memory_id IN $seed_memory_ids AND seed.owner_id = $owner_id
              AND candidate.owner_id = $owner_id AND candidate.is_active = true
              AND {' AND '.join(clauses)}
            RETURN candidate.memory_id, seed.memory_id, edge.relationship_id, edge.relationship_type
            LIMIT $limit
        """, params)
        return [GraphMemoryCandidate(
            memory_id=str(row[0]), seed_memory_id=str(row[1]), relationship_id=str(row[2]),
            relationship_type=str(row[3]).lower(),
        ) for row in rows]


def _node_properties(node: MemoryGraphNode) -> dict[str, Any]:
    return {
        "memory_id": node.memory_id, "owner_id": node.owner_id, "scope": node.scope,
        "session_id": node.session_id, "project_id": node.project_id,
        "project_scope": node.project_scope, "kind": node.kind, "status": node.status,
        "confidence": node.confidence, "user_confirmed": node.user_confirmed,
        "valid_from": node.valid_from.isoformat(),
        "valid_to": node.valid_to.isoformat() if node.valid_to else None,
        "payload_json": node.payload_json, "source_turn_id": node.source_turn_id,
        "is_active": node.is_active, "projection_version": "v1", "projected_at": _utc_now(),
    }


def _node_filters(*, owner_id: str, scope: str | None, project_id: str | None,
                  project_scope: str | None, active: bool | None, alias: str) -> tuple[list[str], dict[str, Any]]:
    clauses = [f"{alias}.owner_id = $owner_id"]
    params: dict[str, Any] = {"owner_id": owner_id}
    if active is not None:
        clauses.append(f"{alias}.is_active = $active")
        params["active"] = active
    if scope is not None:
        clauses.append(f"{alias}.scope = $scope")
        params["scope"] = scope
    if project_id is not None:
        clauses.append(f"{alias}.project_id = $project_id")
        params["project_id"] = project_id
    elif project_scope is not None:
        clauses.append(f"{alias}.project_scope = $project_scope")
        params["project_scope"] = project_scope
    return clauses, params


def _relationship_label(value: str) -> str:
    labels = {"related": "RELATED", "contradiction": "CONTRADICTION", "superseded_by": "SUPERSEDED_BY"}
    try:
        return labels[value]
    except KeyError as error:
        raise ValueError(f"Unsupported memory relationship type: {value}") from error


def _result_maps(result: Any) -> list[dict]:
    rows: list[dict] = []
    for row in result.result_set:
        value = row[0] if isinstance(row, (list, tuple)) else row
        properties = getattr(value, "properties", value)
        rows.append(dict(properties))
    return rows


def _read_maps(graph: Any, query: str, params: dict[str, Any]) -> list[dict]:
    """Return no rows for Falkor's not-yet-created named graph response."""
    try:
        return _result_maps(graph.ro_query(query, params))
    except Exception as error:
        if "empty key" in str(error).casefold():
            return []
        raise


def _read_rows(graph: Any, query: str, params: dict[str, Any]) -> list[list[Any]]:
    try:
        return [list(row) if isinstance(row, tuple) else row for row in graph.ro_query(query, params).result_set]
    except Exception as error:
        if "empty key" in str(error).casefold():
            return []
        raise


def _visibility_filters(*, alias: str, session_id: str, project_id: str | None,
                        project_scope: str | None) -> tuple[list[str], dict[str, Any]]:
    clauses = [f"({alias}.scope = 'session' AND {alias}.session_id = $session_id)", f"{alias}.scope = 'user'"]
    params: dict[str, Any] = {"session_id": session_id}
    if project_id:
        clauses.append(f"({alias}.scope = 'project' AND {alias}.project_id = $project_id)")
        params["project_id"] = project_id
    if project_scope:
        clauses.append(f"({alias}.scope = 'project' AND {alias}.project_id IS NULL AND {alias}.project_scope = $project_scope)")
        params["project_scope"] = project_scope
    return [f"({' OR '.join(clauses)})"], params


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
