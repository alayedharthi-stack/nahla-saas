"""
customer_chat_models.py
───────────────────────
OpenAI-only customer conversation model slugs and escalation policy.

Manager-confirmed slugs (2026-07):
  default (cheap/tiny)  → gpt-5.6-luna
  standard escalation   → gpt-5.6-terra
  premium (gated)       → gpt-5.6-sol

Semantic tier routing is owned by model_router; technical failure escalation
within the same openai_compatible provider is owned by engine._call_with_chain.

Provisional pricing (USD / 1M tokens, documented placeholders until billing
confirms list rates — Terra ≈10× Luna, Sol ≈25× Luna per manager guidance):
  Luna  input $0.015  output $0.060
  Terra input $0.150  output $0.600
  Sol   input $0.375  output $1.500
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

_log = logging.getLogger("nahla.ai.customer_chat_models")

MODEL_LUNA = "gpt-5.6-luna"
MODEL_TERRA = "gpt-5.6-terra"
MODEL_SOL = "gpt-5.6-sol"

_CUSTOMER_CHAT_PROVIDER = "openai_compatible"

# Ordered semantic tiers (cheap → standard → premium).
_SEMANTIC_MODEL_BY_TIER = {
    "tiny": MODEL_LUNA,
    "cheap": MODEL_LUNA,
    "standard": MODEL_TERRA,
    "premium": MODEL_SOL,
}


def resolve_default_customer_chat_model() -> str:
    """Production default for customer chat (Luna)."""
    return (
        os.environ.get("NAHLA_MODEL_CHEAP", "").strip()
        or os.environ.get("OPENAI_MODEL", "").strip()
        or MODEL_LUNA
    )


def resolve_standard_customer_chat_model() -> str:
    return os.environ.get("NAHLA_MODEL_STANDARD", "").strip() or MODEL_TERRA


def resolve_premium_customer_chat_model() -> str:
    return os.environ.get("NAHLA_MODEL_PREMIUM", "").strip() or MODEL_SOL


def resolve_tiny_customer_chat_model() -> str:
    return os.environ.get("NAHLA_MODEL_TINY", "").strip() or MODEL_LUNA


def model_for_semantic_tier(tier: str) -> str:
    key = str(tier or "").strip().lower()
    if key == "premium":
        return resolve_premium_customer_chat_model()
    if key == "standard":
        return resolve_standard_customer_chat_model()
    if key in {"tiny", "cheap"}:
        return resolve_tiny_customer_chat_model() if key == "tiny" else resolve_default_customer_chat_model()
    return resolve_default_customer_chat_model()


def _is_premium_model_allowed() -> bool:
    return os.getenv("ALLOW_PREMIUM_MODEL", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def customer_chat_provider() -> str:
    return _CUSTOMER_CHAT_PROVIDER


def openai_only_provider_chain() -> tuple[str, ...]:
    """Customer conversation path — OpenAI-compatible only (no Anthropic/Gemini)."""
    return (_CUSTOMER_CHAT_PROVIDER,)


def technical_escalation_models(requested_model: str) -> List[str]:
    """
    Technical failure fallback chain (same provider, different model slug).

    Luna → Terra; Terra → Sol only when ALLOW_PREMIUM_MODEL=true.
  """
    model = str(requested_model or "").strip().lower()
    chain: List[str] = []
    luna = resolve_default_customer_chat_model().lower()
    terra = resolve_standard_customer_chat_model().lower()
    sol = resolve_premium_customer_chat_model().lower()

    if model == luna or model == MODEL_LUNA:
        chain.append(resolve_standard_customer_chat_model())
    elif model == terra or model == MODEL_TERRA:
        if _is_premium_model_allowed():
            chain.append(resolve_premium_customer_chat_model())
    return chain


def emit_customer_chat_model_telemetry(
    *,
    provider: str,
    requested_model: str,
    actual_model: str = "",
    escalation_reason: str = "",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Log provider/model routing metadata — no customer content."""
    try:
        payload: Dict[str, Any] = {
            "provider": provider or None,
            "requested_model": requested_model or None,
            "actual_model": actual_model or requested_model or None,
            "escalation_reason": escalation_reason or None,
            "tenant_id": tenant_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
        }
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})
        payload = {k: v for k, v in payload.items() if v is not None}
        _log.info("[CUSTOMER_CHAT_MODEL] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: silent-ok — telemetry must never break replies
        pass


__all__ = [
    "MODEL_LUNA",
    "MODEL_TERRA",
    "MODEL_SOL",
    "customer_chat_provider",
    "emit_customer_chat_model_telemetry",
    "model_for_semantic_tier",
    "openai_only_provider_chain",
    "resolve_default_customer_chat_model",
    "resolve_premium_customer_chat_model",
    "resolve_standard_customer_chat_model",
    "resolve_tiny_customer_chat_model",
    "technical_escalation_models",
]
