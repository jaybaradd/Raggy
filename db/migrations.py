"""Transactional, versioned SQLite schema migrations."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]

class MigrationRunner:
    def __init__(self, component: str) -> None:
        self.component = component

    def apply(self, connection: sqlite3.Connection, migrations: list[Migration]) -> list[int]:
        connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            component TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
            applied_at TEXT NOT NULL, PRIMARY KEY(component, version))""")
        versions = [item.version for item in migrations]
        if versions != sorted(versions) or len(versions) != len(set(versions)):
            raise ValueError("migrations must have unique ascending versions")
        applied = {row["version"] for row in connection.execute(
            "SELECT version FROM schema_migrations WHERE component = ?", (self.component,))}
        completed = []
        for migration in migrations:
            if migration.version in applied:
                continue
            savepoint = f"migration_{migration.version}"
            nested = connection.in_transaction
            try:
                if nested:
                    connection.execute(f"SAVEPOINT {savepoint}")
                else:
                    connection.execute("BEGIN IMMEDIATE")
                migration.apply(connection)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                                   (self.component, migration.version, migration.name,
                                    datetime.now(timezone.utc).isoformat()))
                if nested:
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    connection.commit()
            except Exception:
                if nested:
                    connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    connection.rollback()
                raise
            completed.append(migration.version)
        return completed

    def status(self, connection: sqlite3.Connection) -> list[dict]:
        return [dict(row) for row in connection.execute(
            "SELECT component, version, name, applied_at FROM schema_migrations WHERE component = ? ORDER BY version",
            (self.component,))]
