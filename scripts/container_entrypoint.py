"""Wait for Compose dependencies, then run Raggy's normal entrypoint."""

from __future__ import annotations

import os
import socket
import sys
import time


DEPENDENCIES = (("postgres", 5432), ("qdrant", 6333), ("falkordb", 6379))
TIMEOUT_SECONDS = 90


def _wait_for(host: str, port: int) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f"Dependency ready: {host}:{port}", flush=True)
                return
        except OSError:
            time.sleep(1)
    raise SystemExit(f"Timed out waiting for {host}:{port} after {TIMEOUT_SECONDS} seconds")


def main() -> None:
    for host, port in DEPENDENCIES:
        _wait_for(host, port)
    os.execvp(sys.executable, [sys.executable, "main.py"])


if __name__ == "__main__":
    main()
