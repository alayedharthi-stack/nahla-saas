"""Privacy-safe telemetry for the conversation A1-subject read bridge."""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("nahla.conversation_a1_subject_read")


def log_subject_read_event(
    *,
    status: str,
    reason: Optional[str] = None,
    evidence_class: Optional[str] = None,
) -> None:
    """Emit closed classifications only: never tenant, conversation, or subject IDs."""
    logger.info(
        "[A1 conversation subject read] status=%s reason=%s evidence_class=%s",
        status,
        reason or "-",
        evidence_class or "-",
    )


__all__ = ["log_subject_read_event"]
