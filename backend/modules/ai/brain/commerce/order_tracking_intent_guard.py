"""
order_tracking_intent_guard.py
──────────────────────────────
Platform-wide guard: existing-order tracking follow-ups must not drift
into product browse, availability rewrite, or generic escalation stubs.

Layer 1 — intent boost (classifier + decision engine)
Layer 2 — availability rewrite exempt (via availability_guard_policy)
Layer 3 — staff escalation stub replacement (via staff_escalation_truth_guard)
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

from ..decision.actions import ACTION_LLM_REPLY, ACTION_TRACK_ORDER
from ..types import Decision, INTENT_TRACK_ORDER, Intent

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s.lower()).strip()


# Hypothetical / pre-order shipping — NOT an existing-order follow-up.
_PRE_ORDER_MARKERS_RE = re.compile(
    r"(?:"
    r"(?:اذا|إذا|لو|لما|قبل\s*(?:ما\s*)?(?:اطلب|اطلبي|اشتري|الطلب))"
    r"|"
    r"(?:if\s+i\s+order|before\s+i\s+order|when\s+i\s+order)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Pure browse — no existing-order tracking context.
_PURE_BROWSE_RE = re.compile(
    r"^(?:"
    r"(?:ا|أ)?(?:بي|بغى|بغي|ريد|ودي|بدي)\s+(?:عسل|طلح|سمر|سدر|شمع|حلاو|منتج|\S+)"
    r"|"
    r"(?:وش|ايش|ايه|what)\s*(?:ال)?(?:خيارات|خيار|انواع|أنواع|options|choices)"
    r"|"
    r"(?:وش|ايش|ايه)\s+(?:المتوفر|الموجود|عندكم)"
    r")(?:\s*[\?؟!.]*)?$",
    re.UNICODE | re.IGNORECASE,
)

# General shipping-duration asks — stay ask_shipping unless order evidence exists.
_GENERAL_SHIPPING_DURATION_RE = re.compile(
    r"(?:"
    r"^متي\s+(?:يوصل|توصل|يجي)\s+الطلب(?:\s*[\?؟!.]*)?$|"
    r"^متي\s+(?:يوصل|توصل|يجي)\s+الطلب(?:يه)?(?:\s*[\?؟!.]*)?$|"
    r"متي\s+(?:يوصل|توصل|يجي)\s+الطلب(?:يه)?\s+(?:"
    r"اذا|إذا|لو|عاده|عادة|غالبا|غالباً|عاده|"
    r"لل(?:رياض|جده|جدة|دمام|طائف|مدين|مكه|احساء)|"
    r"بعد\s+(?:ال)?(?:طلب|طلبيه)|اليوم|الحين|الان|الآن"
    r")"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Layer 2 — phrases that must NEVER become catalog product labels.
# Independent of intent routing (ask_shipping vs track_order).
_SHIPPING_TRACKING_NON_PRODUCT_PHRASES_RAW = (
    "متى يوصل الطلب",
    "متى توصل الطلب",
    "متى يجي الطلب",
    "متى توصل الطلبية",
    "وين طلبي",
    "فين طلبي",
    "حالة الطلب",
    "رقم التتبع",
    "رابط التتبع",
    "الشحنة وينها",
    "وين الشحنة",
    "فين الشحنة",
    "تتبع الطلب",
    "order status",
    "tracking number",
    "track my order",
    "where is my order",
)
_SHIPPING_TRACKING_NON_PRODUCT_PHRASES = tuple(
    _norm_ar(p) for p in _SHIPPING_TRACKING_NON_PRODUCT_PHRASES_RAW
)

# Strong existing-order follow-up markers — Layer 1 track_order routing.
_EXPLICIT_TRACKING_PHRASES_RAW = (
    "وين طلبي",
    "فين طلبي",
    "حالة الطلب",
    "رقم التتبع",
    "رابط التتبع",
    "الشحنة وينها",
    "وين الشحنة",
    "فين الشحنة",
    "تتبع الطلب",
    "order status",
    "tracking number",
    "track my order",
    "where is my order",
)
_EXPLICIT_TRACKING_PHRASES = tuple(_norm_ar(p) for p in _EXPLICIT_TRACKING_PHRASES_RAW)

_EXISTING_ORDER_MESSAGE_RE = re.compile(
    r"(?:"
    r"طلبي|طلبيتي|شحنتي|"
    r"عندي\s+طلب|"
    r"طلبت\s+(?:قبل|امس|البارح|الاحد|يوم|اسبوع|اسبوعين)|"
    r"سويت\s*طلب|عملت\s*طلب|قدمت\s*طلب"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_ANCHOR_RE = re.compile(
    r"(?:"
    r"طلبي|طلبيتي|شحنتي|الشحنه|الشحنة|"
    r"رقم\s*الطلب|رقم\s*التتبع|رابط\s*التتبع|التتبع|tracking"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_STATUS_QUESTION_RE = re.compile(
    r"(?:"
    r"متي|وين|فين|اين|أين|حالة|status|"
    r"(?:هل\s+)?(?:يوصل|وصل|وصلت|توصل|تشحن|شحن)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PAST_ORDER_TRACKING_RE = re.compile(
    r"(?:"
    r"طلبت(?:\s+طلب)?|سويت\s*طلب|عملت\s*طلب|قدمت\s*طلب"
    r").{0,50}(?:"
    r"متي\s*(?:يوصل|توصل|يجي|يصل)|"
    r"(?:وين|فين|اين)\s*(?:طلب|الشحن|الشحنه)|"
    r"حالة\s*(?:ال)?طلب|"
    r"(?:و)?اب(?:ي|غى|غي)\s*اعرف\s*متي"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PLACED_ORDER_DELIVERY_RE = re.compile(
    r"طلبت.{0,20}(?:"
    r"متي\s*(?:يوصل|توصل|يجي|يصل)|"
    r"(?:و)?اب(?:ي|غى|غي)\s*اعرف\s*متي\s*(?:يوصل|توصل|يجي|يصل)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PRODUCT_SHIPPING_TIMING_RE = re.compile(
    r"متي\s*(?:يوصل|توصل|يجي|يصل|تاخذ|تاخذون|يستغرق)",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_PRODUCT_HINT_RE = re.compile(
    r"(?:"
    r"عسل|طلح|سمر|سدر|شمع|حلاو|كيلو|جرام|منتج|صنف|نوع"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BARE_ORDER_REF_RE = re.compile(r"^\d{6,12}$")
_LABELED_ORDER_REF_RE = re.compile(
    r"(?:طلب(?:ك|كم)?\s*رقم|رقم\s*(?:ال)?طلب(?:ك|كم)?|order\s*(?:#|number)?)\s*[:#]?\s*(\d{6,12})",
    re.IGNORECASE | re.UNICODE,
)

_ORDER_SUPPORT_TOPIC_RE = re.compile(
    r"(?:"
    r"شحن|توصيل|شحنه|الشحنه|الشحنة|"
    r"وصل|يوصل|توصل|تأخر|متأخر|"
    r"طلبي|طلبيتي|محتوى|محتويات|الطلب\s*فيه|"
    r"مشكله|مشكلة|خطأ|غلط|"
    r"carrier|shipping|delivery|shipment|order\s+problem|track"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_EXPLICIT_NEW_SHOP_RE = re.compile(
    r"(?:"
    r"أ?بي\s*أ?طلب|اب(?:ي|غ(?:ى|a)?)\s*أ?طلب|"
    r"أ?ض(?:ف|يف)|اض(?:ف|يف)|"
    r"اشتري|أشتري|order\s+now|buy\s+now|add\s+to\s+cart"
    r")",
    re.UNICODE | re.IGNORECASE,
)


# Post-order shipping policy / carrier questions — defer to brain (ACTION_LLM_REPLY).
_POST_ORDER_SHIPPING_BRAIN_DEFER_RE = re.compile(
    r"(?:"
    r"(?:اي|ايه|أي|which)\s*(?:فرع|branch)|"
    r"(?:سمسا|smsa|aramex|ارامكس|\bdhl\b)|"
    r"(?:ارسل|أرسل|رسل|شحن|شحنت).{0,30}(?:فرع|branch|شركة|carrier)|"
    r"(?:فرع|branch).{0,20}(?:سمسا|smsa|aramex|ارامكس)|"
    r"بكم\s*(?:ال)?(?:شحن|توصيل)|"
    r"(?:مدة|كم\s+يوم).{0,15}(?:ال)?(?:شحن|توصيل)|"
    r"(?:وش|كيف|شلون).{0,15}(?:ال)?(?:شحن|توصيل|توصل)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def extract_bare_order_reference(message: str) -> str:
    """Return a standalone 6–12 digit order reference when the inbound is only digits."""
    raw = re.sub(r"\s+", "", (message or "").strip())
    if _BARE_ORDER_REF_RE.match(raw):
        return raw
    return ""


def extract_order_reference_from_history(
    history: Optional[List[Any]],
) -> str:
    """Pull the most recent customer-supplied order reference from conversation history."""
    if not history:
        return ""
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("in", "inbound", ""):
                continue
            body = str((turn or {}).get("body") or "").strip()
            if not body:
                continue
            labeled = _LABELED_ORDER_REF_RE.search(body)
            if labeled:
                return labeled.group(1)
            bare = extract_bare_order_reference(body)
            if bare:
                return bare
    except Exception:  # noqa: BLE001
        return ""
    return ""


ORDER_REF_CONTINUITY_WINDOW = 8


def _brain_state_field(state: Any, name: str, default: Any = None) -> Any:
    """Read brain/conversation state fields from dict or object uniformly."""
    if state is None:
        return default
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def _order_prep_field(state: Any, name: str, default: Any = None) -> Any:
    """Read order_prep fields from dict-wrapped or object-wrapped state."""
    op = _brain_state_field(state, "order_prep")
    if op is None:
        return default
    if isinstance(op, dict):
        return op.get(name, default)
    return getattr(op, name, default)


def _inbound_customer_turns(history: Optional[List[Any]]) -> List[Any]:
    """Inbound customer turns with non-empty semantic body."""
    turns: List[Any] = []
    for turn in history or []:
        direction = str((turn or {}).get("direction") or "").lower()
        if direction not in ("in", "inbound", ""):
            continue
        body = str((turn or {}).get("body") or "").strip()
        if not body:
            continue
        turns.append(turn)
    return turns


def is_order_reference_continuity_active(
    history: Optional[List[Any]],
    *,
    window: int = ORDER_REF_CONTINUITY_WINDOW,
) -> bool:
    """True when the latest customer-supplied ref is in the last N inbound turns."""
    ref = extract_order_reference_from_history(history)
    if not ref:
        return False
    tail = _inbound_customer_turns(history)[-window:]
    for turn in tail:
        body = str((turn or {}).get("body") or "").strip()
        labeled = _LABELED_ORDER_REF_RE.search(body)
        if labeled and labeled.group(1) == ref:
            return True
        if extract_bare_order_reference(body) == ref:
            return True
    return False


def has_order_reference_support_context(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """History ref or structured verified order — not stale draft alone."""
    if extract_order_reference_from_history(history):
        return True
    bundle = commerce_bundle if isinstance(commerce_bundle, dict) else {}
    ctx_obj = bundle.get("active_order_context") or {}
    if isinstance(ctx_obj, dict):
        order_id = str(ctx_obj.get("order_id") or ctx_obj.get("reference") or "").strip()
        status = str(ctx_obj.get("order_status") or ctx_obj.get("status") or "").strip().lower()
        if order_id and status and status not in {"pending_customer_info", "draft"}:
            return True
    return False


def is_order_support_topic_reset(message: str) -> bool:
    """Explicit new-order or commerce-topic switch blocks support ownership."""
    semantic = str(message or "").strip()
    if not semantic:
        return False
    if _EXPLICIT_NEW_SHOP_RE.search(_norm_ar(semantic)):
        return True
    try:
        from modules.ai.order_context_gate import has_explicit_commerce_topic_change  # noqa: PLC0415

        return has_explicit_commerce_topic_change(semantic)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — topic-reset probe must not block routing
        return False


def is_order_support_continuity_allowed(
    message: str,
    history: Optional[List[Any]],
    *,
    inbound_metadata: Optional[dict] = None,
) -> bool:
    """Bounded continuity gate shared by ownership and continuity decision paths."""
    semantic = str(message or "").strip()
    if extract_bare_order_reference(semantic):
        return True
    if not extract_order_reference_from_history(history):
        return False
    try:
        from modules.ai.media.routing_guard import (  # noqa: PLC0415
            is_audio_without_trusted_transcript,
        )

        meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
        if not semantic and is_audio_without_trusted_transcript(
            meta,
            semantic_message=semantic,
            inbound_normalized_type=str(
                meta.get("inbound_normalized_type")
                or meta.get("type")
                or (meta.get("normalized_inbound") or {}).get("source_type")
                or ""
            ),
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — unclear-audio probe must not block continuity
        pass
    return is_order_reference_continuity_active(history)


def has_pending_order_reference_evidence(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """Verified order evidence or an unresolved customer-supplied order reference."""
    if has_existing_order_evidence(
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return True
    return bool(extract_order_reference_from_history(history))


def _is_stale_unverified_draft_evidence(
    *,
    state: Any = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """True when persisted evidence is an active draft/checkout, not a verified placed order."""
    if state is not None:
        op = _brain_state_field(state, "order_prep")
        if op is not None:
            if bool(_order_prep_field(state, "payment_receipt_received", False)):
                return False
            if str(_order_prep_field(state, "order_creation_status", "") or "").strip().lower() == "created":
                return True
            if str(_order_prep_field(state, "order_status", "") or "").strip().lower() in {
                "pending_customer_info",
                "draft",
            }:
                return True
        if str(_brain_state_field(state, "draft_order_id", "") or "").strip():
            return True

    bundle = commerce_bundle if isinstance(commerce_bundle, dict) else {}
    ctx_obj = bundle.get("active_order_context") or {}
    if isinstance(ctx_obj, dict):
        status = str(ctx_obj.get("order_status") or ctx_obj.get("status") or "").strip().lower()
        if status in {"pending_customer_info", "draft"}:
            return True
    return False


def is_order_support_operational_follow_up(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """Operational order-support follow-up when a recent order reference exists."""
    if not has_pending_order_reference_evidence(
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return False
    raw = (message or "").strip()
    if not raw:
        return False
    if extract_bare_order_reference(raw):
        return True
    norm = _norm_ar(raw)
    if is_order_tracking_follow_up(
        raw,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return True
    if is_post_order_shipping_brain_defer(raw):
        return True
    if _ORDER_SUPPORT_TOPIC_RE.search(norm):
        return True
    if _EXISTING_ORDER_MESSAGE_RE.search(norm):
        return True
    return False


def build_order_support_follow_up_args(
    *,
    message: str = "",
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
    order_verified: bool = False,
    unclear_audio: bool = False,
) -> Dict[str, Any]:
    """LLM args for existing-order support when lookup may still be unresolved."""
    bundle = commerce_bundle if isinstance(commerce_bundle, dict) else {}
    order_ref = extract_order_reference_from_history(history) or extract_bare_order_reference(message)
    status = ""

    ctx_obj = bundle.get("active_order_context") or {}
    if isinstance(ctx_obj, dict):
        struct_ref = str(ctx_obj.get("order_id") or ctx_obj.get("reference") or "").strip()
        struct_status = str(ctx_obj.get("order_status") or ctx_obj.get("status") or "").strip()
        if struct_ref:
            order_verified = True
            order_ref = order_ref or struct_ref
            status = struct_status

    if not order_verified and not order_ref:
        try:
            from core.active_order_context import resolve_order_reference  # noqa: PLC0415

            resolved_ref, mode = resolve_order_reference(
                commerce_bundle=bundle,
                state=state,
                history=history,
            )
            if resolved_ref and mode == "structured":
                order_verified = True
                order_ref = resolved_ref
        except Exception:  # noqa: BLE001  # noqa: silent-ok — order ref resolve is best-effort
            pass

    if order_verified and not status:
        try:
            from core.active_order_context import resolve_order_status  # noqa: PLC0415

            resolved_status, mode = resolve_order_status(
                commerce_bundle=bundle,
                state=state,
                history=history,
            )
            if resolved_status and mode == "structured":
                status = resolved_status
        except Exception:  # noqa: BLE001  # noqa: silent-ok — order status resolve is best-effort
            pass

    if not order_verified:
        status = ""
    response_goal = _base_existing_order_support_response_goal()
    if unclear_audio:
        response_goal += (
            " The voice note transcript is unavailable; acknowledge that "
            "gently and ask the customer to repeat their question in text "
            "or provide the minimum order identifier needed."
        )
    return {
        "topic": "existing_order_support",
        "order_reference": order_ref,
        "order_verified": bool(order_verified),
        "order_status": status,
        "unclear_audio": bool(unclear_audio),
        "response_goal": response_goal,
    }


_SUPPORT_CHANNEL_OWNERSHIP = (
    "support_channel_ownership — this WhatsApp thread is already the merchant's "
    "active customer-support channel; the customer reached the store by messaging "
    "here, so there is no separate support entry point they still need to open. "
    "Continue the support workflow inside this same conversation — reassurance, "
    "clarification, and follow-up all happen here. When more human investigation "
    "is needed, explain naturally that the merchant team will keep following up "
    "through this WhatsApp chat. Another human or contact path is relevant only "
    "when the platform issues an authenticated staff handoff or a configured "
    "contact action already present in Facts."
)


def _base_existing_order_support_response_goal() -> str:
    return (
        "existing_order_support — reply in natural Saudi Arabic about the "
        "customer's existing order using only known facts. The customer is "
        "already in the merchant WhatsApp support channel in this thread. "
        "If order_verified is false, say the reference is not verified yet and "
        "ask only for the minimum identifier needed. Do NOT promise carrier "
        "changes, discounts, or mutations. Do NOT fabricate tracking, carrier, "
        "or order-status facts without evidence. Do NOT open catalog or restart checkout."
    )


def _base_shipping_post_order_response_goal() -> str:
    return (
        "shipping_post_order — reply in natural Saudi Arabic about the "
        "customer's order, shipping, or delivery concern using only known "
        "facts from context. The customer is already in the merchant WhatsApp "
        "support channel in this thread. Do NOT fabricate tracking URLs, "
        "carrier names, or delivery ETAs without evidence. "
        "Do NOT open catalog or restart checkout."
    )


def compose_order_support_response_goal_for_decision(
    args: Optional[Dict[str, Any]],
) -> str:
    """Merge structured order-support goals for compose (constraints only)."""
    payload = dict(args or {})
    topic = str(payload.get("topic") or "").strip()
    base = str(payload.get("response_goal") or "").strip()
    if not base:
        if topic == "shipping_post_order":
            base = _base_shipping_post_order_response_goal()
        else:
            base = _base_existing_order_support_response_goal()

    lines = [base, _SUPPORT_CHANNEL_OWNERSHIP]
    order_ref = str(payload.get("order_reference") or "").strip()
    if order_ref:
        lines.append(f"order_reference={order_ref}")
    if "order_verified" in payload:
        lines.append(f"order_verified={bool(payload.get('order_verified'))}")
    status = str(payload.get("order_status") or "").strip()
    if status:
        lines.append(f"order_status={status}")
    if payload.get("unclear_audio"):
        lines.append(
            "unclear_audio=true — voice transcript may be missing; ask gently "
            "to repeat in text or provide the minimum order identifier if needed."
        )
    return " | ".join(lines)


def try_order_reference_continuity_decision(ctx: Any) -> Optional[Decision]:
    """Route bare/repeated order references and pending-ref follow-ups before social/catalog."""
    message = str(getattr(ctx, "message", "") or "")
    history = getattr(ctx, "history", None)
    state = getattr(ctx, "state", None)
    commerce_bundle = getattr(ctx, "commerce_bundle", None) or {}
    inbound_metadata: dict = {}
    try:
        profile = getattr(ctx, "profile", None) or {}
        if isinstance(profile, dict):
            inbound_metadata = dict(profile.get("inbound_metadata") or {})
    except Exception:  # noqa: BLE001  # noqa: silent-ok — inbound metadata probe is best-effort
        inbound_metadata = {}

    bare_ref = extract_bare_order_reference(message)
    if bare_ref:
        return Decision(
            action=ACTION_TRACK_ORDER,
            args={"order_number": bare_ref, "order_id": bare_ref},
            reason="bare order reference — existing-order lookup",
            confidence=0.98,
        )

    pending_ref = extract_order_reference_from_history(history)
    if not has_order_reference_support_context(
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return None

    if is_order_support_topic_reset(message):
        return None

    if not is_order_support_continuity_allowed(
        message,
        history,
        inbound_metadata=inbound_metadata,
    ):
        return None

    try:
        from modules.ai.media.routing_guard import (  # noqa: PLC0415
            is_audio_without_trusted_transcript,
        )

        if not message.strip() and is_audio_without_trusted_transcript(
            inbound_metadata,
            semantic_message=message,
            inbound_normalized_type=str(
                inbound_metadata.get("normalized_type")
                or inbound_metadata.get("type")
                or ""
            ),
        ):
            return Decision(
                action=ACTION_LLM_REPLY,
                args=build_order_support_follow_up_args(
                    message=message,
                    state=state,
                    history=history,
                    commerce_bundle=commerce_bundle,
                    unclear_audio=True,
                ),
                reason="pending order reference — unclear audio support clarification",
                confidence=0.91,
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — unclear-audio probe must not block continuity
        pass

    try:
        from modules.ai.brain.intent.link_disambiguation import (  # noqa: PLC0415
            should_use_generative_tracking_follow_up,
        )

        if should_use_generative_tracking_follow_up(
            message,
            history=history,
            state=state,
            commerce_bundle=commerce_bundle,
        ):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — tracking follow-up defer is best-effort
        pass

    if is_order_support_operational_follow_up(
        message,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        if is_post_order_shipping_brain_defer(message) and has_existing_order_evidence(
            state=state,
            history=history,
            commerce_bundle=commerce_bundle,
        ):
            customer_ref = extract_order_reference_from_history(history)
            if not (
                customer_ref
                and _is_stale_unverified_draft_evidence(
                    state=state,
                    commerce_bundle=commerce_bundle,
                )
            ):
                return None
        if is_post_order_shipping_brain_defer(message):
            return Decision(
                action=ACTION_LLM_REPLY,
                args=build_order_support_follow_up_args(
                    message=message,
                    state=state,
                    history=history,
                    commerce_bundle=commerce_bundle,
                ),
                reason="pending order reference — shipping/carrier support follow-up",
                confidence=0.94,
            )
        if is_explicit_order_tracking_request(
            message,
            state=state,
            history=history,
            commerce_bundle=commerce_bundle,
            inbound_metadata=inbound_metadata,
        ):
            ref = pending_ref or extract_bare_order_reference(message)
            return Decision(
                action=ACTION_TRACK_ORDER,
                args={"order_number": ref, "order_id": ref},
                reason="pending order reference — tracking follow-up",
                confidence=0.96,
            )
        return Decision(
            action=ACTION_LLM_REPLY,
            args=build_order_support_follow_up_args(
                message=message,
                state=state,
                history=history,
                commerce_bundle=commerce_bundle,
            ),
            reason="pending order reference — operational follow-up",
            confidence=0.92,
        )

    if pending_ref and not _EXPLICIT_NEW_SHOP_RE.search(_norm_ar(message)):
        norm = _norm_ar(message)
        if _EXISTING_ORDER_MESSAGE_RE.search(norm) or _ORDER_SUPPORT_TOPIC_RE.search(norm):
            if is_order_support_continuity_allowed(
                message,
                history,
                inbound_metadata=inbound_metadata,
            ):
                return Decision(
                    action=ACTION_LLM_REPLY,
                    args=build_order_support_follow_up_args(
                        message=message,
                        state=state,
                        history=history,
                        commerce_bundle=commerce_bundle,
                    ),
                    reason="pending order reference — order clarification",
                    confidence=0.9,
                )
    return None


def is_general_shipping_duration_inquiry(message: str) -> bool:
    """Policy shipping timing — e.g. bare «متى يوصل الطلب» without order context."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    return bool(_GENERAL_SHIPPING_DURATION_RE.search(norm))


def is_shipping_tracking_non_product_label(message: str) -> bool:
    """
    Layer 2 core — shipping/tracking inbound must never become a product label.

    Applies regardless of upstream intent (ask_shipping, ask_product, etc.).
    """
    norm = _norm_ar(message or "")
    if not norm:
        return False
    if any(phrase in norm for phrase in _SHIPPING_TRACKING_NON_PRODUCT_PHRASES):
        return True
    if is_general_shipping_duration_inquiry(message):
        return True
    if _ORDER_ANCHOR_RE.search(norm) and _STATUS_QUESTION_RE.search(norm):
        return True
    if _EXISTING_ORDER_MESSAGE_RE.search(norm):
        return True
    if _PAST_ORDER_TRACKING_RE.search(norm):
        return True
    if _PLACED_ORDER_DELIVERY_RE.search(norm):
        return True
    return False


def has_existing_order_evidence(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """True when persisted/session evidence shows the customer has an active order."""
    try:
        from core.order_creation_evidence import (  # noqa: PLC0415
            recent_outbound_claims_order_creating,
            resolve_order_creation_evidence,
        )

        evidence = resolve_order_creation_evidence(state=state)
        if evidence.can_claim_created() or evidence.can_claim_creating():
            return True
        if recent_outbound_claims_order_creating(history):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence scan is best-effort
        pass

    if state is not None:
        if str(_brain_state_field(state, "draft_order_id", "") or "").strip():
            return True
        if str(_order_prep_field(state, "salla_order_id", "") or "").strip():
            return True
        if str(_order_prep_field(state, "order_status", "") or "").strip():
            return True
        if _order_prep_field(state, "payment_receipt_received", False):
            return True

    bundle = commerce_bundle if isinstance(commerce_bundle, dict) else {}
    ctx_obj = bundle.get("active_order_context") or {}
    if isinstance(ctx_obj, dict) and any(
        str(ctx_obj.get(k) or "").strip()
        for k in ("order_id", "salla_order_id", "reference", "tracking_number")
    ):
        return True
    return False


def is_pre_order_shipping_inquiry(message: str) -> bool:
    """Hypothetical shipping timing — e.g. «متى يوصل عسل الطلح إذا طلبته؟»."""
    norm = _norm_ar(message)
    if not norm:
        return False
    if _PRE_ORDER_MARKERS_RE.search(norm):
        return True
    if (
        _PRODUCT_SHIPPING_TIMING_RE.search(norm)
        and _CATALOG_PRODUCT_HINT_RE.search(norm)
        and not _ORDER_ANCHOR_RE.search(norm)
        and not _PAST_ORDER_TRACKING_RE.search(norm)
    ):
        return True
    return False


def is_order_tracking_follow_up(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """
    True when the customer is asking about an existing order/shipment,
    not browsing catalog or asking general shipping policy.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if extract_bare_order_reference(raw):
        return True
    if is_pre_order_shipping_inquiry(raw):
        return False
    norm = _norm_ar(raw)
    if _PURE_BROWSE_RE.search(norm):
        return False

    order_evidence = has_pending_order_reference_evidence(
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    )

    if is_general_shipping_duration_inquiry(raw):
        return order_evidence

    if any(phrase in norm for phrase in _EXPLICIT_TRACKING_PHRASES):
        return True
    if _EXISTING_ORDER_MESSAGE_RE.search(norm):
        return True
    if _PAST_ORDER_TRACKING_RE.search(norm):
        return True
    if _PLACED_ORDER_DELIVERY_RE.search(norm):
        return True
    if _ORDER_ANCHOR_RE.search(norm) and _STATUS_QUESTION_RE.search(norm):
        return True
    return False


def is_post_order_shipping_brain_defer(message: str) -> bool:
    """Paid/post-order shipping policy questions — brain path, not ACTION_TRACK_ORDER."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    return bool(_POST_ORDER_SHIPPING_BRAIN_DEFER_RE.search(norm))


_POST_ORDER_STATUSES = frozenset({
    "under_review",
    "processing",
    "preparing",
    "ready",
    "shipped",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "payment_pending",
})

# Origin / carrier / delivery-time of THIS customer's shipment — not merchant
# capability ("أي شركة توصلون معها؟") or generic branch ("عندكم فرع في جدة؟").
_ORDER_ACTUAL_SHIPPING_TOPIC_RE = re.compile(
    r"(?:"
    r"فرع|فروع|branch|"
    r"شرك[ةه]|ناقل|carrier|courier|"
    r"سمسا|smsa|aramex|ارامكس|\bdhl\b|"
    r"انرسل|ارسل|طلع|"
    r"متي\s*(?:يوصل|توصل|يجي)|متى\s*(?:يوصل|توصل|يجي)|"
    r"(?:وين|فين|اين)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def conversation_has_post_order_context(state: Any) -> bool:
    """Same post-order signals the ASK_SHIPPING owner already uses."""
    if state is None:
        return False
    if _order_prep_field(state, "payment_receipt_received", False):
        return True
    status = str(_order_prep_field(state, "order_status", "") or "").lower()
    if status in _POST_ORDER_STATUSES:
        return True
    focus = _brain_state_field(state, "current_product_focus", None)
    city = _order_prep_field(state, "city", None)
    return bool(focus) and bool(city)


def is_order_actual_shipping_question(message: str) -> bool:
    """Customer-specific shipment/order question, not a generic merchant fact.

    Requires an order/shipment anchor plus origin, carrier, or delivery-time
    semantics. Does not blacklist ``فرع`` and does not match generic
    branch/capability asks that lack an order reference.
    """
    if is_pre_order_shipping_inquiry(message):
        return False
    norm = _norm_ar(message or "")
    if not norm:
        return False
    if not _ORDER_ANCHOR_RE.search(norm):
        return False
    if is_post_order_shipping_brain_defer(message):
        return True
    return bool(_ORDER_ACTUAL_SHIPPING_TOPIC_RE.search(norm))


def is_explicit_order_tracking_request(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
    inbound_metadata: Optional[dict] = None,
) -> bool:
    """
    Layer 1 routing — only explicit tracking follow-ups become track_order.

    Excludes general shipping duration and post-order carrier/policy asks that
    the decision engine defers to ACTION_LLM_REPLY with order context.
    """
    try:
        from modules.ai.brain.commerce.current_order_amount import (  # noqa: PLC0415
            should_route_current_order_amount_over_tracking,
        )

        if should_route_current_order_amount_over_tracking(
            message,
            state=state,
            inbound_metadata=inbound_metadata,
        ):
            return False
        from modules.ai.brain.commerce.current_order_amount import (  # noqa: PLC0415
            should_route_current_order_inquiry_over_tracking,
        )

        if should_route_current_order_inquiry_over_tracking(
            message,
            state=state,
            inbound_metadata=inbound_metadata,
        ):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — amount guard must not block tracking
        pass
    if not is_order_tracking_follow_up(
        message,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
    ):
        return False
    if is_general_shipping_duration_inquiry(message):
        return False
    if is_post_order_shipping_brain_defer(message):
        return False
    return True


def should_exempt_from_availability_rewrite(
    message: str,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
) -> bool:
    """
    Block availability rewrites for shipping/tracking inbound.

    Layer 2 is independent of track_order routing — even ask_shipping turns
    must not produce «متوفر متى يوصل الطلب بعدة خيارات».
    """
    _ = (state, history, commerce_bundle)  # reserved for future contextual exempt
    try:
        from modules.ai.brain.commerce.product_label_hygiene import (  # noqa: PLC0415
            is_negative_logistics_or_contact_context,
        )

        if is_negative_logistics_or_contact_context(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import at guard boundary
        pass
    return is_shipping_tracking_non_product_label(message)


def boost_track_order_intent(
    message: str,
    rule_intent: Optional[Intent] = None,
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    commerce_bundle: Optional[dict] = None,
    inbound_metadata: Optional[dict] = None,
) -> Optional[Intent]:
    """Return a high-confidence track_order intent when guard fires."""
    if not is_explicit_order_tracking_request(
        message,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
        inbound_metadata=inbound_metadata,
    ):
        return None
    if rule_intent and rule_intent.name == INTENT_TRACK_ORDER:
        return rule_intent
    slots = dict(getattr(rule_intent, "slots", None) or {})
    bare_ref = extract_bare_order_reference(message)
    if bare_ref:
        slots.setdefault("order_id", bare_ref)
        slots.setdefault("order_number", bare_ref)
    return Intent(
        name=INTENT_TRACK_ORDER,
        confidence=0.97,
        slots=slots,
        raw_message=message,
        extraction_method="order_tracking_guard",
    )


__all__ = [
    "boost_track_order_intent",
    "build_order_support_follow_up_args",
    "compose_order_support_response_goal_for_decision",
    "extract_bare_order_reference",
    "extract_order_reference_from_history",
    "has_existing_order_evidence",
    "has_order_reference_support_context",
    "has_pending_order_reference_evidence",
    "is_order_reference_continuity_active",
    "is_order_support_continuity_allowed",
    "is_order_support_topic_reset",
    "is_explicit_order_tracking_request",
    "is_general_shipping_duration_inquiry",
    "is_order_support_operational_follow_up",
    "is_order_tracking_follow_up",
    "conversation_has_post_order_context",
    "is_order_actual_shipping_question",
    "is_post_order_shipping_brain_defer",
    "is_pre_order_shipping_inquiry",
    "is_shipping_tracking_non_product_label",
    "should_exempt_from_availability_rewrite",
    "try_order_reference_continuity_decision",
]
