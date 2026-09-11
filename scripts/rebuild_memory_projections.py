"""Rebuild Qdrant and graph projections from authoritative memory records.

Run while no other process is writing the local Qdrant directory:
    python scripts/rebuild_memory_projections.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.memory.projections import sync_pending_projections
from core.storage.memory_store import memory_store


def main() -> None:
    scheduled = memory_store.enqueue_all_projections()
    total = {"completed": 0, "failed": 0}
    while True:
        result = sync_pending_projections(store=memory_store, limit=100)
        total["completed"] += result["completed"]
        total["failed"] += result["failed"]
        if result["completed"] + result["failed"] == 0:
            break
    print(f"Scheduled {scheduled} memory records; completed={total['completed']} failed={total['failed']}")


if __name__ == "__main__":
    main()
