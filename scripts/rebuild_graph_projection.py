"""Rebuild only the derived memory graph from the authoritative repository.

Run while the application and other projection workers are stopped:
    python scripts/rebuild_graph_projection.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.memory.graph_rebuild import rebuild_graph_projection
from db.repository_factory import repositories


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the derived memory graph only")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    print(rebuild_graph_projection(
        store=repositories.memories, graph=repositories.graph, batch_size=args.batch_size,
    ))


if __name__ == "__main__":
    main()
