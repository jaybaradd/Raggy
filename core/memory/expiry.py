"""Periodic lifecycle worker for time-bound durable memories."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from config import settings
from core.memory.projections import sync_pending_projections
from core.storage.memory_store import MemoryStore
from db.repositories import GraphRepository

logger = logging.getLogger(__name__)


async def run_expiry_sweep(*, store: MemoryStore, graph: GraphRepository) -> int:
    """Expire due records, then synchronize their derived projections."""
    expired = await asyncio.to_thread(store.expire_due)
    if expired:
        result = await asyncio.to_thread(sync_pending_projections, store=store, graph=graph)
        logger.info("Expiry sweep marked %d memories expired; projections=%s", len(expired), result)
    return len(expired)


async def expiry_sweep_loop(*, run_once: Callable[[], Awaitable[int]]) -> None:
    """Run until cancelled; individual failures never stop later sweeps."""
    while True:
        await asyncio.sleep(settings.memory_expiry_sweep_interval_seconds)
        try:
            await run_once()
        except Exception:
            logger.exception("Scheduled expiry sweep failed")
