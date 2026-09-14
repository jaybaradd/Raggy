"""Lossless normalization for descriptive event labels.

Event labels are useful retrieval and display metadata, but they are not a
closed taxonomy and must not decide whether an operational memory is valid.
"""

from __future__ import annotations

import re


def normalize_event_label(value: str) -> str:
    """Return a stable, presentation-neutral slug without semantic aliases."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip("_")


def canonical_event_type(value: str) -> str:
    """Compatibility name for callers awaiting the reconciliation slice."""
    return normalize_event_label(value)
