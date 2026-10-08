"""Migration runner guarantees ordered, atomic, idempotent schema changes."""
from __future__ import annotations

import sqlite3
import unittest

from db.migrations import Migration, MigrationRunner


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row

    def tearDown(self) -> None:
        self.connection.close()

    def test_migrations_apply_once_in_order(self) -> None:
        runner = MigrationRunner("test")
        migrations = [
            Migration(1, "create", lambda db: db.execute("CREATE TABLE records (value TEXT)")),
            Migration(2, "seed", lambda db: db.execute("INSERT INTO records VALUES ('ok')")),
        ]
        self.assertEqual(runner.apply(self.connection, migrations), [1, 2])
        self.assertEqual(runner.apply(self.connection, migrations), [])
        self.assertEqual(self.connection.execute("SELECT value FROM records").fetchone()["value"], "ok")
        self.assertEqual([row["version"] for row in runner.status(self.connection)], [1, 2])

    def test_failed_migration_rolls_back_and_is_not_recorded(self) -> None:
        runner = MigrationRunner("test")
        def fail(db):
            db.execute("CREATE TABLE transient_data (value TEXT)")
            raise RuntimeError("stop")
        with self.assertRaisesRegex(RuntimeError, "stop"):
            runner.apply(self.connection, [Migration(1, "broken", fail)])
        self.assertIsNone(self.connection.execute("SELECT name FROM sqlite_master WHERE name = 'transient_data'").fetchone())
        self.assertEqual(runner.status(self.connection), [])


if __name__ == "__main__":
    unittest.main()
