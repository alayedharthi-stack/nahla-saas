"""Structured audit when a non-empty brain candidate is dropped before send."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger("nahla.outbound_abort_audit")


def log_outbound_candidate_abort(
    *,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    customer_id: Optional[int] = None,
    inbound_message_event_id: Optional[int] = None,
    generated_candidate_non_empty: bool,
    final_response_empty: bool,
    abort_reason: str,
    final_stage: str,
    suppressor: Optional[str] = None,
    expression_owner: Optional[str] = None,
    candidate_preview: Optional[str] = None,
) -> None:
    """Emit one greppable line when brain produced text that never reached send."""
    if not generated_candidate_non_empty or not final_response_empty:
        return
    payload: dict[str, Any] = {
        "event": "outbound_candidate_abort",
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "customer_id": customer_id,
        "inbound_message_event_id": inbound_message_event_id,
        "generated_candidate_non_empty": True,
        "final_response_empty": True,
        "abort_reason": abort_reason,
        "final_stage": final_stage,
        "suppressor": suppressor,
        "expression_owner": expression_owner,
        "candidate_preview": (candidate_preview or "")[:120] or None,
    }
    logger.info("[OUTBOUND_CANDIDATE_ABORT] %s", json.dumps(payload, ensure_ascii=False))
