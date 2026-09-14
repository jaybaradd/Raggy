"""Repository backend selection is explicit and safe."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from config import Settings
from db.repository_factory import create_repositories


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


if __name__ == "__main__":
    unittest.main()
