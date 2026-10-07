"""Small, stable JSON-lines latency logger for end-to-end tracing."""

import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from config import settings

logger = logging.getLogger("raggy.latency")
logger.setLevel(logging.INFO)
_handler_lock = threading.Lock()
_file_handler_ready = False


def now_ns() -> int:
    """Return a monotonic timestamp suitable only for elapsed-time measurement."""
    return time.perf_counter_ns()


def elapsed_ms(started_ns: int) -> float:
    return round((time.perf_counter_ns() - started_ns) / 1_000_000, 3)


def log_latency(
    pipeline: str,
    stage: str,
    duration_ms: float,
    *,
    outcome: str = "ok",
    **fields: Any,
) -> None:
    """Emit one deterministic LATENCY JSON object to stdout and optional JSONL."""
    _ensure_file_handler()
    payload = {
        "duration_ms": round(float(duration_ms), 3),
        "outcome": outcome,
        "pipeline": pipeline,
        "stage": stage,
        **{key: value for key, value in fields.items() if value is not None},
    }
    logger.info("%s", json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


@contextmanager
def latency_span(pipeline: str, stage: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a synchronous stage; yielded fields may be enriched before exit."""
    started = now_ns()
    result_fields = dict(fields)
    try:
        yield result_fields
    except Exception as exc:
        log_latency(
            pipeline, stage, elapsed_ms(started), outcome="error",
            error_type=type(exc).__name__, **result_fields,
        )
        raise
    else:
        log_latency(pipeline, stage, elapsed_ms(started), **result_fields)


def _ensure_file_handler() -> None:
    global _file_handler_ready
    if _file_handler_ready or not settings.latency_log_path:
        return
    with _handler_lock:
        if _file_handler_ready:
            return
        try:
            path = Path(settings.latency_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        except OSError:
            logging.getLogger(__name__).exception(
                "Could not open latency log file %s; latency events remain on stdout",
                settings.latency_log_path,
            )
        finally:
            _file_handler_ready = True
