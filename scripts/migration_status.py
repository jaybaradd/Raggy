"""Print recorded migration versions for Raggy's local SQLite databases."""
from __future__ import annotations
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))
from db.session_store import session_store
from core.storage.memory_store import memory_store
from core.storage.graph_store import graph_store
from core.storage.evidence_store import evidence_store
from db.migrations import MigrationRunner

def main() -> None:
    for component, store in (("sessions", session_store), ("memories", memory_store),
                             ("graph", graph_store), ("evidence", evidence_store)):
        with store._connect() as connection:  # operational inspection only
            print(component, MigrationRunner(component).status(connection))
if __name__ == "__main__": main()
