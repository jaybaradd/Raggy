"""Repository backend selection is explicit and safe."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from config import Settings
from db.repository_factory import Repositories, _LazyRepositories, create_repositories


class RepositoryFactoryTests(unittest.TestCase):
    def test_sqlite_is_the_default_backend(self) -> None:
        repositories = create_repositories(Settings())
        self.assertTrue(hasattr(repositories.sessions, "create_session"))
        self.assertTrue(hasattr(repositories.memories, "capture_event"))

    def test_postgres_requires_a_connection_url(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "POSTGRES_DATABASE_URL"):
            create_repositories(Settings(AUTHORITATIVE_DB_BACKEND="postgres", POSTGRES_DATABASE_URL=""))

    def test_postgres_constructs_all_authoritative_repositories_together(self) -> None:
        config = Settings(AUTHORITATIVE_DB_BACKEND="postgres", POSTGRES_DATABASE_URL="postgresql://example", POSTGRES_SCHEMA="test_schema")
        class Fake:
            def __init__(self, *args, **kwargs): self.args, self.kwargs = args, kwargs
        with patch("db.postgres_session_store.PostgresSessionRepository", Fake), \
             patch("db.postgres_memory_store.PostgresMemoryRepository", Fake), \
             patch("db.postgres_evidence_store.PostgresEvidenceRepository", Fake), \
             patch("core.storage.graph_store.GraphStore", Fake):
            repositories = create_repositories(config)
        self.assertIsInstance(repositories.sessions, Fake)
        self.assertIsInstance(repositories.memories, Fake)
        self.assertIsInstance(repositories.evidence, Fake)
        self.assertEqual(repositories.sessions.kwargs["schema"], "test_schema")

    def test_postgres_constructor_failure_does_not_fall_back_to_sqlite(self) -> None:
        config = Settings(AUTHORITATIVE_DB_BACKEND="postgres", POSTGRES_DATABASE_URL="postgresql://example")
        with patch("db.postgres_session_store.PostgresSessionRepository", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                create_repositories(config)

    def test_falkor_graph_is_selected_without_changing_authoritative_repositories(self) -> None:
        config = Settings(GRAPH_PROJECTION_BACKEND="falkor", FALKORDB_URL="redis://example:6380")

        class FakeGraph:
            def __init__(self, *args, **kwargs): self.kwargs = kwargs

        with patch("core.storage.falkor_graph_store.FalkorGraphStore", FakeGraph):
            repositories = create_repositories(config)

        self.assertIsInstance(repositories.graph, FakeGraph)
        self.assertEqual(repositories.graph.kwargs["url"], "redis://example:6380")

    def test_postgres_falkor_profile_constructs_no_sqlite_repositories(self) -> None:
        config = Settings(
            AUTHORITATIVE_DB_BACKEND="postgres",
            POSTGRES_DATABASE_URL="postgresql://example",
            POSTGRES_SCHEMA="test_schema",
            GRAPH_PROJECTION_BACKEND="falkor",
            FALKORDB_URL="redis://example:6380",
            FALKORDB_GRAPH_NAME="raggy_cutover_test",
        )

        class FakeAuthority:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        class FakeGraph:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

        with patch("db.postgres_session_store.PostgresSessionRepository", FakeAuthority), \
             patch("db.postgres_memory_store.PostgresMemoryRepository", FakeAuthority), \
             patch("db.postgres_evidence_store.PostgresEvidenceRepository", FakeAuthority), \
             patch("core.storage.falkor_graph_store.FalkorGraphStore", FakeGraph), \
             patch("core.storage.graph_store.GraphStore", side_effect=AssertionError("SQLite graph must not be used")):
            repositories = create_repositories(config)

        self.assertIsInstance(repositories.sessions, FakeAuthority)
        self.assertIsInstance(repositories.memories, FakeAuthority)
        self.assertIsInstance(repositories.evidence, FakeAuthority)
        self.assertIsInstance(repositories.graph, FakeGraph)
        self.assertEqual(repositories.graph.kwargs["graph_name"], "raggy_cutover_test")

    def test_repository_field_proxy_does_not_construct_backends_until_used(self) -> None:
        class FakeMemory:
            def list(self): return []

        lazy = _LazyRepositories()
        resolved = Repositories(sessions=object(), memories=FakeMemory(), evidence=object(), graph=object())
        with patch("db.repository_factory.create_repositories", return_value=resolved) as construct:
            memory = lazy.memories
            construct.assert_not_called()
            self.assertEqual(memory.list(), [])
            construct.assert_called_once()


if __name__ == "__main__":
    unittest.main()
