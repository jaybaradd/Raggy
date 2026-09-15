"""Select authoritative repository implementations from one configuration seam."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

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
        try:
            from db.postgres_session_store import PostgresSessionRepository
            from db.postgres_memory_store import PostgresMemoryRepository
            from db.postgres_evidence_store import PostgresEvidenceRepository
            # Construct every authoritative repository before returning any of
            # them: a failed constructor aborts startup and cannot fall back.
            sessions = PostgresSessionRepository(config.postgres_database_url, schema=config.postgres_schema)
            memories = PostgresMemoryRepository(config.postgres_database_url, schema=config.postgres_schema)
            evidence = PostgresEvidenceRepository(config.postgres_database_url, schema=config.postgres_schema)
            graph = _create_graph_repository(config)
        except Exception as error:
            raise RuntimeError("Postgres authoritative repository initialization failed") from error
        return Repositories(sessions, memories, evidence, graph)
    if config.authoritative_db_backend != "sqlite":
        raise RuntimeError(f"Unsupported authoritative database backend: {config.authoritative_db_backend}")
    # Imports remain here so application callers do not bind to SQLite classes.
    from db.session_store import session_store
    from core.storage.memory_store import memory_store
    from core.storage.evidence_store import evidence_store
    return Repositories(session_store, memory_store, evidence_store, _create_graph_repository(config))


def _create_graph_repository(config: Settings) -> GraphRepository:
    if config.graph_projection_backend == "sqlite":
        from core.storage.graph_store import GraphStore
        return GraphStore(config.graph_db_path)
    if config.graph_projection_backend == "falkor":
        from core.storage.falkor_graph_store import FalkorGraphStore
        return FalkorGraphStore(url=config.falkordb_url, graph_name=config.falkordb_graph_name)
    raise RuntimeError(f"Unsupported graph projection backend: {config.graph_projection_backend}")


class _LazyRepositories:
    """Create repositories at first use, not while importing API modules.

    This makes backend readiness a startup/request concern and keeps utility
    imports and focused tests independent from an optional Falkor service.
    """

    def __init__(self) -> None:
        self._value: Repositories | None = None
        self._lock = Lock()

    def get(self) -> Repositories:
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = create_repositories()
        return self._value

    def __getattr__(self, name: str) -> Any:
        if name in {"sessions", "memories", "evidence", "graph"}:
            return _RepositoryProxy(self, name)
        return getattr(self.get(), name)


class _RepositoryProxy:
    """Delay a repository method lookup until the caller actually uses it."""

    def __init__(self, provider: _LazyRepositories, name: str) -> None:
        self._provider = provider
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        return getattr(getattr(self._provider.get(), self._name), attribute)


repositories = _LazyRepositories()
