"""
external_outbound_context.py
────────────────────────────
Platform-wide conversational context from merchant outbound messages —
regardless of whether Nahla, a shipping system, CRM, or manual WhatsApp
sent them. The conversation transcript is the source of truth.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

logger = logging.getLogger("nahla.brain.external_outbound_context")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

CONTEXT_DELIVERY_REVIEW = "delivery_review_request"
CONTEXT_SHIPMENT_UPDATE = "shipment_update"
CONTEXT_PAYMENT_REMINDER = "payment_reminder"
CONTEXT_MANUAL_SUPPORT = "manual_support_followup"
CONTEXT_ORDER_STATUS = "order_status_update"

SOURCE_CONVERSATION = "conversation_outbound"
SOURCE_NAHLA = "nahla"
SOURCE_EXTERNAL = "external"

_SESSION_KEY = "external_outbound_context"
_POST_PURCHASE_FLAG = "post_purchase_active"

_DELIVERY_REVIEW_RE = re.compile(
    r"(?:"
    r"تم\s*توصيل|وصل\s*طلب(?:ك|كم)|تم\s*التسليم|تم\s*التوصيل|"
    r"شاركنا\s*ر(?:ا|أ)يك|"
    r"كيف\s*كان(?:ت)?\s*الت(?:ج|)رب(?:ة|ه)|"
    r"نود\s*(?:أ?ن\s*)?نعرف|"
    r"ر(?:ا|أ)يك\s*في|"
    r"تقييم\s*(?:ال)?(?:منتج|تجرب)|"
    r"delivered|share\s*your\s*(?:feedback|review)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SHIPMENT_UPDATE_RE = re.compile(
    r"(?:"
    r"شحنت(?:ك|كم)|في\s*الط(?:ر|)يق|خرج(?:ت)?\s*للتوصيل|"
    r"out\s*for\s*delivery|shipment\s*update|tracking"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PAYMENT_REMINDER_RE = re.compile(
    r"(?:"
    r"باق(?:ي|ي)\s*(?:المبلغ|الدفع)|"
    r"ت(?:ذ|)كير\s*(?:بال)?(?:دفع|تحويل)|"
    r"payment\s*reminder|awaiting\s*payment"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_MANUAL_SUPPORT_RE = re.compile(
    r"(?:"
    r"تواصل(?:نا|و)\s*مع(?:ك|كم)|"
    r"فريق(?:نا)?\s*(?:يرد|يتابع)|"
    r"support\s*follow(?:-|\s*)up"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_STATUS_RE = re.compile(
    r"(?:"
    r"حالة\s*طلب(?:ك|كم)|"
    r"طلب(?:ك|كم)\s*(?:قيد|تحت)|"
    r"order\s*status"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_REF_RE = re.compile(r"\b(\d{6,12})\b")

_DEFAULT_OUTBOUND_LOOKBACK = 3


def _session_dict(state: Any) -> dict:
    if state is None:
        return {}
    if isinstance(state, dict):
        raw = state.get("commerce_session")
        return dict(raw or {}) if isinstance(raw, dict) else {}
    raw = getattr(state, "commerce_session", None)
    return dict(raw or {}) if isinstance(raw, dict) else {}


def _order_status(state: Any) -> str:
    if isinstance(state, dict):
        op = state.get("order_prep") or {}
        if isinstance(op, dict):
            return str(op.get("order_status") or "").strip().lower()
        return ""
    op = getattr(state, "order_prep", None)
    if op is None:
        return ""
    if isinstance(op, dict):
        return str(op.get("order_status") or "").strip().lower()
    return str(getattr(op, "order_status", "") or "").strip().lower()


@dataclass(frozen=True)
class ExternalOutboundContext:
    context_type: str
    order_reference: Optional[str] = None
    source: str = SOURCE_CONVERSATION
    created_at: Optional[datetime] = None
    body_snippet: str = ""
    extra: dict = field(default_factory=dict)


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
    )
    return _WS_RE.sub(" ", t).strip()


def _turn_body(turn: Any) -> str:
    if not isinstance(turn, dict):
        return ""
    return str(turn.get("body") or turn.get("text") or "").strip()


def _turn_direction(turn: Any) -> str:
    if not isinstance(turn, dict):
        return ""
    return str(turn.get("direction") or turn.get("role") or "").lower()


def _turn_source(turn: Any) -> str:
    if not isinstance(turn, dict):
        return SOURCE_CONVERSATION
    for key in ("source", "echo_source", "origin", "sender"):
        val = str(turn.get(key) or "").strip().lower()
        if val:
            if "nahla" in val or val in {"bot", "ai", "assistant"}:
                return SOURCE_NAHLA
            if val in {"merchant_mobile_app", "external", "automation", "crm"}:
                return SOURCE_EXTERNAL
            return val
    return SOURCE_CONVERSATION


def _turn_created_at(turn: Any) -> Optional[datetime]:
    if not isinstance(turn, dict):
        return None
    for key in ("created_at", "timestamp", "sent_at"):
        raw = turn.get(key)
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def classify_external_outbound(body: str) -> Optional[ExternalOutboundContext]:
    """Classify a single outbound message body."""
    raw = (body or "").strip()
    if not raw:
        return None
    norm = _norm(raw)
    if not norm:
        return None

    order_ref = None
    ref_match = _ORDER_REF_RE.search(raw)
    if ref_match:
        order_ref = ref_match.group(1)

    if _DELIVERY_REVIEW_RE.search(norm):
        ctx_type = CONTEXT_DELIVERY_REVIEW
    elif _SHIPMENT_UPDATE_RE.search(norm):
        ctx_type = CONTEXT_SHIPMENT_UPDATE
    elif _PAYMENT_REMINDER_RE.search(norm):
        ctx_type = CONTEXT_PAYMENT_REMINDER
    elif _MANUAL_SUPPORT_RE.search(norm):
        ctx_type = CONTEXT_MANUAL_SUPPORT
    elif _ORDER_STATUS_RE.search(norm):
        ctx_type = CONTEXT_ORDER_STATUS
    else:
        return None

    return ExternalOutboundContext(
        context_type=ctx_type,
        order_reference=order_ref,
        source=SOURCE_CONVERSATION,
        body_snippet=raw[:160],
    )


def resolve_external_outbound_context(
    history: Optional[List[Any]] = None,
    *,
    lookback_outbound: int = _DEFAULT_OUTBOUND_LOOKBACK,
) -> Optional[ExternalOutboundContext]:
    """Most recent classified outbound context from conversation history."""
    seen = 0
    for turn in reversed(history or []):
        direction = _turn_direction(turn)
        if direction not in {"out", "outbound", "assistant", "bot", "ai"}:
            continue
        body = _turn_body(turn)
        if not body:
            continue
        classified = classify_external_outbound(body)
        if classified is not None:
            return ExternalOutboundContext(
                context_type=classified.context_type,
                order_reference=classified.order_reference,
                source=_turn_source(turn),
                created_at=_turn_created_at(turn),
                body_snippet=classified.body_snippet,
            )
        seen += 1
        if seen >= lookback_outbound:
            break
    return None


def get_persisted_external_outbound_context(state: Any) -> Optional[ExternalOutboundContext]:
    cs = _session_dict(state)
    if not cs:
        return None
    raw = cs.get(_SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    ctx_type = str(raw.get("context_type") or "").strip()
    if not ctx_type:
        return None
    return ExternalOutboundContext(
        context_type=ctx_type,
        order_reference=str(raw.get("order_reference") or "") or None,
        source=str(raw.get("source") or SOURCE_CONVERSATION),
        body_snippet=str(raw.get("body_snippet") or "")[:160],
    )


def persist_external_outbound_context(state: Any, ctx: ExternalOutboundContext) -> None:
    cs = _session_dict(state)
    cs[_SESSION_KEY] = {
        "context_type": ctx.context_type,
        "order_reference": ctx.order_reference,
        "source": ctx.source,
        "body_snippet": ctx.body_snippet,
    }
    cs[_POST_PURCHASE_FLAG] = ctx.context_type in {
        CONTEXT_DELIVERY_REVIEW,
        CONTEXT_SHIPMENT_UPDATE,
        CONTEXT_ORDER_STATUS,
    }
    if isinstance(state, dict):
        state["commerce_session"] = cs
        return
    try:
        state.commerce_session = cs
    except Exception:  # noqa: BLE001  # noqa: silent-ok — duck-typed state patch is best-effort
        pass


def is_post_purchase_context_active(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
) -> bool:
    """True when conversation evidence indicates post-purchase framing."""
    cs = _session_dict(state)
    if cs.get(_POST_PURCHASE_FLAG):
        return True

    persisted = get_persisted_external_outbound_context(state)
    if persisted and persisted.context_type in {
        CONTEXT_DELIVERY_REVIEW,
        CONTEXT_SHIPMENT_UPDATE,
        CONTEXT_ORDER_STATUS,
    }:
        return True

    resolved = resolve_external_outbound_context(history)
    if resolved and resolved.context_type in {
        CONTEXT_DELIVERY_REVIEW,
        CONTEXT_SHIPMENT_UPDATE,
        CONTEXT_ORDER_STATUS,
    }:
        return True

    try:
        from .commerce_objective import (  # noqa: PLC0415
            COMMERCE_OBJECTIVE_POST_PURCHASE,
            COMMERCE_OBJECTIVE_SUPPORT,
            COMMERCE_OBJECTIVE_TRACKING,
            get_commerce_objective,
        )

        obj = get_commerce_objective(state)
        cs = _session_dict(state)
        if obj in {
            COMMERCE_OBJECTIVE_POST_PURCHASE,
            COMMERCE_OBJECTIVE_TRACKING,
        } or (obj == COMMERCE_OBJECTIVE_SUPPORT and cs.get(_POST_PURCHASE_FLAG)):
            status = _order_status(state)
            if (
                obj == COMMERCE_OBJECTIVE_POST_PURCHASE
                or obj == COMMERCE_OBJECTIVE_SUPPORT
                or status == "delivered"
            ):
                return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — objective probe is best-effort
        pass

    if _order_status(state) == "delivered":
        return True
    return False


def apply_external_outbound_context(ctx: Any) -> Optional[ExternalOutboundContext]:
    """Resolve outbound context from history and stamp session + objective."""
    history = list(getattr(ctx, "history", None) or [])
    state = getattr(ctx, "state", None)
    if state is None:
        return None

    resolved = resolve_external_outbound_context(history)
    if resolved is None:
        return get_persisted_external_outbound_context(state)

    persist_external_outbound_context(state, resolved)
    if resolved.context_type == CONTEXT_DELIVERY_REVIEW:
        try:
            from .commerce_objective import transition_commerce_objective_for_post_purchase  # noqa: PLC0415

            transition_commerce_objective_for_post_purchase(
                state,
                reason="external_delivery_review_outbound",
                order_reference=resolved.order_reference,
            )
        except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — objective shift must not block context
            logger.debug("[EXTERNAL_OUTBOUND] objective_shift_failed err=%s", exc)

    logger.info(
        "[EXTERNAL_OUTBOUND] context_type=%s order_ref=%s source=%s preview=%r",
        resolved.context_type,
        resolved.order_reference or "-",
        resolved.source,
        resolved.body_snippet[:72],
    )
    return resolved


__all__ = [
    "CONTEXT_DELIVERY_REVIEW",
    "CONTEXT_MANUAL_SUPPORT",
    "CONTEXT_ORDER_STATUS",
    "CONTEXT_PAYMENT_REMINDER",
    "CONTEXT_SHIPMENT_UPDATE",
    "ExternalOutboundContext",
    "apply_external_outbound_context",
    "classify_external_outbound",
    "get_persisted_external_outbound_context",
    "is_post_purchase_context_active",
    "persist_external_outbound_context",
    "resolve_external_outbound_context",
]
