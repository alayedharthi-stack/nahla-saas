"""
model_router_decision.py
────────────────────────
Safe compose-time model router telemetry — no customer text or KB content.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

_log = logging.getLogger("nahla.ai.brain.cost.model_router")


def log_model_router_decision(
    *,
    tenant_id: Optional[int] = None,
    intent: str = "",
    selected_tier: str = "",
    selected_provider: str = "",
    selected_model: str = "",
    provider_hint: str = "",
    requested_model: str = "",
    actual_model: str = "",
    escalation_reason: str = "",
    fallback_used: bool = False,
    reason_code: str = "",
    commerce_slim_applied: bool = False,
    prompt_chars: int = 0,
    state_topic_shift: bool = False,
    checkout_relevant: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit one ``[MODEL_ROUTER_DECISION]`` line; never raises."""
    try:
        payload: Dict[str, Any] = {
            "tenant_id": tenant_id,
            "intent": (intent or "").strip().lower() or None,
            "selected_tier": selected_tier or None,
            "selected_provider": selected_provider or None,
            "selected_model": selected_model or None,
            "provider_hint": provider_hint or None,
            "requested_model": requested_model or selected_model or None,
            "actual_model": actual_model or selected_model or None,
            "escalation_reason": escalation_reason or None,
            "fallback_used": bool(fallback_used),
            "reason_code": reason_code or None,
            "commerce_slim_applied": bool(commerce_slim_applied),
            "prompt_chars": int(prompt_chars or 0),
            "state_topic_shift": bool(state_topic_shift),
            "checkout_relevant": bool(checkout_relevant),
        }
        if extra:
            for key, value in extra.items():
                if value is not None:
                    payload[key] = value
        payload = {k: v for k, v in payload.items() if v is not None}
        _log.info("[MODEL_ROUTER_DECISION] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: silent-ok — audit must never break compose
        pass


__all__ = ["log_model_router_decision"]
