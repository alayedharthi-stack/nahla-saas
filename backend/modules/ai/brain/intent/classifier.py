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
     rule candidate. A non-empty non-general Layer 2 hint is evidence:
     a genuinely different operational label is an authoritative override;
     a Layer 2 hint that the canonical semantic-relation registry proves is
     the direct broader label of the rule candidate is compatible evidence
     and must not erase that more-specific rule candidate.

This keeps the "happy path" (clear Arabic greeting / product ask / buy)
at zero extra latency while falling through to LLM only for ambiguous input.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..state.stages import STAGE_CHECKOUT, STAGE_DECIDING, STAGE_ORDERING
from ..types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_STORE_INFO,
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
from .semantic_relation import is_direct_broader_relation

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
PROVENANCE_COMPATIBLE_BROADER_EVIDENCE = "COMPATIBLE_BROADER_EVIDENCE"

SEMANTIC_RELATION_SAME_LABEL = "same_label"
SEMANTIC_RELATION_AUTHORITATIVE_OVERRIDE = "authoritative_override"
SEMANTIC_RELATION_COMPATIBLE_BROADER = "compatible_broader_evidence"
SEMANTIC_RELATION_NON_AUTHORITATIVE = "non_authoritative_layer2"
SEMANTIC_RELATION_NO_AUTHORITATIVE = "no_authoritative_layer2"


def is_authoritative_layer2_intent(hint: Any) -> bool:
    """True when Layer 2 returned a non-empty, non-general hint.

    The slot extractor always emits ``intent_hint``, defaulting to
    ``general`` when it has no usable operational label. That fallback is
    not an authoritative owner. A non-general hint is Layer 2 evidence —
    precedence still decides whether it overrides or is compatible broader
    evidence for a more-specific rule candidate.
    """
    name = str(hint or "").strip()
    return bool(name) and name != INTENT_GENERAL


def layer2_is_compatible_broader_evidence(rule_name: Any, layer2_hint: Any) -> bool:
    """True only when the canonical registry proves a direct broader relation.

    Classifier ownership consumes ``is_direct_broader_relation``; it does not
    infer hierarchy from Layer 2 vocabulary, out-of-vocabulary status, shared
    domain, or shared downstream actions. Unknown relationships fail closed.
    """
    return is_direct_broader_relation(rule_name, layer2_hint)


def _stamp_classifier_precedence(
    slots: Dict[str, Any],
    *,
    rule_intent: Intent | None,
    layer2_hint: str,
    winner: str,
    provenance: str,
    semantic_relation: str = "",
) -> None:
    slots["semantic_owner"] = "brain_classifier"
    slots["classification_provenance"] = provenance
    slots["precedence_winner"] = winner
    if semantic_relation:
        slots["semantic_relation"] = semantic_relation
    if rule_intent is not None and str(rule_intent.name or "").strip():
        slots["rule_candidate"] = str(rule_intent.name)
    if str(layer2_hint or "").strip():
        slots["layer2_result"] = str(layer2_hint)


def _resolve_layer2_rule_precedence(
    *,
    rule_intent: Intent | None,
    llm_hint: str,
    base_conf: float,
) -> tuple[str, float, str, str, str, str]:
    """Apply the canonical Layer 2 vs raw-rule ownership contract.

    Returns ``(name, confidence, extraction_method, provenance, winner,
    semantic_relation)``. This is classifier ownership, not a visual
    feature and not a second intent router.
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
                SEMANTIC_RELATION_SAME_LABEL,
            )
        if layer2_is_compatible_broader_evidence(rule_name, layer2):
            return (
                rule_name,
                float(base_conf or 0.72),
                "hybrid",
                PROVENANCE_COMPATIBLE_BROADER_EVIDENCE,
                "rule_candidate",
                SEMANTIC_RELATION_COMPATIBLE_BROADER,
            )
        return (
            layer2,
            0.72,
            "llm",
            PROVENANCE_LAYER2_SEMANTIC_OVERRIDE,
            "layer2",
            SEMANTIC_RELATION_AUTHORITATIVE_OVERRIDE,
        )
    if rule_intent is not None:
        return (
            rule_name,
            float(base_conf or 0.72),
            "hybrid",
            PROVENANCE_RULE_CANDIDATE_CONFIRMED,
            "rule_candidate",
            SEMANTIC_RELATION_NON_AUTHORITATIVE,
        )
    return (
        INTENT_GENERAL,
        0.72,
        "llm",
        PROVENANCE_RULE_FALLBACK_AFTER_NO_AUTHORITATIVE_LAYER2,
        "layer2",
        SEMANTIC_RELATION_NO_AUTHORITATIVE,
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
        semantic_relation=SEMANTIC_RELATION_NO_AUTHORITATIVE,
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

        # URL-only inbounds: do not let Layer 1 or Layer 2 rebuild
        # ask_owner_contact / ask_store_info from a token inside a URL.
        # Raw message stays on Intent.raw_message for model context/storage.
        try:
            from core.inbound_url_spans import is_url_only_inbound  # noqa: PLC0415

            if not in_order_flow and is_url_only_inbound(message):
                logger.info(
                    "[Classifier] url-only inbound → general (skip layer2) | preview=%r",
                    (message or "")[:60],
                )
                return Intent(
                    name=INTENT_GENERAL,
                    confidence=0.50,
                    raw_message=message,
                    extraction_method="rules",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — projection must not block classify
            pass

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

        if str(llm_hint or "") in {INTENT_ASK_OWNER_CONTACT, INTENT_ASK_STORE_INFO}:
            try:
                from modules.ai.brain.commerce.merchant_profile_intents import (  # noqa: PLC0415
                    classify_store_profile_topic,
                )

                _profile_topic = classify_store_profile_topic(message)
                _hint_ok = (
                    (
                        llm_hint == INTENT_ASK_OWNER_CONTACT
                        and _profile_topic == "owner_contact"
                    )
                    or (
                        llm_hint == INTENT_ASK_STORE_INFO
                        and _profile_topic in {
                            "store_info",
                            "store_about",
                            "store_currency",
                            "store_status",
                        }
                    )
                )
                if not _hint_ok:
                    logger.info(
                        "[Classifier] drop layer2 profile hint | hint=%s topic=%s preview=%r",
                        llm_hint,
                        _profile_topic,
                        (message or "")[:60],
                    )
                    llm_hint = base_intent if base_intent not in {
                        INTENT_ASK_OWNER_CONTACT,
                        INTENT_ASK_STORE_INFO,
                    } else INTENT_GENERAL
            except Exception:  # noqa: BLE001  # noqa: silent-ok — profile gate must not block classify
                pass

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

        resolved_name, resolved_conf, method, provenance, winner, relation = (
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
            semantic_relation=relation,
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
