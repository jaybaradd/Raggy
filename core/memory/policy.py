"""Deterministic lifecycle policy for user-stated project memories.

The extractor proposes typed candidates. This module, rather than the model,
decides whether a candidate becomes durable project memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.memory.extractor import MemoryCandidate
from core.memory.event_types import is_known_event_type


EVENT_AUTO_PROMOTE_CONFIDENCE = 0.75
PROJECT_PREFERENCE_AUTO_PROMOTE_CONFIDENCE = 0.90
DEFAULT_EVENT_REVIEW_WINDOW = timedelta(days=30)
EVENT_GRACE_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class CaptureDecision:
    scope: str
    status: str
    user_confirmed: bool
    valid_to: datetime | None
    event_type: str
    reason: str


def decide_project_capture(*, candidate: MemoryCandidate, project_scope: str | None,
                           user_content: str, captured_at: datetime | None = None) -> CaptureDecision:
    """Apply safe, explainable automatic retention rules.

    Only user-originated content reaches this policy. Projects currently
    auto-promote concrete events and explicit project preferences; generic
    knowledge, entities, and solutions stay reviewable candidates until their
    dedicated policy and schemas are introduced.
    """
    if not project_scope:
        return _candidate("no_project_scope")

    now = captured_at or datetime.now(timezone.utc)
    if candidate.kind == "event" and not is_known_event_type(str(candidate.payload.get("event_type", ""))):
        return _candidate("unclassified_event_type")
    if candidate.kind == "event" and candidate.confidence >= EVENT_AUTO_PROMOTE_CONFIDENCE:
        return CaptureDecision(
            scope="project",
            status="active",
            user_confirmed=True,
            valid_to=event_valid_to(user_content, candidate.payload.get("temporal_scope"), now),
            event_type="auto_promoted_project_event",
            reason="high_confidence_user_stated_operational_event",
        )
    if (
        candidate.kind == "preference"
        and candidate.confidence >= PROJECT_PREFERENCE_AUTO_PROMOTE_CONFIDENCE
        and bool(candidate.payload.get("consent"))
    ):
        return CaptureDecision(
            scope="project",
            status="active",
            user_confirmed=True,
            valid_to=None,
            event_type="auto_promoted_project_preference",
            reason="high_confidence_user_stated_project_preference",
        )
    return _candidate("requires_review")


def event_valid_to(user_content: str, temporal_scope: object, captured_at: datetime) -> datetime:
    """Derive a conservative review/expiry date without trusting model dates.

    Relative durations are parsed from the original user text first. Events
    without a parseable duration stay useful for thirty days, then naturally
    fall out of retrieval through their validity metadata.
    """
    text = f"{user_content} {temporal_scope or ''}".casefold()
    if "tomorrow" in text:
        return captured_at + timedelta(days=1) + EVENT_GRACE_WINDOW

    match = re.search(r"\bin\s+(\d+)\s+(hour|day|week|month)s?\b", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        duration = {
            "hour": timedelta(hours=amount),
            "day": timedelta(days=amount),
            "week": timedelta(weeks=amount),
            "month": timedelta(days=30 * amount),
        }[unit]
        return captured_at + duration + EVENT_GRACE_WINDOW
    return captured_at + DEFAULT_EVENT_REVIEW_WINDOW


def _candidate(reason: str) -> CaptureDecision:
    return CaptureDecision(
        scope="session", status="candidate", user_confirmed=False,
        valid_to=None, event_type="candidate_extracted", reason=reason,
    )
