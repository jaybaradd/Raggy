"""Process durable evidence-to-Qdrant projection jobs."""
from __future__ import annotations
import argparse
from db.postgres_evidence_store import PostgresEvidenceRepository
from config import settings
from core.evidence.projections import sync_pending_evidence_projections

def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--limit", type=int, default=100); parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    store = PostgresEvidenceRepository(settings.postgres_database_url)
    if args.retry_failed: store.requeue_failed_projections()
    print(sync_pending_evidence_projections(store=store, limit=args.limit))
if __name__ == "__main__": main()
