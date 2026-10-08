"""Explicit, backend-neutral rebuilds for the derived memory graph."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.memory.projections import sync_pending_projections

if TYPE_CHECKING:
    from core.storage.memory_store import MemoryStore
    from db.repositories import GraphRepository


def rebuild_graph_projection(*, store: "MemoryStore", graph: "GraphRepository",
                             batch_size: int = 100) -> dict[str, int]:
    """Replace the graph projection from authoritative records only.

    The caller should stop other projection workers while this operational
    repair runs. Vector jobs are deliberately neither created nor consumed.
    """
    if not 1 <= batch_size <= 1_000:
        raise ValueError("batch_size must be between 1 and 1000")
    graph.reset_projection()
    scheduled = store.enqueue_all_projections(targets=("graph",))
    completed = failed = 0
    while True:
        result = sync_pending_projections(
            store=store, graph=graph, limit=batch_size, targets={"graph"},
        )
        completed += result["completed"]
        failed += result["failed"]
        if result["completed"] + result["failed"] == 0:
            break
    return {"scheduled": scheduled, "completed": completed, "failed": failed}
