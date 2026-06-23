"""
routing/layer0_router.py
────────────────────────
Phase A — deterministic pre-Brain router for zero-LLM social / FAQ turns.

Handles: greetings, thanks, store links, working hours, goodbye.
Commerce-mixed messages always yield ``None`` so Brain remains authoritative.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    INTENT_ASK_STORE_INFO,
    INTENT_ASK_WORKING_HOURS,
    INTENT_FAREWELL,
    INTENT_GREETING,
    INTENT_SOCIAL,
    Intent,
    MerchantConversationState,
)

logger = logging.getLogger("nahla.routing.layer0")

_FLAG = "LAYER0_ROUTER_ENABLED"
_MIN_RULE_CONFIDENCE = 0.85

_LAYER0_INTENTS = frozenset({
    INTENT_GREETING,
    INTENT_SOCIAL,
    INTENT_ASK_STORE_INFO,
    INTENT_ASK_WORKING_HOURS,
    INTENT_FAREWELL,
})

# Product / order / price stems — blocks Layer 0 when present alongside FAQ asks.
# Deliberately excludes bare «رابط» so pure store-link requests stay on L0.
_FAQ_MIXED_COMMERCE_RE = re.compile(
    r"(?:"
    r"سعر|تكلف|ثمن|بكم|كم\s+سعر|"
    r"طلب|اطلب|اشتري|شراء|"
    r"اب(?:ي|غ(?:ى|y|a)?)\s+اطلب|بغ(?:يت|ى)\s+اطلب|"
    r"اب(?:ي|غ(?:ى|y|a)?)|اريد|اود|ودي|بغيت|بدي|"
    r"منتج|بضاع|سلع|صنف|عسل|"
    r"عند(?:كم|ك)\s+\S|لديك(?:م|)?\s+\S|"
    r"do\s+you\s+have|\bproduct\b|\bbuy\b|\border\b"
    r")",
    re.IGNORECASE | re.UNICODE,
)


@dataclass(frozen=True)
class Layer0RouteDecision:
    matched: str
    reply_text: str
    intent_name: str = ""
    social_category: str = ""


def layer0_router_enabled() -> bool:
    return os.getenv(_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _faq_mixed_commerce_blocks(message: str) -> bool:
    from modules.ai.brain.intent.rules import _normalize_residue_text  # noqa: PLC0415

    norm = _normalize_residue_text(message or "")
    if not norm:
        return False
    return _FAQ_MIXED_COMMERCE_RE.search(norm) is not None


def _social_turn_blocks_layer0(message: str) -> bool:
    """Mirror social_classifier disqualifiers — defer mixed turns to Brain."""
    from modules.ai.brain.intent.social_classifier import (  # noqa: PLC0415
        _has_closing_signal,
        _has_commercial_signal,
        _has_practical_question_signal,
        _has_relational_non_social_signal,
        _norm,
    )

    norm = _norm(message or "")
    if not norm:
        return True
    if _has_commercial_signal(norm):
        return True
    if _has_relational_non_social_signal(norm):
        return True
    if _has_practical_question_signal(norm):
        return True
    # Closing-only turns are handled by INTENT_FAREWELL rules, not social.
    if _has_closing_signal(norm):
        return True
    return False


def _greeting_blocks_layer0(message: str) -> bool:
    from modules.ai.brain.intent.rules import (  # noqa: PLC0415
        is_pure_greeting_without_commerce,
    )

    return not is_pure_greeting_without_commerce(message)


def _intent_blocks_layer0(message: str, intent: Intent) -> bool:
    name = str(intent.name or "").strip()
    if name == INTENT_GREETING:
        return _greeting_blocks_layer0(message)
    if name == INTENT_SOCIAL:
        return _social_turn_blocks_layer0(message)
    if name in {INTENT_ASK_STORE_INFO, INTENT_ASK_WORKING_HOURS, INTENT_FAREWELL}:
        return _faq_mixed_commerce_blocks(message)
    if intent.slots.get("embedded_greeting"):
        return True
    return False


def _load_layer0_facts(db: Any, tenant_id: int) -> CommerceFacts:
    """Lightweight tenant facts for Layer 0 templates — no catalog preload."""
    from database.models import StoreKnowledgeSnapshot, TenantSettings  # noqa: PLC0415
    from core.store_display import clean_store_name  # noqa: PLC0415

    facts = CommerceFacts()
    try:
        snapshot = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
            .first()
        )
        if snapshot:
            profile = snapshot.store_profile or {}
            policy = snapshot.policy_summary or {}
            facts.store_name = clean_store_name(profile.get("store_name", "") or "")
            facts.store_url = str(profile.get("store_url") or "").strip()
            facts.store_description = str(profile.get("description") or "").strip()
            facts.support_hours = str(policy.get("support_hours") or "").strip()

        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if settings:
            store_cfg = dict(settings.store_settings or {})
            ai_cfg = dict(settings.ai_settings or {})
            if not facts.store_name:
                facts.store_name = clean_store_name(store_cfg.get("store_name") or "")
            if not facts.store_url:
                facts.store_url = str(store_cfg.get("store_url") or "").strip()
            if not facts.support_hours:
                facts.support_hours = str(
                    store_cfg.get("working_hours")
                    or store_cfg.get("support_hours")
                    or ai_cfg.get("working_hours")
                    or ""
                ).strip()
            assistant = str(ai_cfg.get("assistant_name") or "").strip()
            if assistant:
                facts.assistant_name = assistant
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[LAYER0_ROUTER] facts load skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )
    return facts


def _load_layer0_state(
    db: Any,
    tenant_id: int,
    customer_phone: str,
) -> MerchantConversationState:
    try:
        from modules.ai.brain.state.store import DefaultStateStore  # noqa: PLC0415

        return DefaultStateStore().load(db, tenant_id, customer_phone)
    except Exception:
        return MerchantConversationState()


def _build_context(
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    history: List[Dict[str, Any]],
    intent: Intent,
    facts: CommerceFacts,
    state: MerchantConversationState,
    conversation_id: Optional[int],
) -> BrainContext:
    return BrainContext(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=message,
        intent=intent,
        state=state,
        facts=facts,
        history=list(history or []),
        conversation_id=conversation_id,
    )


def _compose_greeting(ctx: BrainContext) -> str:
    from modules.ai.brain.compose.greeting_etiquette import (  # noqa: PLC0415
        apply_greeting_etiquette,
        customer_message_for_etiquette,
    )
    from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
        pick_persona_greeting,
    )

    re_greet = bool(getattr(ctx.state, "greeted", False))
    text = pick_persona_greeting(ctx, re_greet=re_greet)
    return apply_greeting_etiquette(
        text,
        customer_message_for_etiquette(ctx),
        ctx.state,
        tenant_id=getattr(ctx, "tenant_id", None),
    )


def _compose_thanks(ctx: BrainContext, *, social_category: str) -> str:
    from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
        pick_persona_social_reply,
    )

    return pick_persona_social_reply(
        ctx,
        social_category,
        inbound_text=(ctx.message or ""),
    )


def _compose_store_link(facts: CommerceFacts) -> str:
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    return T.faq_store_info(
        store_name=facts.store_name,
        store_url=facts.store_url,
        store_description=facts.store_description,
    )


def _compose_working_hours(facts: CommerceFacts) -> str:
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    return T.faq_working_hours(
        support_hours=facts.support_hours,
        store_name=facts.store_name,
    )


def _compose_farewell(ctx: BrainContext) -> str:
    from modules.ai.brain.compose.persona_template_engine import (  # noqa: PLC0415
        pick_persona_farewell,
    )

    return pick_persona_farewell(ctx)


def evaluate_layer0_route(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
    history: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[int] = None,
) -> Optional[Layer0RouteDecision]:
    """
    Return a deterministic Layer 0 reply, or ``None`` to continue to Brain.

    Never calls LLM, slot extractor, or ``MerchantBrain.process()``.
    """
    if not layer0_router_enabled():
        return None

    text = (message or "").strip()
    if not text:
        return None

    try:
        from modules.ai.brain.postprocess.social_single_reply_guard import (  # noqa: PLC0415
            should_defer_layer0_for_brain_social,
        )

        if should_defer_layer0_for_brain_social(text):
            logger.info(
                "[LAYER0_ROUTER] defer=brain_social tenant=%s preview=%r",
                tenant_id,
                text[:80],
            )
            return None
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[LAYER0_ROUTER] brain_social defer check failed: %s",
            exc,
        )

    from modules.ai.brain.intent import rules  # noqa: PLC0415

    intent = rules.match(text)
    if intent is None:
        return None
    if intent.name not in _LAYER0_INTENTS:
        return None
    if float(intent.confidence or 0) < _MIN_RULE_CONFIDENCE:
        return None
    if _intent_blocks_layer0(text, intent):
        return None

    facts = _load_layer0_facts(db, tenant_id)
    state = _load_layer0_state(db, tenant_id, customer_phone)
    ctx = _build_context(
        tenant_id=tenant_id,
        customer_phone=customer_phone,
        message=text,
        history=history or [],
        intent=intent,
        facts=facts,
        state=state,
        conversation_id=conversation_id,
    )

    matched = str(intent.name)
    social_category = str((intent.slots or {}).get("social_category") or "")

    if intent.name == INTENT_GREETING:
        reply = _compose_greeting(ctx)
        matched = "greeting"
    elif intent.name == INTENT_SOCIAL:
        category = social_category or "thanks"
        reply = _compose_thanks(ctx, social_category=category)
        if not (reply or "").strip():
            return None
        matched = f"thanks:{category}"
    elif intent.name == INTENT_ASK_STORE_INFO:
        reply = _compose_store_link(facts)
        matched = "store_link"
    elif intent.name == INTENT_ASK_WORKING_HOURS:
        reply = _compose_working_hours(facts)
        matched = "working_hours"
    elif intent.name == INTENT_FAREWELL:
        reply = _compose_farewell(ctx)
        matched = "goodbye"
    else:
        return None

    reply = (reply or "").strip()
    if not reply:
        return None

    logger.info(
        "[LAYER0_ROUTER] matched=%s tenant=%s intent=%s preview=%r",
        matched,
        tenant_id,
        intent.name,
        text[:80],
    )
    return Layer0RouteDecision(
        matched=matched,
        reply_text=reply,
        intent_name=intent.name,
        social_category=social_category,
    )


__all__ = [
    "Layer0RouteDecision",
    "evaluate_layer0_route",
    "layer0_router_enabled",
]
