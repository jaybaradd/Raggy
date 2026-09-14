"""Select authoritative repository implementations from one configuration seam."""
from __future__ import annotations

from dataclasses import dataclass

from config import Settings, settings
from db.repositories import EvidenceRepository, GraphRepository, MemoryRepository, SessionRepository


@dataclass(frozen=True)
class Repositories:
    sessions: SessionRepository
    memories: MemoryRepository
    evidence: EvidenceRepository
    graph: GraphRepository


def create_repositories(config: Settings = settings) -> Repositories:
    if config.authoritative_db_backend == "postgres":
        if not config.postgres_database_url:
            raise RuntimeError("POSTGRES_DATABASE_URL is required when AUTHORITATIVE_DB_BACKEND=postgres")
        raise RuntimeError("Postgres repositories are not implemented yet; use sqlite for this release")
    if config.authoritative_db_backend != "sqlite":
        raise RuntimeError(f"Unsupported authoritative database backend: {config.authoritative_db_backend}")
    # Imports remain here so application callers do not bind to SQLite classes.
    from db.session_store import session_store
    from core.storage.memory_store import memory_store
    from core.storage.evidence_store import evidence_store
    from core.storage.graph_store import graph_store
    return Repositories(session_store, memory_store, evidence_store, graph_store)


repositories = create_repositories()
