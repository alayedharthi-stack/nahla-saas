"""
brain/state/state_relevance.py
──────────────────────────────
Conversational State Relevance Engine — Phase 1.

Stored workflow flags alone must NOT resume stale funnels. This module
validates whether the CURRENT inbound turn is semantically relevant to
persisted state before payment continuation, fulfillment lock resume,
candidate replay, addon upsell, or dedup fallbacks resurrect old flows.

Runs AFTER semantic interpretation when available; does not bypass any
existing guard — it sits ABOVE stale-state reuse decisions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.state_relevance")

_PAYMENT_SEMANTICS_RE = re.compile(
    r"(?:حولت|تم\s*التحويل|ايصال|إيصال|الايصال|الإيصال|تحويل|حوال[ةه]|"
    r"transfer|receipt|iban|ايبان|آيبان|سدد|ادفع|أدفع|دفع|التحويل|"
    r"ارسل.*(?:ايصال|إيصال)|أرسل.*(?:ايصال|إيصال)|proof\s*of\s*payment)",
    re.UNICODE | re.IGNORECASE,
)

_PRICE_VARIANT_COMMERCE_RE = re.compile(
    r"(?:سعر|بكم|كم\s*سعر|ثمن|حجم|احجام|أحجام|حجام|مقاس|مقاسات|"
    r"كلها|كلهم|الثاني|الثالث|الاول|الأول|كبير|صغير|وسط|"
    r"show\s*all|all\s*sizes|variant|price|how\s*much)",
    re.UNICODE | re.IGNORECASE,
)

_FULFILLMENT_SEMANTICS_RE = re.compile(
    r"(?:موقع|الموقع|عنوان|العنوان|توصيل|استلام|وصل|ارسل(?:ه|ها)\s*هنا|"
    r"أرسل(?:ه|ها)\s*هنا|وصل(?:ه|ها)\s*هنا|maps\.google|goo\.gl/maps|"
    r"short\s*address|العنوان\s*الوطني|location|delivery|address)",
    re.UNICODE | re.IGNORECASE,
)

_REPLAY_SEMANTICS_RE = re.compile(
    r"(?:باقي\s*الخيارات|وريني\s*باقي|خيارات\s*اكثر|خيارات\s*أكثر|"
    r"more\s*options|show\s*more|مره\s*ثانيه|مرة\s*ثانية|مرة\s*اخرى|"
    r"كرر|اعد|اعيد|وريني\s*الخيارات|وريني\s*تاني|repeat|show\s*again|"
    r"list\s*again)",
    re.UNICODE | re.IGNORECASE,
)

_COMMERCE_PRODUCT_RE = re.compile(
    r"(?:منتج|عسل|سعر|بكم|اطلب|أطلب|ابي|أبي|عندكم|عندك|price|order|buy)",
    re.UNICODE | re.IGNORECASE,
)

_COMMERCE_INTERPRETATION_INTENTS = frozenset({
    "show_all_variants_or_prices",
    "ask_price_specific_variant",
    "select_list_option",
    "refer_last_product",
    "clarify_variants_natural",
})

_PAYMENT_INTERPRETATION_INTENTS = frozenset({
    "fulfillment_location_update",
})


@dataclass(frozen=True)
class StateRelevanceVerdict:
    payment_state_relevant: bool = False
    fulfillment_state_relevant: bool = False
    product_replay_relevant: bool = False
    addon_recommendation_relevant: bool = False
    stale_product_focus_relevant: bool = False
    pending_candidates_relevant: bool = False
    safe_to_resume_state: bool = True
    detected_topic_shift: bool = False
    support_listing_topic_shift: bool = False
    product_correction_topic_shift: bool = False
    product_information_topic_shift: bool = False
    relevance_confidence: float = 0.5
    active_workflows: tuple = field(default_factory=tuple)
    current_intent_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "payment_state_relevant": self.payment_state_relevant,
            "fulfillment_state_relevant": self.fulfillment_state_relevant,
            "product_replay_relevant": self.product_replay_relevant,
            "addon_recommendation_relevant": self.addon_recommendation_relevant,
            "stale_product_focus_relevant": self.stale_product_focus_relevant,
            "pending_candidates_relevant": self.pending_candidates_relevant,
            "safe_to_resume_state": self.safe_to_resume_state,
            "detected_topic_shift": self.detected_topic_shift,
            "support_listing_topic_shift": self.support_listing_topic_shift,
            "product_correction_topic_shift": self.product_correction_topic_shift,
            "product_information_topic_shift": self.product_information_topic_shift,
            "relevance_confidence": self.relevance_confidence,
            "active_workflows": list(self.active_workflows),
            "current_intent_hint": self.current_intent_hint,
        }


def _normalize(text: str) -> str:
    try:
        from ..interpret.semantic_turn_interpreter import normalize_ar  # noqa: PLC0415

        return normalize_ar(text or "")
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def _semantic_intent(ctx_or_interp: Any) -> str:
    if ctx_or_interp is None:
        return ""
    if hasattr(ctx_or_interp, "interpreted_intent"):
        return str(getattr(ctx_or_interp, "interpreted_intent", "") or "")
    if isinstance(ctx_or_interp, dict):
        return str(ctx_or_interp.get("interpreted_intent") or "")
    interp = getattr(ctx_or_interp, "semantic_interpretation", None)
    if interp is not None:
        return str(getattr(interp, "interpreted_intent", "") or "")
    slots = getattr(getattr(ctx_or_interp, "intent", None), "slots", None) or {}
    raw = slots.get("semantic_interpretation")
    if isinstance(raw, dict):
        return str(raw.get("interpreted_intent") or "")
    return ""


def _intent_name(ctx: Any) -> str:
    if ctx is None:
        return ""
    if isinstance(ctx, str):
        return ""
    intent = getattr(ctx, "intent", None)
    return str(getattr(intent, "name", "") or "")


def has_payment_semantics(message: str) -> bool:
    return bool(_PAYMENT_SEMANTICS_RE.search(message or ""))


def has_price_variant_commerce_semantics(
    message: str,
    *,
    semantic_intent: str = "",
) -> bool:
    if semantic_intent in _COMMERCE_INTERPRETATION_INTENTS:
        return True
    norm = _normalize(message)
    if not norm:
        return False
    return bool(_PRICE_VARIANT_COMMERCE_RE.search(norm))


def has_fulfillment_semantics(
    message: str,
    *,
    semantic_intent: str = "",
) -> bool:
    if semantic_intent == "fulfillment_location_update":
        return True
    return bool(_FULFILLMENT_SEMANTICS_RE.search(message or ""))


def has_explicit_replay_semantics(message: str) -> bool:
    return bool(_REPLAY_SEMANTICS_RE.search(message or ""))


def detect_topic_shift(
    message: str,
    *,
    semantic_intent: str = "",
    intent_name: str = "",
) -> bool:
    try:
        from ..commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(message or ""):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — browse defocus gate must not break relevance
        pass

    try:
        from ..order_context_gate import has_explicit_commerce_topic_change  # noqa: PLC0415

        if has_explicit_commerce_topic_change(message or ""):
            return True
    except Exception:  # noqa: BLE001
        pass

    if semantic_intent in _COMMERCE_INTERPRETATION_INTENTS:
        return True

    if intent_name in {"ask_price", "ask_product", "solution_seeking_commerce"}:
        return True

    return has_price_variant_commerce_semantics(
        message, semantic_intent=semantic_intent,
    )


def _active_workflows_from_state(state: Any) -> List[str]:
    workflows: List[str] = []
    op = getattr(state, "order_prep", None)

    if op is not None and getattr(op, "awaiting_payment_receipt", False):
        workflows.append("awaiting_payment_receipt")

    if op is not None and getattr(op, "awaiting_variant_choice", False):
        workflows.append("awaiting_variant_choice")

    missing = list(getattr(op, "missing_fields", None) or []) if op else []
    if missing:
        addr_fields = {
            "google_maps_url", "short_address_code", "address",
            "address_line", "city", "district", "street", "postal_code",
        }
        if missing and (set(missing) & addr_fields):
            workflows.append("awaiting_location")
        else:
            workflows.append("active_fulfillment")

    stage = str(getattr(state, "stage", "") or "")
    if stage in ("ordering", "deciding", "checkout") and (
        getattr(state, "current_product_focus", None)
        or (op is not None and str(getattr(op, "product_id", "") or "").strip())
    ):
        if "active_fulfillment" not in workflows:
            workflows.append("active_fulfillment")

    if list(getattr(state, "last_search_candidates", None) or []):
        workflows.append("pending_candidates")

    if getattr(state, "current_product_focus", None):
        workflows.append("stale_product_focus")

    return workflows


def validate_state_relevance(
    ctx: Any,
    *,
    message: Optional[str] = None,
    state: Optional[Any] = None,
    semantic_interpretation: Optional[Any] = None,
) -> StateRelevanceVerdict:
    """Validate current-turn relevance against persisted workflow state."""
    msg = message if message is not None else str(getattr(ctx, "message", "") or "")
    st = state if state is not None else getattr(ctx, "state", None)
    sem = semantic_interpretation
    if sem is None and ctx is not None:
        sem = getattr(ctx, "semantic_interpretation", None)

    sem_intent = _semantic_intent(sem or ctx)
    intent_name = _intent_name(ctx)

    payment_turn = has_payment_semantics(msg)
    commerce_turn = has_price_variant_commerce_semantics(
        msg, semantic_intent=sem_intent,
    ) or bool(_COMMERCE_PRODUCT_RE.search(msg or ""))
    fulfillment_turn = has_fulfillment_semantics(msg, semantic_intent=sem_intent)
    replay_turn = has_explicit_replay_semantics(msg)
    topic_shift = detect_topic_shift(
        msg, semantic_intent=sem_intent, intent_name=intent_name,
    )

    support_listing_shift = False
    try:
        from .support_listing_topic import (  # noqa: PLC0415
            collect_support_listing_context,
            detect_support_listing_topic_shift,
        )

        _extra = ""
        if ctx is not None:
            _extra = collect_support_listing_context(ctx)
        support_listing_shift = detect_support_listing_topic_shift(
            msg, extra_context=_extra,
        )
    except Exception:  # noqa: BLE001
        support_listing_shift = False

    product_correction_shift = False
    product_info_shift = False
    try:
        from .product_correction import detect_product_correction  # noqa: PLC0415

        product_correction_shift = detect_product_correction(msg)
    except Exception:  # noqa: BLE001
        product_correction_shift = False
    try:
        from .product_information_topic import (  # noqa: PLC0415
            detect_product_information_topic_shift,
            recent_unresolved_product_information,
        )

        _history = list(getattr(ctx, "history", None) or [])
        product_info_shift = (
            detect_product_information_topic_shift(msg)
            or recent_unresolved_product_information(
                _history,
                current_message=msg,
            )
        )
    except Exception:  # noqa: BLE001
        product_info_shift = False

    _global_browse = False
    try:
        from ..commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        _global_browse = global_availability_browse_requested(msg)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — browse gate fallback treats turn as focused
        _global_browse = False

    op = getattr(st, "order_prep", None)
    awaiting_payment = bool(
        op is not None and getattr(op, "awaiting_payment_receipt", False)
    )

    payment_relevant = payment_turn
    if awaiting_payment and commerce_turn and not payment_turn:
        payment_relevant = False
    elif awaiting_payment and payment_turn:
        payment_relevant = True
    elif awaiting_payment and not commerce_turn and not payment_turn:
        payment_relevant = False

    fulfillment_relevant = fulfillment_turn
    if commerce_turn and not fulfillment_turn:
        fulfillment_relevant = False
    if support_listing_shift:
        fulfillment_relevant = False
    if product_correction_shift or product_info_shift:
        fulfillment_relevant = False

    pending_candidates_relevant = (
        replay_turn
        or intent_name == "pick_list_item"
        or sem_intent == "select_list_option"
        or (commerce_turn and not topic_shift)
    )
    if topic_shift and not replay_turn and intent_name != "pick_list_item":
        pending_candidates_relevant = False

    product_replay_relevant = replay_turn

    stale_focus_relevant = (
        (commerce_turn or fulfillment_turn or payment_turn)
        and not (awaiting_payment and commerce_turn and not payment_turn)
        and not _global_browse
        and not support_listing_shift
        and not product_correction_shift
        and not product_info_shift
    )

    addon_relevant = (
        intent_name in {"start_order", "pay_now", "ask_product"}
        and not topic_shift
        and not awaiting_payment
    )

    active = tuple(_active_workflows_from_state(st) if st is not None else ())

    safe = True
    if awaiting_payment and not payment_relevant and commerce_turn:
        safe = False
    if "active_fulfillment" in active and fulfillment_turn is False and commerce_turn:
        safe = False
    if "pending_candidates" in active and not pending_candidates_relevant and topic_shift:
        safe = False

    confidence = 0.5
    if sem_intent:
        confidence = max(confidence, 0.82)
    if payment_turn or fulfillment_turn or replay_turn:
        confidence = max(confidence, 0.78)
    if topic_shift:
        confidence = max(confidence, 0.75)
    if support_listing_shift:
        confidence = max(confidence, 0.84)
    if product_correction_shift:
        confidence = max(confidence, 0.86)
    if product_info_shift:
        confidence = max(confidence, 0.85)

    return StateRelevanceVerdict(
        payment_state_relevant=payment_relevant,
        fulfillment_state_relevant=fulfillment_relevant,
        product_replay_relevant=product_replay_relevant,
        addon_recommendation_relevant=addon_relevant,
        stale_product_focus_relevant=stale_focus_relevant,
        pending_candidates_relevant=pending_candidates_relevant,
        safe_to_resume_state=safe,
        detected_topic_shift=topic_shift,
        support_listing_topic_shift=support_listing_shift,
        product_correction_topic_shift=product_correction_shift,
        product_information_topic_shift=product_info_shift,
        relevance_confidence=confidence,
        active_workflows=active,
        current_intent_hint=sem_intent or intent_name,
    )


def validate_state_relevance_from_summary(
    *,
    message: str,
    summary: Dict[str, Any],
    semantic_intent: str = "",
) -> StateRelevanceVerdict:
    """Lightweight validator for webhook / order_flow paths without BrainContext."""

    class _StubState:
        def __init__(self, s: Dict[str, Any]):
            self.current_product_focus = (
                {"title": s.get("selected_product")} if s.get("selected_product") else None
            )
            self.stage = s.get("stage") or ""
            self.last_search_candidates = list(s.get("last_search_candidates") or [])

            class _OP:
                awaiting_payment_receipt = bool(s.get("awaiting_payment_receipt"))
                awaiting_variant_choice = False
                missing_fields = list(s.get("missing_fields") or [])
                product_id = str(s.get("product_id") or s.get("selected_product_id") or "")
                payment_receipt_received = bool(s.get("payment_receipt_received"))
                order_creation_status = str(s.get("order_creation_status") or "")
                salla_order_id = str(s.get("salla_order_id") or "")
                salla_failure_count = int(s.get("salla_failure_count") or 0)
                last_order_failed = bool(s.get("last_order_failed"))

            self.order_prep = _OP()

    class _StubCtx:
        pass

    stub = _StubCtx()
    stub.message = message or ""
    stub.state = _StubState(summary)
    stub.intent = type("I", (), {"name": "", "slots": {}})()
    stub.semantic_interpretation = (
        {"interpreted_intent": semantic_intent} if semantic_intent else None
    )

    return validate_state_relevance(
        stub,
        message=message,
        semantic_interpretation=(
            {"interpreted_intent": semantic_intent} if semantic_intent else None
        ),
    )


def should_block_workflow_resume(
    workflow: str,
    verdict: StateRelevanceVerdict,
) -> bool:
    """True when stored *workflow* must NOT resume on this turn."""
    mapping = {
        "awaiting_payment_receipt": not verdict.payment_state_relevant,
        "payment_flow": not verdict.payment_state_relevant,
        "awaiting_transfer": not verdict.payment_state_relevant,
        "active_fulfillment": (
            verdict.support_listing_topic_shift
            or verdict.product_correction_topic_shift
            or verdict.product_information_topic_shift
            or (
                not verdict.fulfillment_state_relevant
                and verdict.detected_topic_shift
            )
        ),
        "awaiting_location": (
            verdict.support_listing_topic_shift
            or verdict.product_correction_topic_shift
            or verdict.product_information_topic_shift
            or (
                not verdict.fulfillment_state_relevant
                and verdict.detected_topic_shift
            )
        ),
        "pending_candidates": (
            not verdict.pending_candidates_relevant
            and verdict.detected_topic_shift
        ),
        "product_replay": not verdict.product_replay_relevant,
        "show_more": not verdict.product_replay_relevant,
        "addon_recommendation": not verdict.addon_recommendation_relevant,
        "stale_product_focus": (
            verdict.support_listing_topic_shift
            or verdict.product_correction_topic_shift
            or verdict.product_information_topic_shift
            or (
                not verdict.stale_product_focus_relevant
                and verdict.detected_topic_shift
            )
        ),
    }
    return bool(mapping.get(workflow, False))


_CURRENT_INTENT_OUTRANKS_ORDERING = frozenset({
    "ask_store_info",
    "online_store_inquiry",
    "ask_location",
    "talk_to_human",
    "ask_owner_contact",
    "ask_shipping",
    "track_order",
    "employee_not_responding",
    "who_are_you",
    "greeting",
    "social",
    "platform_inquiry",
    "persona_interaction",
    "order_history_count",
    "latest_order_summary",
    "order_reference_list",
})

_CHECKOUT_CONTINUATION_SLOT_KEYS = frozenset({
    "customer_first_name",
    "customer_last_name",
    "customer_name",
    "full_name",
    "city",
    "short_address_code",
    "google_maps_url",
    "location_url",
    "address",
    "address_line",
    "street",
    "district",
    "postal_code",
    "zip_code",
    "building_number",
    "additional_number",
    "latitude",
    "longitude",
    "quantity",
    "phone",
    "shipping_phone",
})


def _intent_slot_keys(ctx: Any) -> frozenset:
    intent = getattr(ctx, "intent", None)
    slots = getattr(intent, "slots", None)
    if isinstance(slots, dict):
        return frozenset(str(k) for k in slots.keys() if k)
    return frozenset()


def current_intent_outranks_ordering_safety_net(ctx: Any) -> bool:
    """True when stale ordering/checkout state must not own this turn.

    Current Brain intent outranks leftover ``stage=ordering`` /
    ``pending_action``. Checkout-slot fulfillment and product-order
    intents may still continue the funnel.
    """
    intent_name = _intent_name(ctx)
    if intent_name in _CURRENT_INTENT_OUTRANKS_ORDERING:
        return True
    if intent_name in {"general", ""} and (
        _intent_slot_keys(ctx) & _CHECKOUT_CONTINUATION_SLOT_KEYS
    ):
        return False
    try:
        from ..commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            message_fulfills_checkout_slot,
        )

        order_prep = getattr(getattr(ctx, "state", None), "order_prep", None)
        if message_fulfills_checkout_slot(
            str(getattr(ctx, "message", "") or ""),
            order_prep=order_prep,
        ):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — checkout-slot probe is optional
        pass
    if intent_name in {"general", ""}:
        return True
    return False


def log_state_relevance(
    *,
    tenant_id: Any = None,
    verdict: StateRelevanceVerdict,
    state_name: str = "",
    relevant: Optional[bool] = None,
    reason: str = "",
) -> None:
    try:
        logger.info(
            "[STATE_RELEVANCE] tenant=%s state=%s relevant=%s reason=%s "
            "topic_shift=%s support_listing_shift=%s product_correction=%s "
            "product_information=%s intent_hint=%s "
            "confidence=%.2f active=%s",
            tenant_id,
            state_name or "-",
            str(relevant if relevant is not None else verdict.safe_to_resume_state).lower(),
            reason or "-",
            str(verdict.detected_topic_shift).lower(),
            str(verdict.support_listing_topic_shift).lower(),
            str(verdict.product_correction_topic_shift).lower(),
            str(verdict.product_information_topic_shift).lower(),
            verdict.current_intent_hint or "-",
            float(verdict.relevance_confidence or 0.0),
            ",".join(verdict.active_workflows) or "-",
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — logging must not raise
        pass


def log_state_resurrection_blocked(
    *,
    tenant_id: Any = None,
    blocked_state: str,
    reason: str,
    preview: str = "",
    intent_hint: str = "",
) -> None:
    try:
        logger.info(
            "[STATE_RESURRECTION_BLOCKED] tenant=%s blocked_state=%s "
            "reason=%s intent_hint=%s preview=%r",
            tenant_id,
            blocked_state,
            reason,
            intent_hint or "-",
            (preview or "")[:80],
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — logging must not raise
        pass


__all__ = [
    "StateRelevanceVerdict",
    "detect_topic_shift",
    "has_explicit_replay_semantics",
    "has_fulfillment_semantics",
    "has_payment_semantics",
    "has_price_variant_commerce_semantics",
    "log_state_relevance",
    "log_state_resurrection_blocked",
    "should_block_workflow_resume",
    "validate_state_relevance",
    "validate_state_relevance_from_summary",
    "current_intent_outranks_ordering_safety_net",
]
