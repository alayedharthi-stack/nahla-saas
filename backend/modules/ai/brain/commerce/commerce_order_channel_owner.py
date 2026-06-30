"""
commerce_order_channel_owner.py
─────────────────────────────────
PR-CE3 — order channel choice + cold shipping inquiry ownership.

Deterministic routing for storefront self-checkout vs WhatsApp quick order,
and cold/refrigerated shipping questions — without LLM channel decisions.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.commerce_order_channel_owner")

TOPIC_COMMERCE_ORDER_CHANNEL = "commerce_order_channel"
TOPIC_STOREFRONT_SELF_CHECKOUT = "storefront_self_checkout"
TOPIC_COLD_SHIPPING_INQUIRY = "cold_shipping_inquiry"

_PREFERRED_CHANNEL_KEY = "preferred_order_channel"
_PENDING_COLD_SHIPPING_KEY = "pending_cold_shipping_inquiry"
_SESSION_KEY = "commerce_order_channel"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_STOREFRONT_SELF_CHECKOUT_RE = re.compile(
    r"(?:"
    r"(?:بدخل|ب(?:دخل|فتح))\s*(?:ال)?(?:سل[ةه]|رابط|موقع|متجر|السل[ةه])"
    r"|(?:ب|س)?(?:طلب|كمل|اكمل|أ?كمل)\s*(?:من\s*)?(?:ال)?(?:سل[ةه]|رابط|موقع)"
    r"|(?:س)?(?:و(?:ي|وي))\s*(?:ال)?(?:طلب)\s*(?:من\s*)?(?:ال)?(?:سل[ةه])"
    r"|(?:انا|أنا)\s*(?:بدخل|ب(?:دخل|طلب))\s*(?:ال)?(?:سل[ةه]|رابط|موقع|متجر)"
    r"|(?:اطلب|أطلب)\s*(?:من\s*)?(?:ال)?(?:موقع|سل[ةه])(?:\s|$|[؟?])"
    r"|(?:اطلب|أطلب)\s*(?:من\s*)?(?:ال)?متجر(?:\s|$|[؟?])(?!\s*(?:كتalog|catalog|كاتلوج|الخيارات))"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COLD_SHIPPING_RE = re.compile(
    r"(?:"
    r"مبرد(?:\s*(?:ال)?(?:توصيل|شحن))?"
    r"|(?:ال)?(?:توصيل|شحن)\s*مبرد"
    r"|(?:يوصل|يوصلون|يصل)\s*مبرد"
    r"|(?:شحن|توصيل).*مبرد"
    r"|مبرد.*(?:شحن|توصيل|يوصل)"
    r"|cold\s*(?:ship|deliver|shipping|delivery)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CITY_REPLY_RE = re.compile(
    r"(?:"
    r"^(?:جده|جدة|riyadh|الرياض|الدمام|مكه|مكة|المدين(?:ه|ة)|"
    r"الخبر|الطائف|تبوك|ابها|أبها|حائل|نجران|جازان|ينبع|القصيم|"
    r"الاحساء|الأحساء|خميس|بريده|بريدة|عرعر|سكاكا|الجبيل|"
    r"dammam|jeddah|makkah|madinah|khobar|taif|tabuk|abha|hail|"
    r"najran|jazan|yanbu|qassim|ahsa|buraidah|arar|jubail)"
    r"(?:\s|$|[!.؟?])"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_COLD_SHIPPING_KB_KINDS = frozenset({"cold_shipping", "shipping_zones", "shipping_policy"})


class OrderChannelRouteKind(str, Enum):
    STOREFRONT_SELF_CHECKOUT = "storefront_self_checkout"
    COLD_SHIPPING_INQUIRY = "cold_shipping_inquiry"
    WHATSAPP_QUICK_ORDER = "whatsapp_quick_order"


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
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def is_storefront_self_checkout_request(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_STOREFRONT_SELF_CHECKOUT_RE.search(_norm(raw)))


def is_cold_shipping_inquiry(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_COLD_SHIPPING_RE.search(_norm(raw)))


def get_preferred_order_channel(state: Any) -> str:
    session = dict(getattr(state, "commerce_session", None) or {})
    return str(session.get(_PREFERRED_CHANNEL_KEY) or "").strip()


def has_storefront_channel_committed(state: Any) -> bool:
    channel = get_preferred_order_channel(state)
    if channel == OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value:
        return True
    prep = getattr(state, "order_prep", None)
    checkout = str(getattr(prep, "checkout_channel", "") or "").strip().lower()
    return checkout in {"store_link", "online_store", "storefront_self_checkout"}


def pin_storefront_self_checkout(state: Any, *, source: str) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_PREFERRED_CHANNEL_KEY] = OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value
    session[_SESSION_KEY] = {
        "route_kind": OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value,
        "source": str(source or "explicit"),
        "created_at": time.time(),
    }
    state.commerce_session = session
    prep = getattr(state, "order_prep", None)
    if prep is not None:
        try:
            prep.checkout_channel = "store_link"
        except Exception:  # noqa: BLE001  # noqa: silent-ok — prep patch is best-effort
            pass


def get_pending_cold_shipping(state: Any) -> Optional[Dict[str, Any]]:
    session = dict(getattr(state, "commerce_session", None) or {})
    pending = session.get(_PENDING_COLD_SHIPPING_KEY)
    if not isinstance(pending, dict):
        return None
    if str(pending.get("type") or "") != OrderChannelRouteKind.COLD_SHIPPING_INQUIRY.value:
        return None
    return dict(pending)


def pin_pending_cold_shipping_city(state: Any, *, source: str) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_PENDING_COLD_SHIPPING_KEY] = {
        "type": OrderChannelRouteKind.COLD_SHIPPING_INQUIRY.value,
        "needs_city": True,
        "source": str(source or "cold_shipping_inquiry"),
        "created_at": time.time(),
    }
    state.commerce_session = session


def clear_pending_cold_shipping(state: Any) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session.pop(_PENDING_COLD_SHIPPING_KEY, None)
    state.commerce_session = session


def _looks_like_city_reply(message: str) -> bool:
    raw = (message or "").strip()
    if not raw or len(raw.split()) > 6:
        return False
    if is_storefront_self_checkout_request(raw):
        return False
    if is_cold_shipping_inquiry(raw):
        return False
    norm = _norm(raw)
    if _CITY_REPLY_RE.search(norm):
        return True
    if len(norm.split()) <= 3 and not re.search(r"(?:منتج|طلب|كتalog|catalog|كاتلوج|عسل)", norm):
        return True
    return False


def _ce1_status_should_own(ctx: Any) -> bool:
    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    session = dict(getattr(state, "commerce_session", None) or {})
    sr_ctx = session.get("status_reply_product_context") or {}
    if not (focus.get("from_status_reply") or sr_ctx.get("active")):
        return False
    try:
        from modules.ai.brain.commerce.commerce_entry_orchestrator import (  # noqa: PLC0415
            CustomerAction,
            classify_customer_action,
        )
        from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
            extract_status_reply_quantity,
            is_status_reply_follow_up_message,
        )

        if not is_status_reply_follow_up_message(message):
            return False
        action = classify_customer_action(
            message,
            quantity_hint=extract_status_reply_quantity(message),
            has_product_focus=bool(focus.get("title") or focus.get("id")),
        )
        return action in {
            CustomerAction.PRICE,
            CustomerAction.BUY,
            CustomerAction.QUANTITY,
        }
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return False


def _hard_defer_ce3(ctx: Any) -> bool:
    """Defer CE3 for owners that must never be overridden."""
    message = str(getattr(ctx, "message", "") or "").strip()
    if not message:
        return True

    try:
        from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: PLC0415
            current_turn_has_payment_evidence,
        )

        if current_turn_has_payment_evidence(ctx):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    if _ce1_status_should_own(ctx):
        return True

    try:
        from modules.ai.brain.commerce.product_knowledge_or_comparison import (  # noqa: PLC0415
            classify_product_knowledge_kind,
        )

        if classify_product_knowledge_kind(message) is not None:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    try:
        from modules.ai.brain.commerce.physical_location_ownership import (  # noqa: PLC0415
            is_physical_location_request,
        )

        if is_physical_location_request(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    return False


def _should_defer_to_prior_owners(ctx: Any) -> bool:
    if _hard_defer_ce3(ctx):
        return True
    message = str(getattr(ctx, "message", "") or "").strip()
    try:
        from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: PLC0415
            _is_explicit_catalog_browse_request,
        )

        if _is_explicit_catalog_browse_request(message, ctx):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    return False


def should_suppress_product_ordering_prompt(ctx: Any) -> bool:
    """Block 'وش المنتج اللي أجهزه لك؟' when CE3 owns the turn."""
    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)
    if is_cold_shipping_inquiry(message):
        return True
    if get_pending_cold_shipping(state):
        return True
    if has_storefront_channel_committed(state) and not _ce1_status_should_own(ctx):
        return True
    if is_storefront_self_checkout_request(message):
        return True
    return False


def _retrieve_cold_shipping_kb(db: Any, tenant_id: int) -> Optional[Dict[str, Any]]:
    if not db or not tenant_id:
        return None
    try:
        from models import MerchantKnowledgeSection  # noqa: PLC0415
        from core.knowledge import apply_ai_visible_kb_query_filters  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return None
    try:
        rows = (
            apply_ai_visible_kb_query_filters(db.query(MerchantKnowledgeSection))
            .filter(
                MerchantKnowledgeSection.tenant_id == int(tenant_id),
                MerchantKnowledgeSection.kind.in_(tuple(_COLD_SHIPPING_KB_KINDS)),
            )
            .order_by(
                MerchantKnowledgeSection.priority.asc(),
                MerchantKnowledgeSection.updated_at.desc(),
            )
            .limit(40)
            .all()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return None
    for row in rows:
        kind = str(getattr(row, "kind", "") or "").strip()
        title = str(getattr(row, "title", "") or "").strip()
        body = str(getattr(row, "body", "") or "").strip()
        if not body:
            continue
        norm_body = _norm(body)
        if kind == "cold_shipping" or "مبرد" in norm_body or "cold" in norm_body:
            return {
                "section_id": int(getattr(row, "id", 0) or 0),
                "title": title,
                "body": body,
                "kind": kind,
            }
    return None


def _compose_storefront_goal(*, store_url: str, store_name: str, cold_shipping: bool) -> str:
    parts = [
        "STOREFRONT_SELF_CHECKOUT compose principles: customer will complete purchase "
        "via online store cart themselves; natural concise Saudi Arabic; "
        "do NOT ask which product to prepare; do NOT start WhatsApp quick order; "
        "do NOT say you will complete the order on their behalf; "
        "offer store link when available.",
        f"store_url={'yes' if store_url else 'no'}",
    ]
    if store_name:
        parts.append(f"store_name={store_name[:40]}")
    if cold_shipping:
        parts.append(
            "same_turn_also_asks_cold_shipping=true | answer refrigerated shipping "
            "from KB/policy only; if city-dependent ask delivery city ONLY; "
            "no extra generic follow-up questions"
        )
    return " | ".join(parts)


def _compose_cold_shipping_goal(
    *,
    kb: Optional[Dict[str, Any]],
    needs_city: bool,
    city: str = "",
    storefront_committed: bool = False,
) -> str:
    parts = [
        "COLD_SHIPPING_INQUIRY compose principles: customer asks about refrigerated "
        "delivery/shipping; natural concise Saudi Arabic; do NOT ask which product first; "
        "do NOT start WhatsApp quick order or order_prep; "
        "no generic 'before I complete your order' upsell.",
        f"needs_city={needs_city}",
        f"storefront_committed={storefront_committed}",
    ]
    if city:
        parts.append(f"customer_city={city[:40]}")
    if kb:
        parts.append(f"kb_section_id={kb.get('section_id')}")
        parts.append("ground_answer_in_kb_facts_only=true")
    else:
        parts.append(
            "no_kb_cold_shipping=true | if availability depends on city ask delivery "
            "city ONLY; otherwise explain verification depends on city/carrier policy"
        )
    if needs_city:
        parts.append("ask_delivery_city_only=true | no_extra_follow_up_question=true")
    return " | ".join(parts)


def _load_store_capabilities(ctx: Any) -> Dict[str, str]:
    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    store_url = ""
    store_name = ""
    if db and tenant_id:
        try:
            from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: PLC0415
                load_channel_capabilities,
            )

            caps = load_channel_capabilities(db, tenant_id)
            store_url = str(caps.store_url or "").strip()
            store_name = str(caps.store_name or "").strip()
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            pass
    return {"store_url": store_url, "store_name": store_name}


def _resolve_storefront(ctx: Any, *, cold_shipping: bool) -> Any:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    state = getattr(ctx, "state", None)
    pin_storefront_self_checkout(state, source="explicit_storefront_self_checkout")
    caps = _load_store_capabilities(ctx)
    allowed: Dict[str, Any] = {
        "preferred_order_channel": OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value,
        "store_url": caps.get("store_url") or "",
        "store_name": caps.get("store_name") or "",
    }
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_STOREFRONT_SELF_CHECKOUT,
            "order_channel_route_kind": OrderChannelRouteKind.STOREFRONT_SELF_CHECKOUT.value,
            "commerce_entry_owner": TOPIC_COMMERCE_ORDER_CHANNEL,
            "block_commerce_escalation": False,
            "block_whatsapp_quick_order": True,
            "forbidden_claims": [
                "whatsapp_quick_order_start",
                "ask_product_before_channel",
                "complete_order_on_behalf",
            ],
            "allowed_facts": allowed,
            "response_goal": _compose_storefront_goal(
                store_url=caps.get("store_url") or "",
                store_name=caps.get("store_name") or "",
                cold_shipping=cold_shipping,
            ),
        },
        reason="commerce_order_channel — storefront self-checkout",
        confidence=0.94,
    )


def _resolve_cold_shipping(
    ctx: Any,
    *,
    city: str = "",
    storefront_committed: bool = False,
) -> Any:
    from modules.ai.brain.decision.actions import ACTION_LLM_REPLY  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    state = getattr(ctx, "state", None)
    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    kb = _retrieve_cold_shipping_kb(db, tenant_id)
    kb_needs_city = bool(kb and re.search(r"(?:مدين|city|منطق)", _norm(kb.get("body") or "")))
    needs_city = bool(not city and (kb_needs_city or not kb))

    if needs_city and not city:
        pin_pending_cold_shipping_city(state, source="cold_shipping_inquiry")
    else:
        clear_pending_cold_shipping(state)

    allowed: Dict[str, Any] = {
        "cold_shipping_inquiry": True,
        "delivery_city": city or "",
    }
    if kb:
        allowed.update({
            "kb_section_id": kb.get("section_id"),
            "kb_section_title": kb.get("title"),
            "kb_section_body": kb.get("body"),
            "kb_section_kind": kb.get("kind"),
        })

    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": TOPIC_COLD_SHIPPING_INQUIRY,
            "order_channel_route_kind": OrderChannelRouteKind.COLD_SHIPPING_INQUIRY.value,
            "commerce_entry_owner": TOPIC_COMMERCE_ORDER_CHANNEL,
            "block_whatsapp_quick_order": True,
            "block_commerce_escalation": storefront_committed,
            "forbidden_claims": [
                "whatsapp_quick_order_start",
                "ask_product_before_shipping",
                "generic_order_completion_upsell",
            ],
            "allowed_facts": allowed,
            "response_goal": _compose_cold_shipping_goal(
                kb=kb,
                needs_city=needs_city and not city,
                city=city,
                storefront_committed=storefront_committed,
            ),
        },
        reason="commerce_order_channel — cold shipping inquiry",
        confidence=0.93,
    )


def try_commerce_order_channel_decision(ctx: Any) -> Optional[Any]:
    """
    CE3 owner — storefront self-checkout channel + cold shipping inquiry.

    Runs after payment-evidence (#362) and before catalog delivery (CE2).
    """
    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)
    if not message:
        return None

    if _hard_defer_ce3(ctx):
        return None

    pending = get_pending_cold_shipping(state)
    storefront = is_storefront_self_checkout_request(message)
    cold = is_cold_shipping_inquiry(message)
    committed_storefront = has_storefront_channel_committed(state)

    if pending and pending.get("needs_city") and _looks_like_city_reply(message):
        city = message.strip()
        clear_pending_cold_shipping(state)
        logger.info(
            "[COMMERCE_ORDER_CHANNEL] cold_shipping_city tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return _resolve_cold_shipping(
            ctx,
            city=city,
            storefront_committed=committed_storefront,
        )

    if storefront:
        logger.info(
            "[COMMERCE_ORDER_CHANNEL] storefront_self_checkout tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return _resolve_storefront(ctx, cold_shipping=cold)

    if cold:
        logger.info(
            "[COMMERCE_ORDER_CHANNEL] cold_shipping_inquiry tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return _resolve_cold_shipping(ctx, storefront_committed=committed_storefront)

    if _should_defer_to_prior_owners(ctx):
        return None

    return None


__all__ = [
    "OrderChannelRouteKind",
    "TOPIC_COLD_SHIPPING_INQUIRY",
    "TOPIC_COMMERCE_ORDER_CHANNEL",
    "TOPIC_STOREFRONT_SELF_CHECKOUT",
    "get_pending_cold_shipping",
    "get_preferred_order_channel",
    "has_storefront_channel_committed",
    "is_cold_shipping_inquiry",
    "is_storefront_self_checkout_request",
    "pin_storefront_self_checkout",
    "should_suppress_product_ordering_prompt",
    "try_commerce_order_channel_decision",
]
