"""
intent_priority/telemetry.py
────────────────────────────
Structured logging for Customer Intent Priority Layer.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .types import IntentPriorityVerdict

logger = logging.getLogger("nahla.brain.intent_priority")


def log_intent_priority_verdict(
    *,
    tenant_id: Optional[int],
    verdict: IntentPriorityVerdict,
    preview: str = "",
    intent_name: str = "",
) -> None:
    """Emit a greppable flight-recorder line (no sensitive data beyond preview)."""
    try:
        payload = {
            "tenant_id": tenant_id,
            "intent": intent_name or "-",
            "preview": (preview or "")[:80],
            "intent_priority": verdict.to_trace_dict(),
        }
        logger.info(
            "[INTENT_PRIORITY] %s",
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = ["log_intent_priority_verdict"]
