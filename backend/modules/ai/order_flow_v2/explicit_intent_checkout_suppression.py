"""Explicit current-turn intents that bypass stale checkout rehydration."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

from modules.ai.brain.types import (
    INTENT_ASK_OWNER_CONTACT,
    INTENT_ASK_PAYMENT_INFO,
    INTENT_ASK_PRODUCT,
    INTENT_GREETING,
    INTENT_LATEST_ORDER_SUMMARY,
    INTENT_ORDER_HISTORY_COUNT,
    INTENT_SOCIAL,
    INTENT_TALK_HUMAN,
    INTENT_TRACK_ORDER,
)

from .triggers import (
    is_catalog_selection_acknowledgment,
    is_checkout_escape_inquiry,
    is_checkout_order_number_intent,
    is_delivery_continuation_intent,
    is_resume_order_command,
)

logger = logging.getLogger("nahla.order_flow_v2.explicit_intent")

PAYMENT_BARCODE_IMAGE_REQUEST = "payment_barcode_image_request"
SOCIAL_GREETING = "social_greeting"
SOCIAL_THANKS = "social_thanks"
SOCIAL_DUA = "social_dua"
SOCIAL_PHATIC = "social_phatic"
PRODUCT_KNOWLEDGE_FACTS = "product_knowledge_facts"
EXISTING_ORDER_SUPPORT = "existing_order_support"

_BYPASS_INTENTS = frozenset(
    {
        INTENT_ORDER_HISTORY_COUNT,
        INTENT_LATEST_ORDER_SUMMARY,
        INTENT_TRACK_ORDER,
        INTENT_ASK_PAYMENT_INFO,
        INTENT_ASK_PRODUCT,
        INTENT_TALK_HUMAN,
        INTENT_ASK_OWNER_CONTACT,
        PAYMENT_BARCODE_IMAGE_REQUEST,
        "catalog_browse",
        INTENT_GREETING,
        INTENT_SOCIAL,
        SOCIAL_GREETING,
        SOCIAL_THANKS,
        SOCIAL_DUA,
        SOCIAL_PHATIC,
        PRODUCT_KNOWLEDGE_FACTS,
    }
)

_PHATIC_GREETING_RE = re.compile(
    r"^(?:"
    r"كيف\s*الحال|كيف\s*حالك|كيفك|كيف\s*أحوالك|كيف\s*احوالك|"
    r"(?:انت|أنت|انتي|أنتِ)\s*وش\s*(?:أ|ا|إ)?خبارك|"
    r"وش\s*(?:أ|ا|إ)?خبارك|كيف\s*(?:أ|ا|إ)?خبارك|"
    r"(?:أ|ا|إ)?خبارك\s*كيف|"
    r"هاي|هلو|hello|hi\b|hey\b"
    r")\s*[!.؟?]*\s*$",
    re.I | re.UNICODE,
)

_SHORT_AFFIRMATION_RE = re.compile(
    r"^(?:نعم|ايوه|أيوه|ايه|أيه|تمام|اوكي|أوكي|ok|okay|ماشي|طيب)\s*[!.؟?]*$",
    re.I | re.UNICODE,
)
_NUMERIC_SLOT_RE = re.compile(r"^\d{1,3}\s*[!.؟?]*$")
_PAYMENT_METHOD_ANSWER_RE = re.compile(
    r"^(?:"
    r"تحويل\s*بنكي|تحويل|بنكي|"
    r"دفع\s*عند\s*الاستلام|الدفع\s*عند\s*الاستلام|cod|كاش"
    r")\s*[!.؟?]*$",
    re.I | re.UNICODE,
)
_ADDRESS_CONFIRM_RE = re.compile(
    r"(?:"
    r"اعتمد\s*(?:نفس\s*)?(?:ال)?عنوان|"
    r"نفس\s*العنوان|"
    r"العنوان\s*(?:صحيح|صح|تمام|نعم|موافق)"
    r")",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class CheckoutSuppressionDecision:
    suppress: bool
    detected_intent: str = ""
    reason: str = ""


def _prep_dict(order_prep: Any) -> Dict[str, Any]:
    if isinstance(order_prep, dict):
        return dict(order_prep)
    return {}


def _message_has_explicit_order_reference(message: str) -> bool:
    """True when inbound names a specific order number (not the active draft ask)."""
    try:
        from core.order_status_dedup_reply import (  # noqa: PLC0415
            _extract_order_ref_from_inbound,
        )

        return bool(_extract_order_ref_from_inbound(message))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional ref extractor
        return False


def is_active_checkout_draft_order_number_question(message: str) -> bool:
    """Bare «كم رقم الطلب؟» during checkout — not a named historical order ref."""
    if not is_checkout_order_number_intent(message):
        return False
    return not _message_has_explicit_order_reference(message)


def is_checkout_payment_method_answer(
    message: str,
    order_prep: Dict[str, Any],
    missing_fields: Optional[Sequence[str]] = None,
) -> bool:
    """Bare payment-method selection during checkout — not an info request."""
    text = str(message or "").strip()
    if not text or is_payment_barcode_image_request(text):
        return False
    prep = _prep_dict(order_prep)
    missing = list(missing_fields or [])
    last_field = str(prep.get("order_flow_v2_last_field") or "").strip().lower()
    awaiting_payment = last_field == "payment_method" or (
        "payment_method" in missing and not _higher_priority_than_payment(missing)
    )
    if not awaiting_payment:
        return False
    if _PAYMENT_METHOD_ANSWER_RE.match(text):
        return True
    try:
        from .payment import requested_bank_brand  # noqa: PLC0415

        return bool(requested_bank_brand(text)) and len(text.split()) <= 3
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional bank brand probe
        return False


def is_payment_barcode_image_request(message: str) -> bool:
    try:
        from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
            is_payment_barcode_image_request as _is_barcode,
        )

        return _is_barcode(message)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import boundary
        return False


def detect_social_phatic_intent(message: str) -> str:
    """Return a bypass key when the turn is social/phatic — not checkout continuation."""
    text = str(message or "").strip()
    if not text:
        return ""

    try:
        from .triggers import is_greeting_message  # noqa: PLC0415

        if is_greeting_message(text):
            return INTENT_GREETING
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional greeting probe
        pass

    if _PHATIC_GREETING_RE.match(text):
        return SOCIAL_GREETING

    try:
        from modules.ai.brain.intent.social_classifier import (  # noqa: PLC0415
            SOCIAL_BASMALA,
            SOCIAL_BLESSING,
            SOCIAL_PROPHET_INVOCATION,
            SOCIAL_THANKS as CLASSIFIER_SOCIAL_THANKS,
            classify_social,
        )

        social = classify_social(text)
        if social is not None:
            category = str(getattr(social, "category", "") or "")
            if category == CLASSIFIER_SOCIAL_THANKS:
                return SOCIAL_THANKS
            if category in {
                SOCIAL_BLESSING,
                SOCIAL_BASMALA,
                SOCIAL_PROPHET_INVOCATION,
            }:
                return SOCIAL_DUA
            return INTENT_SOCIAL
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional social classifier
        pass

    try:
        from modules.ai.brain.intent.rules import match as match_intent  # noqa: PLC0415

        matched = match_intent(text)
        if matched is not None and float(getattr(matched, "confidence", 0) or 0) >= 0.85:
            name = str(getattr(matched, "name", "") or "")
            if name == INTENT_GREETING:
                return INTENT_GREETING
            if name == INTENT_SOCIAL:
                return INTENT_SOCIAL
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional intent match
        pass

    return ""


def is_explicit_payment_info_request(
    message: str,
    *,
    order_prep: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[Sequence[str]] = None,
) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if is_checkout_payment_method_answer(text, _prep_dict(order_prep), missing_fields):
        return False
    if is_payment_barcode_image_request(text):
        return True
    try:
        from modules.ai.brain.decision.payment_barcode_routing import (  # noqa: PLC0415
            ASK_PAYMENT_INFO,
            classify_payment_request,
        )

        return classify_payment_request(text) == ASK_PAYMENT_INFO
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import boundary
        return False


def _detect_pending_order_support_intent(
    message: str,
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    history: Optional[Sequence[Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
) -> str:
    """Bypass stale checkout when recent order-reference context owns the turn."""
    hist = list(history or [])
    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            has_pending_order_reference_evidence,
            is_order_support_operational_follow_up,
        )

        has_pending = has_pending_order_reference_evidence(
            state=brain_state,
            history=hist,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order-support probe must not block checkout
        return ""

    if not has_pending:
        return ""

    semantic = str(message or "").strip()
    try:
        from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
            is_placed_order_statement,
        )

        if semantic and is_placed_order_statement(semantic):
            return EXISTING_ORDER_SUPPORT
        if semantic and is_order_support_operational_follow_up(
            semantic,
            state=brain_state,
            history=hist,
        ):
            return EXISTING_ORDER_SUPPORT
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order-support probe must not block checkout
        return ""

    try:
        from modules.ai.media.routing_guard import (  # noqa: PLC0415
            is_audio_without_trusted_transcript,
        )

        if is_audio_without_trusted_transcript(
            inbound_metadata,
            semantic_message=semantic,
        ):
            return EXISTING_ORDER_SUPPORT
    except Exception:  # noqa: BLE001  # noqa: silent-ok — audio transcript probe must not block checkout
        return ""

    return ""


def detect_explicit_non_checkout_intent(
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    *,
    order_prep: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[Sequence[str]] = None,
    history: Optional[Sequence[Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
) -> str:
    """Return a bypass intent key when the current turn is not checkout continuation."""
    pending_support = _detect_pending_order_support_intent(
        message,
        inbound_metadata=inbound_metadata,
        history=history,
        brain_state=brain_state,
        order_prep=order_prep,
    )
    if pending_support:
        return pending_support

    text = str(message or "").strip()
    if not text:
        return ""

    prep = _prep_dict(order_prep)

    if is_checkout_payment_method_answer(text, prep, missing_fields):
        return ""

    social_phatic = detect_social_phatic_intent(text)
    if social_phatic:
        return social_phatic

    if is_active_checkout_draft_order_number_question(text):
        return ""

    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            is_product_knowledge_message,
        )

        if is_product_knowledge_message(text):
            return PRODUCT_KNOWLEDGE_FACTS
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional product-knowledge probe
        pass

    if (
        is_checkout_order_number_intent(text)
        and _message_has_explicit_order_reference(text)
    ):
        return INTENT_TRACK_ORDER

    if is_checkout_escape_inquiry(text, inbound_metadata):
        return "catalog_browse"

    if is_explicit_payment_info_request(
        text,
        order_prep=prep,
        missing_fields=missing_fields,
    ):
        if is_payment_barcode_image_request(text):
            return PAYMENT_BARCODE_IMAGE_REQUEST
        return INTENT_ASK_PAYMENT_INFO

    try:
        from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: PLC0415
            is_explicit_order_tracking_request,
        )

        if is_explicit_order_tracking_request(text):
            return INTENT_TRACK_ORDER
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional guard import
        pass

    try:
        from modules.ai.brain.intent.rules import match as match_intent  # noqa: PLC0415

        matched = match_intent(text)
        if matched is not None and float(getattr(matched, "confidence", 0) or 0) >= 0.85:
            name = str(getattr(matched, "name", "") or "")
            if name in _BYPASS_INTENTS:
                return name
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional intent match
        pass

    return ""


def is_checkout_continuation_turn(
    message: str,
    *,
    order_prep: Dict[str, Any],
    brain_state: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[Sequence[str]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    history: Optional[Sequence[Any]] = None,
) -> bool:
    """True when the customer is answering the active checkout prompt."""
    text = str(message or "").strip()
    if not text:
        return False

    prep = _prep_dict(order_prep)
    missing = list(missing_fields or [])

    if is_active_checkout_draft_order_number_question(text):
        return True

    if detect_explicit_non_checkout_intent(
        text,
        inbound_metadata,
        order_prep=prep,
        missing_fields=missing,
        history=history,
        brain_state=brain_state,
    ):
        return False

    if is_catalog_selection_acknowledgment(text):
        return True
    if is_resume_order_command(text):
        return True
    if is_delivery_continuation_intent(text):
        return True
    if _SHORT_AFFIRMATION_RE.match(text):
        return True
    if _ADDRESS_CONFIRM_RE.search(text):
        return True

    try:
        from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
            is_address_on_file_claim,
        )

        if is_address_on_file_claim(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional address claim probe
        pass

    last_field = str(prep.get("order_flow_v2_last_field") or "").strip().lower()
    if _NUMERIC_SLOT_RE.match(text):
        if last_field in {"product", "quantity", "qty", "variant"}:
            return True
        if any(f in missing for f in ("product", "quantity", "qty", "variant")):
            return True

    if is_checkout_payment_method_answer(text, prep, missing):
        return True

    try:
        from .slot_ownership import is_explicit_customer_name_turn  # noqa: PLC0415

        if is_explicit_customer_name_turn(text):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional name slot probe
        pass

    try:
        from .slot_ownership import apply_active_checkout_city_turn  # noqa: PLC0415

        city_patch, _ = apply_active_checkout_city_turn(
            message=text,
            order_prep=prep,
            missing_fields=missing,
            checkout_active=True,
        )
        if city_patch:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional city slot probe
        pass

    return False


def _higher_priority_than_payment(missing_fields: Sequence[str]) -> bool:
    priority = ("customer_name", "city", "delivery_address")
    missing = set(missing_fields or [])
    return any(field in missing for field in priority)


def evaluate_stale_checkout_suppression(
    *,
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
    missing_fields: Optional[Sequence[str]] = None,
    history: Optional[Sequence[Any]] = None,
    checkout_active: bool,
    draft_active: bool,
) -> CheckoutSuppressionDecision:
    """Suppress OrderFlowV2 when stale checkout would hijack an explicit operational turn."""
    if not checkout_active and not draft_active:
        return CheckoutSuppressionDecision(suppress=False)

    prep = _prep_dict(order_prep)
    if is_checkout_continuation_turn(
        message,
        order_prep=prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
        inbound_metadata=inbound_metadata,
        history=history,
    ):
        return CheckoutSuppressionDecision(
            suppress=False,
            reason="checkout_continuation_turn",
        )

    detected = detect_explicit_non_checkout_intent(
        message,
        inbound_metadata,
        order_prep=prep,
        missing_fields=missing_fields,
        history=history,
        brain_state=brain_state,
    )
    if not detected:
        return CheckoutSuppressionDecision(suppress=False, reason="no_explicit_intent")

    return CheckoutSuppressionDecision(
        suppress=True,
        detected_intent=detected,
        reason="explicit_intent_over_stale_checkout",
    )


def should_suppress_stale_checkout_for_message(
    *,
    message: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
    history: Optional[Sequence[Any]] = None,
    checkout_active: bool,
    draft_active: bool = False,
) -> bool:
    decision = evaluate_stale_checkout_suppression(
        message=message,
        inbound_metadata=inbound_metadata,
        order_prep=order_prep,
        brain_state=brain_state,
        history=history,
        checkout_active=checkout_active,
        draft_active=draft_active,
    )
    return decision.suppress


def log_checkout_suppressed_by_explicit_intent(
    *,
    tenant_id: int,
    conversation_id: Optional[int],
    detected_intent: str,
    checkout_ref: str,
    reason: str,
) -> None:
    logger.info(
        "[CHECKOUT_OWNER_SUPPRESSED_BY_EXPLICIT_INTENT] tenant_id=%s "
        "conversation_id=%s detected_intent=%s checkout_ref=%s reason=%s",
        tenant_id,
        conversation_id if conversation_id is not None else "-",
        detected_intent or "-",
        checkout_ref or "-",
        reason or "-",
    )


__all__ = [
    "CheckoutSuppressionDecision",
    "EXISTING_ORDER_SUPPORT",
    "PAYMENT_BARCODE_IMAGE_REQUEST",
    "PRODUCT_KNOWLEDGE_FACTS",
    "SOCIAL_GREETING",
    "SOCIAL_DUA",
    "SOCIAL_PHATIC",
    "SOCIAL_THANKS",
    "detect_explicit_non_checkout_intent",
    "detect_social_phatic_intent",
    "evaluate_stale_checkout_suppression",
    "is_active_checkout_draft_order_number_question",
    "is_checkout_continuation_turn",
    "is_checkout_payment_method_answer",
    "is_explicit_payment_info_request",
    "is_payment_barcode_image_request",
    "log_checkout_suppressed_by_explicit_intent",
    "should_suppress_stale_checkout_for_message",
]
