"""Process existing memory projection jobs without scheduling a full rebuild.

Run while the app is stopped when using file-backed local Qdrant:
    python scripts/sync_pending_projections.py
    python scripts/sync_pending_projections.py --retry-failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.memory.projections import sync_pending_projections
from db.repository_factory import repositories


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize existing pending memory projections")
    parser.add_argument("--limit", type=int, default=100, help="Maximum jobs to process (default: 100)")
    parser.add_argument("--retry-failed", action="store_true", help="Requeue failed jobs before syncing")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 1_000:
        parser.error("--limit must be between 1 and 1000")
    memory_store = repositories.memories
    requeued = memory_store.requeue_failed_projections() if args.retry_failed else 0
    result = sync_pending_projections(store=memory_store, graph=repositories.graph, limit=args.limit)
    print({"requeued_failed": requeued, **result})


if __name__ == "__main__":
    main()
