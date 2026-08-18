"""
brain/intent/classifier.py
──────────────────────────
IntentClassifier — the single entry point consumed by MerchantBrain.

Phase 1 hybrid strategy:
  1. Run rules.match() synchronously (0 latency).
  2. If confidence >= RULES_ONLY_THRESHOLD: return immediately,
     except Family 3 product-visual (Brain semantic ownership required).
  3. Otherwise: run slot_extractor.extract_slots() (fast Haiku call).
     Merge the LLM's intent_hint into the result if the LLM's hint is
     more specific than the rules result. ``general`` is the documented
     non-authoritative Layer 2 fallback and must not erase a supported
     rule candidate. Any other non-empty Layer 2 hint is the
     authoritative semantic owner.

This keeps the "happy path" (clear Arabic greeting / product ask / buy)
at zero extra latency while falling through to LLM only for ambiguous input.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING
from ..types import (
    INTENT_GENERAL,
    INTENT_PICK_LIST_ITEM,
    INTENT_PRODUCT_VISUAL_REQUEST,
    INTENT_TALK_HUMAN,
    Intent,
    MerchantConversationState,
)
from . import rules
from . import slot_extractor as _slot_mod
from .ordering_extractor import extract_ordering_slots

logger = logging.getLogger("nahla.brain.classifier")

# Rules with confidence >= this bypass LLM slot extraction
RULES_ONLY_THRESHOLD = 0.85

# Family 3: product-media need is Brain-semantic. Legacy visual regex may
# remain a compatibility candidate via rules.match(), but it must not
# rules-only short-circuit customer runtime and skip Layer 2 extraction.
_BRAIN_SEMANTIC_REQUIRED_INTENTS = frozenset({
    INTENT_PRODUCT_VISUAL_REQUEST,
})

# Closed classifier-ownership provenance. Distinguishes the merge winner
# without a second intent classifier or a visual-specific allowlist.
PROVENANCE_LAYER2_SEMANTIC_OVERRIDE = "LAYER2_SEMANTIC_OVERRIDE"
PROVENANCE_RULE_CANDIDATE_CONFIRMED = "RULE_CANDIDATE_CONFIRMED"
PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2 = (
    "RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2"
)


def is_authoritative_layer2_intent(hint: Any) -> bool:
    """True when Layer 2 returned a more-specific semantic result than general.

    The slot extractor always emits ``intent_hint``, defaulting to
    ``general`` when it has no usable operational label. That fallback is
    not an authoritative owner. Any other non-empty hint is.
    """
    name = str(hint or "").strip()
    return bool(name) and name != INTENT_GENERAL


def _stamp_classifier_precedence(
    slots: Dict[str, Any],
    *,
    rule_intent: Intent | None,
    layer2_hint: str,
    winner: str,
    provenance: str,
) -> None:
    slots["semantic_owner"] = "brain_classifier"
    slots["classification_provenance"] = provenance
    slots["precedence_winner"] = winner
    if rule_intent is not None and str(rule_intent.name or "").strip():
        slots["rule_candidate"] = str(rule_intent.name)
    if str(layer2_hint or "").strip():
        slots["layer2_result"] = str(layer2_hint)


def _resolve_layer2_rule_precedence(
    *,
    rule_intent: Intent | None,
    llm_hint: str,
    base_conf: float,
) -> tuple[str, float, str, str, str]:
    """Apply the canonical Layer 2 vs raw-rule ownership contract.

    Returns ``(name, confidence, extraction_method, provenance, winner)``.
    This is classifier ownership, not a visual/product-media feature.
    """
    layer2 = str(llm_hint or "").strip() or INTENT_GENERAL
    rule_name = str(rule_intent.name) if rule_intent is not None else ""

    if is_authoritative_layer2_intent(layer2):
        if rule_name and layer2 == rule_name:
            return (
                rule_name,
                float(base_conf or 0.72),
                "hybrid",
                PROVENANCE_RULE_CANDIDATE_CONFIRMED,
                "rule_candidate",
            )
        return (
            layer2,
            0.72,
            "llm",
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE,
            "layer2",
        )
    if rule_intent is not None:
        return (
            rule_name,
            float(base_conf or 0.72),
            "hybrid",
            PROVENANCE_RULE_CANDIDATE_CONFIRMED,
            "rule_candidate",
        )
    return (
        INTENT_GENERAL,
        0.72,
        "llm",
        PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2,
        "layer2",
    )


def _brain_owned_product_visual_intent(
    *,
    message: str,
    slots: Dict[str, Any],
    confidence: float,
    method: str = "hybrid",
    provenance: str = PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2,
    layer2_hint: str = "",
    rule_intent: Intent | None = None,
) -> Intent:
    clean = {k: v for k, v in (slots or {}).items() if v not in ("", {}, None)}
    _stamp_classifier_precedence(
        clean,
        rule_intent=rule_intent,
        layer2_hint=layer2_hint,
        winner="rule_candidate",
        provenance=provenance,
    )
    return Intent(
        name=INTENT_PRODUCT_VISUAL_REQUEST,
        confidence=confidence,
        slots=clean,
        raw_message=message,
        extraction_method=method,
    )

# Stages where we MUST run slot extraction even for high-confidence rules.
# A customer mid-checkout sending "تركي الحارثي" matches the rules-only
# greeting/general bucket, but we need to surface customer_name etc. to the
# decision engine so the order continues. Skipping extraction here is the
# root cause of the "bot loses my name when I send it" symptom.
_ORDERING_STAGES = {STAGE_DECIDING, STAGE_ORDERING, STAGE_CHECKOUT}


class DefaultIntentClassifier:
    """
    Implements the IntentClassifier protocol.
    """

    async def classify(
        self,
        message: str,
        history: List[Dict[str, Any]],
        state: MerchantConversationState,
    ) -> Intent:
        # ── Layer 1: rules ─────────────────────────────────────────────────
        rule_intent = rules.match(message)

        try:
            from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
                boost_track_order_intent,
            )
            _tracking_boost = boost_track_order_intent(
                message,
                rule_intent,
                state=state,
                history=history,
            )
            if _tracking_boost is not None:
                logger.info(
                    "[Classifier] order_tracking_guard → track_order | preview=%r",
                    (message or "")[:60],
                )
                return _tracking_boost
        except Exception:  # noqa: BLE001  # noqa: silent-ok — guard must not block classify
            pass

        in_order_flow = (
            state is not None
            and getattr(state, "stage", None) in _ORDERING_STAGES
            and bool(getattr(state, "current_product_focus", None))
        )

        # ── Numeric pick is ALWAYS rule-based (deterministic) ──────────────
        # When the user types "1", "2", "٣", we must NEVER let the LLM
        # reclassify this as anything else. The decision engine relies on
        # PICK_LIST_ITEM with list_index to bridge to ACTION_PROPOSE_DRAFT_ORDER.
        if rule_intent and rule_intent.name == INTENT_PICK_LIST_ITEM:
            logger.info(
                "[Classifier] numeric pick → rules-only | idx=%s",
                rule_intent.slots.get("list_index"),
            )
            return rule_intent

        if (
            rule_intent
            and rule_intent.confidence >= RULES_ONLY_THRESHOLD
            and not in_order_flow
            and str(rule_intent.name or "") not in _BRAIN_SEMANTIC_REQUIRED_INTENTS
        ):
            logger.debug(
                "[Classifier] rules-only | intent=%s conf=%.2f",
                rule_intent.name, rule_intent.confidence,
            )
            return rule_intent

        # ── Layer 2: LLM slot extraction ───────────────────────────────────
        logger.info(
            "[Classifier] calling LLM slot extractor | in_order_flow=%s rule=%s",
            in_order_flow,
            rule_intent.name if rule_intent else None,
        )
        slots = await _slot_mod.extract_slots(message, history)

        # Layer 2b: deterministic Arabic ordering-slot extractor. Runs
        # ALWAYS during the order flow, and as a defensive fallback
        # whenever the LLM returns nothing useful — guarantees that
        # a free-text "تركي الحارثي / الطائف / <maps url>" is parsed
        # into customer_name / city / google_maps_url even when the
        # Anthropic key is unset.
        if in_order_flow or not slots:
            heuristic_slots = extract_ordering_slots(message)
            if heuristic_slots:
                slots = {**heuristic_slots, **(slots or {})}

        try:
            from modules.ai.brain.postprocess.payment_reply_guard import (  # noqa: PLC0415
                strip_customer_name_slots_when_future_transfer,
            )
            slots = strip_customer_name_slots_when_future_transfer(message, slots)
        except Exception:  # noqa: BLE001
            pass

        if not slots:
            # LLM unavailable or empty — fall back to rules or general.
            # Product-visual still cannot return as a rules-only owner after
            # Layer 2 was attempted.
            if (
                rule_intent
                and str(rule_intent.name or "") in _BRAIN_SEMANTIC_REQUIRED_INTENTS
            ):
                return _brain_owned_product_visual_intent(
                    message=message,
                    slots=dict(rule_intent.slots or {}),
                    confidence=float(rule_intent.confidence or 0.72),
                    method="hybrid",
                    provenance=PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2,
                    layer2_hint="",
                    rule_intent=rule_intent,
                )
            if rule_intent:
                return rule_intent
            return Intent(
                name=INTENT_GENERAL,
                confidence=0.50,
                raw_message=message,
                extraction_method="rules",
            )

        # Merge: start from the rules intent (or general), then enrich slots
        base_intent = rule_intent.name if rule_intent else INTENT_GENERAL
        base_conf   = rule_intent.confidence if rule_intent else 0.50

        llm_hint = slots.pop("intent_hint", None) or INTENT_GENERAL

        # ── Guard: never let LLM hijack an in-order-flow turn into handoff ──
        # When the customer is mid-checkout and provides order data
        # (name / city / short address code / google maps url), the LLM
        # sometimes mis-classifies this as `talk_to_human`. That triggers
        # ACTION_HANDOFF and breaks the entire order flow.
        # The user must EXPLICITLY ask to talk to a human (rules will catch
        # it with high confidence) — we never trust LLM to escalate.
        if llm_hint == INTENT_TALK_HUMAN:
            _has_order_data = any(slots.get(k) for k in (
                "customer_name", "city", "short_address_code",
                "google_maps_url", "customer_first_name", "customer_last_name",
            ))
            if in_order_flow or _has_order_data:
                logger.warning(
                    "[Classifier] BLOCKED LLM talk_to_human hint | "
                    "in_order_flow=%s has_order_data=%s — keeping rule intent",
                    in_order_flow, _has_order_data,
                )
                llm_hint = base_intent  # keep rule intent (likely general)

        resolved_name, resolved_conf, method, provenance, winner = (
            _resolve_layer2_rule_precedence(
                rule_intent=rule_intent,
                llm_hint=str(llm_hint or INTENT_GENERAL),
                base_conf=float(base_conf or 0.50),
            )
        )

        # Remove empty string values from slots
        clean_slots = {k: v for k, v in slots.items() if v not in ("", {}, None)}
        _stamp_classifier_precedence(
            clean_slots,
            rule_intent=rule_intent,
            layer2_hint=str(llm_hint or ""),
            winner=winner,
            provenance=provenance,
        )

        try:
            from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
                boost_track_order_intent,
            )
            _tracking_boost = boost_track_order_intent(
                message,
                rule_intent,
                state=state,
                history=history,
            )
            if _tracking_boost is not None:
                resolved_name = _tracking_boost.name
                resolved_conf = max(resolved_conf, _tracking_boost.confidence)
                method = "order_tracking_guard"
        except Exception:  # noqa: BLE001  # noqa: silent-ok — guard must not block classify
            pass

        intent = Intent(
            name=resolved_name,
            confidence=resolved_conf,
            slots=clean_slots,
            raw_message=message,
            extraction_method=method,
        )
        logger.debug(
            "[Classifier] %s | intent=%s conf=%.2f slots=%s",
            method, resolved_name, resolved_conf, clean_slots,
        )
        return intent
