"""Check that a PostgreSQL cutover target is reachable and fully migrated.

This command is read-only: it never creates tables or changes application data.
"""
from __future__ import annotations
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))
from config import settings

EXPECTED = {"sessions": 2, "memories": 1, "evidence": 1}

def main() -> None:
    if not settings.postgres_database_url:
        raise SystemExit("POSTGRES_DATABASE_URL is required")
    try:
        import psycopg
        with psycopg.connect(settings.postgres_database_url, connect_timeout=5) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT component, MAX(version) FROM schema_migrations GROUP BY component")
            versions = dict(cursor.fetchall())
    except Exception as error:
        raise SystemExit(f"Postgres preflight failed: {error}") from error
    missing = {component: version for component, version in EXPECTED.items() if versions.get(component, 0) < version}
    if missing: raise SystemExit(f"Postgres preflight failed: missing migrations {missing}")
    print({"status": "ready", "components": {key: versions[key] for key in EXPECTED}, "backend": settings.authoritative_db_backend})

if __name__ == "__main__": main()
