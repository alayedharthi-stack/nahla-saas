"""
brain/pre_commerce_gate.py
──────────────────────────
Pre-commerce gate — skip catalog / product hydration for social turns.

Production logs showed correct ``[SOCIAL_ROUTE]`` / ``[NON_COMMERCE_ROUTE]``
but ``[CATALOG SEARCH]`` still ran on Eid dua text because
``build_merchant_context(product_query=message)`` executed before the
decision engine chose ``ACTION_SOCIAL_REPLY``.

Invariant: when intent is social OR non-commerce block is active (above
confidence threshold), the pipeline MUST NOT preload:

  * catalog search / top_products
  * merchant_context product lists
  * commerce facts top_products query
  * sales-context best_sellers
  * recommendation-breadth / browse pools (downstream)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from .intent.non_commerce_classifier import NonCommerceMatch
from .types import CommerceFacts, INTENT_GREETING, INTENT_PERSONA_INTERACTION, INTENT_SOCIAL, INTENT_WHO_ARE_YOU, Intent

logger = logging.getLogger("nahla.brain.pre_commerce_gate")

_DEFAULT_MIN_CONFIDENCE = 0.82


def pre_commerce_gate_min_confidence() -> float:
    try:
        return float(
            (os.getenv("PRE_COMMERCE_SOCIAL_MIN_CONFIDENCE") or str(_DEFAULT_MIN_CONFIDENCE)).strip()
        )
    except (TypeError, ValueError):
        return _DEFAULT_MIN_CONFIDENCE


def should_pre_commerce_shortcut(
    intent: Intent,
    nc_match: Optional[NonCommerceMatch],
    *,
    min_confidence: Optional[float] = None,
    message: str = "",
    state: Any = None,
    social_human_context: Any = None,
) -> bool:
    """True when this turn must bypass commerce preload entirely."""
    threshold = (
        float(min_confidence)
        if min_confidence is not None
        else pre_commerce_gate_min_confidence()
    )
    conf = float(getattr(intent, "confidence", 0) or 0)
    slots = getattr(intent, "slots", None) or {}

    if social_human_context is not None and getattr(
        social_human_context, "suppress_greeting_fast_path", False
    ):
        return False

    if nc_match is not None and nc_match.block_commerce:
        if float(nc_match.confidence or 0) >= threshold:
            return True

    if slots.get("block_commerce_escalation") and conf >= threshold:
        return True

    if intent.name == INTENT_SOCIAL and conf >= threshold:
        return True

    if intent.name == INTENT_WHO_ARE_YOU and conf >= threshold:
        return True

    if intent.name == INTENT_PERSONA_INTERACTION and conf >= threshold:
        return True

    if intent.name == INTENT_GREETING and conf >= threshold:
        if slots.get("embedded_greeting"):
            return False
        try:
            from .decision.engine import _first_turn_has_actionable_substance  # noqa: PLC0415

            # Pure greetings must skip catalog preload regardless of whether
            # compose uses LLM or legacy templates (Doctrine: gate ≠ wording).
            if not _first_turn_has_actionable_substance(message):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PRE_COMMERCE_GATE_ERROR] failed to evaluate routine greeting gate: %s",
                type(exc).__name__,
            )

    if message and state is not None:
        try:
            from .commerce.conversational_priority import (  # noqa: PLC0415
                absence_of_positive_commerce_signal,
            )
            from .types import INTENT_GENERAL, INTENT_HESITATION  # noqa: PLC0415

            if intent.name in {INTENT_GENERAL, INTENT_HESITATION}:
                if absence_of_positive_commerce_signal(
                    message,
                    intent_name=intent.name,
                    intent_confidence=conf,
                    state=state,
                    nc_match=nc_match,
                ):
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PRE_COMMERCE_GATE_ERROR] failed to evaluate commerce signal gate: %s",
                type(exc).__name__,
            )

    return False


def resolve_shortcut_category(
    intent: Intent,
    nc_match: Optional[NonCommerceMatch],
) -> str:
    if nc_match is not None:
        return str(nc_match.category or nc_match.social_category or "religious_media")
    slots = getattr(intent, "slots", None) or {}
    return str(slots.get("social_category") or "general_courtesy")


def log_pre_commerce_shortcut(
    *,
    tenant_id: Any,
    intent: Intent,
    nc_match: Optional[NonCommerceMatch],
) -> None:
    try:
        logger.info(
            "[PRE_COMMERCE_GATE] tenant=%s shortcut=1 intent=%s conf=%.2f "
            "nc_category=%s social_category=%s preview=%r",
            tenant_id,
            getattr(intent, "name", "?"),
            float(getattr(intent, "confidence", 0) or 0),
            getattr(nc_match, "category", None) if nc_match else "",
            str((getattr(intent, "slots", None) or {}).get("social_category") or ""),
            (getattr(intent, "raw_message", None) or "")[:80],
        )
    except Exception:
        pass


def load_minimal_commerce_facts(db: Any, tenant_id: int) -> CommerceFacts:
    """Store label only — no product counts or top_products preload."""
    facts = CommerceFacts()
    try:
        from database.models import TenantSettings  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if ts:
            ss = dict(ts.store_settings or {})
            facts.store_name = str(ss.get("store_name") or "").strip()
    except Exception as exc:
        logger.debug(
            "[PRE_COMMERCE_GATE] minimal facts load skipped tenant=%s: %s",
            tenant_id,
            exc,
        )
    return facts


def load_minimal_ai_settings(db: Any, tenant_id: int) -> dict:
    """Tenant tone/settings for optional social LLM — no catalog block."""
    try:
        from models import TenantSettings  # noqa: PLC0415
        from core.tenant import merge_ai_defaults  # noqa: PLC0415

        ts = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if ts:
            return dict(merge_ai_defaults(ts.ai_settings) or {})
    except Exception as exc:
        logger.debug(
            "[PRE_COMMERCE_GATE] ai_settings load skipped tenant=%s: %s",
            tenant_id,
            exc,
        )
    return {}


__all__ = [
    "load_minimal_ai_settings",
    "load_minimal_commerce_facts",
    "log_pre_commerce_shortcut",
    "pre_commerce_gate_min_confidence",
    "resolve_shortcut_category",
    "should_pre_commerce_shortcut",
]
