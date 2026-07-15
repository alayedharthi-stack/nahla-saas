"""Privacy-safe logging for conversation A1-subject bindings (no entity IDs / PII)."""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("nahla.conversation_a1_subject_binding")


def log_binding_write_event(
    *,
    event: str,
    tenant_id: int,
    outcome: str,
    binding_state: Optional[str] = None,
    subject_kind: Optional[str] = None,
    binding_source: Optional[str] = None,
    evidence_class: Optional[str] = None,
    provenance_kind: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    logger.info(
        "[A1 conversation binding] event=%s tenant_id=%s outcome=%s "
        "binding_state=%s subject_kind=%s binding_source=%s "
        "evidence_class=%s provenance_kind=%s reason=%s",
        event,
        tenant_id,
        outcome,
        binding_state or "-",
        subject_kind or "-",
        binding_source or "-",
        evidence_class or "-",
        provenance_kind or "-",
        reason or "-",
    )


__all__ = ["log_binding_write_event"]
