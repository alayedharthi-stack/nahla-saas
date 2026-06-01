"""
brain/interpret/semantic_routing.py
───────────────────────────────────
Apply semantic interpretations to intent + decision routing.

The interpreter proposes meaning; guards and gates still enforce execution.
"""
from __future__ import annotations

from typing import Any, Optional

from ..decision.actions import (
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
)
from ..types import (
    BrainContext,
    Decision,
    INTENT_ASK_PRICE,
    INTENT_PICK_LIST_ITEM,
    Intent,
)
from .semantic_turn_interpreter import (
    INTENT_ASK_PRICE_SPECIFIC_VARIANT,
    INTENT_CLARIFY_VARIANTS_NATURAL,
    INTENT_FULFILLMENT_LOCATION_UPDATE,
    INTENT_REFER_LAST_PRODUCT,
    INTENT_SELECT_LIST_OPTION,
    INTENT_SHOW_ALL_VARIANTS_OR_PRICES,
    SemanticTurnInterpretation,
)

_MIN_CONFIDENCE = 0.70


def apply_semantic_intent_override(
    intent: Intent,
    interpretation: SemanticTurnInterpretation,
    *,
    state: Any = None,
) -> Intent:
    """Adjust classifier output when semantic confidence is high enough."""
    if float(interpretation.confidence or 0) < _MIN_CONFIDENCE:
        return intent

    slots = dict(intent.slots or {})
    slots["semantic_interpretation"] = interpretation.to_dict()

    name = intent.name
    if interpretation.interpreted_intent == INTENT_SELECT_LIST_OPTION:
        name = INTENT_PICK_LIST_ITEM
        slots["list_index"] = int(
            (interpretation.slots or {}).get("list_index") or 1
        )
    elif interpretation.interpreted_intent in (
        INTENT_SHOW_ALL_VARIANTS_OR_PRICES,
        INTENT_ASK_PRICE_SPECIFIC_VARIANT,
    ):
        name = INTENT_ASK_PRICE
        slots["semantic_intent"] = interpretation.interpreted_intent
        if interpretation.slots.get("size_hint"):
            slots["size_hint"] = interpretation.slots["size_hint"]
    elif interpretation.interpreted_intent == INTENT_REFER_LAST_PRODUCT:
        name = INTENT_ASK_PRICE
        slots["semantic_intent"] = INTENT_REFER_LAST_PRODUCT

    return Intent(
        name=name,
        confidence=max(float(intent.confidence or 0), float(interpretation.confidence)),
        slots=slots,
        raw_message=interpretation.raw_text or intent.raw_message,
        extraction_method="semantic_interpreter",
    )


def try_semantic_interpretation_decision(
    ctx: BrainContext,
) -> Optional[Decision]:
    """Map a high-confidence interpretation to a routing decision."""
    interp = getattr(ctx, "semantic_interpretation", None)
    if interp is None:
        slots = getattr(ctx.intent, "slots", None) or {}
        raw = slots.get("semantic_interpretation")
        if isinstance(raw, dict):
            interp = SemanticTurnInterpretation(
                canonical_text=str(raw.get("canonical_text") or ""),
                interpreted_intent=str(raw.get("interpreted_intent") or ""),
                context_anchor=str(raw.get("context_anchor") or ""),
                confidence=float(raw.get("confidence") or 0),
                is_typo_repair=bool(raw.get("is_typo_repair")),
                repair_notes=str(raw.get("repair_notes") or ""),
                commerce_frame=str(raw.get("commerce_frame") or ""),
                topic_shift=bool(raw.get("topic_shift")),
                should_override_social=bool(raw.get("should_override_social")),
                override_reason=str(raw.get("override_reason") or ""),
                slots=dict(raw.get("slots") or {}),
                raw_text=str(raw.get("raw_text") or ctx.message or ""),
            )

    if interp is None or float(interp.confidence or 0) < _MIN_CONFIDENCE:
        return None

    if interp.topic_shift:
        return None

    intent_name = str(getattr(ctx.intent, "name", "") or "")
    if intent_name == "social" and not interp.should_override_social:
        return None

    state = ctx.state
    focus = getattr(state, "current_product_focus", None) or {}

    if interp.interpreted_intent == INTENT_SHOW_ALL_VARIANTS_OR_PRICES:
        if not focus:
            return Decision(
                action=ACTION_CLARIFY,
                args={
                    "question": (
                        "تقصد أسعار أي منتج؟ اكتب اسمه أو نوعه "
                        "وأعرض لك كل الأحجام والأسعار."
                    ),
                },
                reason="semantic: all sizes/prices without product focus",
                confidence=interp.confidence,
            )
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "show_all_variants_prices",
                "product": dict(focus),
                "semantic_interpretation": interp.to_dict(),
            },
            reason=(
                f"semantic: show all variants/prices "
                f"(anchor={interp.context_anchor})"
            ),
            confidence=interp.confidence,
        )

    if interp.interpreted_intent == INTENT_ASK_PRICE_SPECIFIC_VARIANT:
        if focus:
            return Decision(
                action=ACTION_LLM_REPLY,
                args={
                    "topic": "price",
                    "product": dict(focus),
                    "size_hint": (interp.slots or {}).get("size_hint"),
                    "semantic_interpretation": interp.to_dict(),
                },
                reason=(
                    f"semantic: price for size "
                    f"{(interp.slots or {}).get('size_hint')}"
                ),
                confidence=interp.confidence,
            )
        return Decision(
            action=ACTION_CLARIFY,
            args={
                "question": (
                    "تقصد سعر أي منتج بالحجم المطلوب؟ "
                    "اكتب اسم المنتج أو نوعه."
                ),
            },
            reason="semantic: size price without product focus",
            confidence=interp.confidence,
        )

    if interp.interpreted_intent == INTENT_SELECT_LIST_OPTION:
        idx = int((interp.slots or {}).get("list_index") or 1)
        if focus and list(getattr(state, "pending_option_groups", None) or []):
            return Decision(
                action=ACTION_PROPOSE_DRAFT_ORDER,
                args={"product": dict(focus)},
                reason=f"semantic: ordinal pick {idx} during option selection",
                confidence=interp.confidence,
            )
        return None

    if interp.interpreted_intent == INTENT_FULFILLMENT_LOCATION_UPDATE:
        try:
            from ..order_context_gate import try_order_context_update_decision  # noqa: PLC0415

            upd = try_order_context_update_decision(ctx)
            if upd is not None:
                return upd
        except Exception:  # noqa: BLE001
            pass
        return None

    if interp.interpreted_intent == INTENT_CLARIFY_VARIANTS_NATURAL:
        return Decision(
            action=ACTION_CLARIFY,
            args={
                "question": (
                    "تقصد أسعار كل الأحجام لمنتج معيّن؟ "
                    "اكتب اسم المنتج أو نوعه وأعرض لك الخيارات."
                ),
            },
            reason="semantic: clarify all sizes without anchor",
            confidence=interp.confidence,
        )

    if interp.interpreted_intent == INTENT_REFER_LAST_PRODUCT and focus:
        return Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "price",
                "product": dict(focus),
                "semantic_interpretation": interp.to_dict(),
            },
            reason="semantic: deictic reference to focused product",
            confidence=interp.confidence,
        )

    return None


__all__ = [
    "apply_semantic_intent_override",
    "try_semantic_interpretation_decision",
]
