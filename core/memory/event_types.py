"""Canonical event-type taxonomy used by lifecycle and identity decisions."""

from __future__ import annotations

import re


_ALIASES = {
    "shipment": "shipment_arrival",
    "shipment_arrival": "shipment_arrival",
    "arrival_of_shipment": "shipment_arrival",
    "cargo_arrival": "shipment_arrival",
    "delivery_arrival": "shipment_arrival",
    "shipment_departure": "shipment_departure",
    "shipment_dispatch": "shipment_departure",
    "delivery": "delivery",
    "deadline": "deadline",
    "meeting": "meeting",
    "incident": "incident",
    "reservation": "reservation",
}


def canonical_event_type(value: str) -> str:
    """Map spelling and known aliases to a stable snake-case event type."""
    slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.casefold())).strip("_")
    return _ALIASES.get(slug, slug)


def is_known_event_type(value: str) -> bool:
    return canonical_event_type(value) in set(_ALIASES.values())
