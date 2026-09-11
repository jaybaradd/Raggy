"""Open-world relationship normalization for graph-compatible memory edges."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal


RelationFamily = Literal[
    "association", "hierarchy", "part_whole", "possession", "participation",
    "dependency", "causal", "temporal", "spatial", "transformation",
    "provenance", "similarity", "contradiction", "other",
]


def normalize_label(value: str) -> str:
    """Return a conservative identity key; do not use fuzzy matching automatically."""
    text = unicodedata.normalize("NFKC", value).casefold().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text)


def normalize_predicate(value: str) -> str:
    """Keep an open predicate vocabulary while making predicates queryable."""
    text = normalize_label(value)
    return re.sub(r"\s+", "_", text).strip("_") or "related_to"


def relation_family(predicate: str) -> RelationFamily:
    """Classify a predicate broadly without discarding its precise wording."""
    value = normalize_predicate(predicate)
    tokens = set(value.split("_"))

    if tokens & {"contradicts", "contradict", "supersedes", "superseded"}:
        return "contradiction"
    if tokens & {"supports", "supported", "evidence", "cites", "sourced"}:
        return "provenance"
    if tokens & {"similar", "resembles", "equivalent", "alias"}:
        return "similarity"
    if tokens & {"transform", "transforms", "converted", "converts", "summarizes", "summarised"}:
        return "transformation"
    if tokens & {"causes", "cause", "leads", "results", "prevents"}:
        return "causal"
    if tokens & {"requires", "depends", "dependency", "prerequisite", "uses"}:
        return "dependency"
    if tokens & {"contains", "part", "component", "includes", "comprises"}:
        return "part_whole"
    if tokens & {"parent", "child", "subtask", "subcategory", "belongs"}:
        return "hierarchy"
    if tokens & {"owns", "has", "possesses", "belongs_to"}:
        return "possession"
    if tokens & {"attended", "participated", "contributed", "member"}:
        return "participation"
    if tokens & {"near", "inside", "located", "location", "between"}:
        return "spatial"
    if tokens & {"before", "after", "during", "until", "since", "valid"}:
        return "temporal"
    return "association" if value != "related_to" else "other"
