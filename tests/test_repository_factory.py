"""Repository backend selection is explicit and safe."""
from __future__ import annotations

import unittest

from config import Settings
from db.repository_factory import create_repositories


class RepositoryFactoryTests(unittest.TestCase):
    def test_sqlite_is_the_default_backend(self) -> None:
        repositories = create_repositories(Settings())
        self.assertTrue(hasattr(repositories.sessions, "create_session"))
        self.assertTrue(hasattr(repositories.memories, "capture_event"))

    def test_postgres_requires_a_connection_url(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "POSTGRES_DATABASE_URL"):
            create_repositories(Settings(authoritative_db_backend="postgres", postgres_database_url=""))

    def test_postgres_is_not_silently_mapped_to_sqlite(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not implemented"):
            create_repositories(Settings(authoritative_db_backend="postgres", postgres_database_url="postgresql://example"))


if __name__ == "__main__":
    unittest.main()
