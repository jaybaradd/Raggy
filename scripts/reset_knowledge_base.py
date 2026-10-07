"""Explicitly reset Raggy's evidence subsystem without touching conversations or memory."""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings


TABLES = (
    "evidence_projection_jobs",
    "ingestion_runs",
    "evidence_segments",
    "asset_bindings",
    "assets",
)
CONFIRMATION = "RESET_KNOWLEDGE_BASE"


def _qualified(name: str) -> str:
    if settings.postgres_schema:
        # Settings validation and repository construction already restrict the
        # schema to identifiers. Keep this script independently safe as well.
        import re
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings.postgres_schema):
            raise SystemExit("POSTGRES_SCHEMA is not a valid identifier")
        return f'"{settings.postgres_schema}"."{name}"'
    return f'"{name}"'


def _counts(connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f"SELECT COUNT(*) FROM {_qualified(table)}")
            counts[table] = int(cursor.fetchone()[0])
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="perform the reset; otherwise only print a preflight")
    parser.add_argument("--confirm", default="", help=f"must be exactly {CONFIRMATION}")
    parser.add_argument("--delete-raw-files", action="store_true")
    parser.add_argument("--delete-evidence-collections", action="store_true")
    args = parser.parse_args()

    if not settings.postgres_database_url:
        raise SystemExit("POSTGRES_DATABASE_URL is required")
    try:
        import psycopg
    except ImportError as error:
        raise SystemExit("psycopg is required") from error

    with psycopg.connect(settings.postgres_database_url) as connection:
        counts = _counts(connection)
        print("Knowledge-base reset preflight:")
        for table, count in counts.items():
            print(f"  {table}: {count}")
        print(f"  raw files: {'DELETE' if args.delete_raw_files else 'keep'} ({Path(settings.upload_dir).resolve()})")
        if args.delete_evidence_collections:
            print("  Qdrant evidence collections: DELETE configured v2 collections")
        if not args.execute:
            print(f"Dry run only. Re-run with --execute --confirm {CONFIRMATION}")
            return
        if args.confirm != CONFIRMATION:
            raise SystemExit(f"Refusing reset: --confirm must be exactly {CONFIRMATION}")
        with connection.cursor() as cursor:
            cursor.execute(
                "TRUNCATE TABLE " + ", ".join(_qualified(table) for table in TABLES)
                + " RESTART IDENTITY CASCADE"
            )
        connection.commit()

    if args.delete_raw_files:
        upload_dir = Path(settings.upload_dir).resolve()
        if upload_dir.exists():
            shutil.rmtree(upload_dir)
        upload_dir.mkdir(parents=True, exist_ok=True)
    if args.delete_evidence_collections:
        try:
            from qdrant_client import QdrantClient
        except ImportError as error:
            raise SystemExit("qdrant-client is required to delete evidence collections") from error
        if settings.qdrant_url == ":memory:":
            client = QdrantClient(":memory:")
        elif settings.qdrant_url.startswith(("http://", "https://")):
            client = QdrantClient(url=settings.qdrant_url)
        else:
            client = QdrantClient(path=settings.qdrant_url)
        existing = {item.name for item in client.get_collections().collections}
        evidence_collections = {
            settings.qdrant_collection, settings.qdrant_table_collection,
            settings.qdrant_image_collection, settings.qdrant_video_collection,
        }
        for collection in evidence_collections & existing:
            client.delete_collection(collection)
    print("Knowledge base reset complete. Conversation and memory data were not touched.")


if __name__ == "__main__":
    main()
