"""
commerce_entry_catalog_delivery.py
──────────────────────────────────
PR-CE2 — unified catalog delivery ownership.

Deterministic routing for when to send the full catalog, a specific product
card/link, or block catalog in favor of KB / correction / product knowledge.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger("nahla.brain.commerce_entry_catalog_delivery")

TOPIC_COMMERCE_ENTRY_CATALOG = "commerce_entry_catalog"
_SESSION_KEY = "commerce_entry_catalog_delivery"
_BLOCK_KEY = "catalog_delivery_blocked"
_LAST_SENT_KEY = "catalog_delivery_last_sent"
_PENDING_CATALOG_KEY = "pending_catalog_delivery"

_CATALOG_ARABIC_TOKENS = (
    "كاتلوج",
    "الكتالوج",
    "كتالوج",
    "كتalog",
    "catalog",
    "الخيارات",
    "المتجر",
)

_CATALOG_NORM_TOKENS = frozenset({
    "كاتلوج",
    "الكتالوج",
    "كتaloj",
    "كتalog",
    "catalog",
    "الانواع",
    "انواع",
    "المتوفر",
    "المتاح",
})

_CATALOG_TOKEN_RE = (
    r"(?:"
    r"كاتلوج|الكتالوج|كتaloj|كتalog|catalog|الخيارات|المتجر"
    r")"
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_SEND_CATALOG_EXPLICIT_RE = re.compile(
    r"(?:"
    rf"(?:أ?رسل|ابع|ابع(?:ه|لي| لي))\s*(?:ال)?{_CATALOG_TOKEN_RE}"
    rf"|(?:^|\s)(?:ال)?{_CATALOG_TOKEN_RE}(?:\s|$|[؟?])"
    rf"|(?:أ?عرض|ورني|وريني|أ?بين|شوف)\s*(?:ال)?{_CATALOG_TOKEN_RE}"
    r"|(?:وش|ايش)\s+(?:ال)?(?:انواع|أنواع)(?:\s+(?:المتوف(?:ر(?:ه|ة))?|عند(?:كم|ك)))?"
    r"|(?:وش|ايش)\s+(?:المتوفر|المتاح|عند(?:كم|ك)\s+(?:من\s+)?\S)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_SEND_CONFIRMATION_RE = re.compile(
    r"^(?:"
    r"ارسل|أرسل|ابع|ابعه|ابع(?:ه|لي| لي)"
    r"|نعم|ايه|أيه|آيه|اي|أي|اه|أه"
    r"|تمام|طيب|اوك|أوك|ok|yes|yep"
    r")(?:\s*[!.؟?🌷👍✅]*)*$",
    re.UNICODE | re.IGNORECASE,
)

_SHOW_CATEGORY_BROWSE_RE = re.compile(
    r"(?:"
    r"(?:أ?بي|أ?بغ[ىي]|(?:أ?ريد|(?:أ?ود(?:ي)?)))\s+(?:أ?شوف|(?:أ?عرض))\s+(?:ال)?\S"
    r"|(?:أ?رسل|ابع)\s+(?:ال)?(?:عسل|\S.{2,40})"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SEND_PRODUCT_ITEM_RE = re.compile(
    r"(?:"
    r"أ?رسل\s+(?:ال)?(?:رابط|المنتج|الصور(?:ه|ة)|ه|ها)\b"
    r"|(?:أ?بغ[ىي]|أ?بي|أ?ريد)\s*(?:أ?شوف\s*)?(?:ال)?(?:رابط|المنتج|الخيارات)"
    r"|send\s+(?:link|product|catalog\s+item)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_PRODUCT_AVAIL_RE = re.compile(
    r"(?:"
    r"(?:هل|في(?:ه)?|فيه)\s+(?:عند(?:كم|ك)\s+)?\S.{2,50}\s*(?:متوفر|موجود|available)"
    r"|(?:\S.{2,50})\s*(?:متوفر|موجود)\s*(?:عند(?:كم|ك))?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_MISS_COMPLAINT_RE = re.compile(
    r"(?:"
    r"(?:ما|مو)\s*(?:فيه|في(?:ه)?)\s*(?:إلا|الا|ب(?:س|س))"
    r"|(?:مو|ما)\s+(?:ال)?(?:عسل|منتج|هذا|هذي|الكتaloj|الكاتلوج|الخيارات)"
    r"|(?:انا|أنا)\s+(?:اسأل|بسأل|سأل(?:ت)?)\s+عن"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ORDER_CONTINUATION_RE = re.compile(
    r"(?:"
    r"أ?رسل\s*(?:ال)?(?:طلب|الطلب)"
    r"|(?:كمّ?ل|اكمل|أ?كمل)\s*(?:ال)?(?:طلب|الطلب)"
    r"|send\s+(?:the\s+)?order"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ACTIVE_ORDER_STAGES = frozenset({"ordering", "checkout", "order"})

_COMPARISON_KNOWLEDGE_RE = re.compile(
    r"(?:"
    r"(?:ال)?(?:فرق|اختلاف)|compare|comparison|أيهما|ايهما|"
    r"وش\s+(?:ال)?(?:فرق|اختلاف|يميز(?:ه|ها)?)|"
    r"(?:وش|ايش|ما)\s+فرق\s+عن|"
    r"(?:ليش|لماذا|why)\s+(?:أ?غلى|اغلى|expensive)|"
    r"(?:هو|هي|هذا)\s+(?:نفس|same)\s+(?:ال)?(?:إنتاج|production|batch)|"
    r"(?:وش|ما)\s+(?:قص(?:ت(?:ه|ها)?|ة)|معن(?:ى|ا))"
    r")",
    re.UNICODE | re.IGNORECASE,
)


class CatalogDeliveryKind(str, Enum):
    SEND_CATALOG = "send_catalog"
    SEND_PRODUCT_CATALOG_ITEM = "send_product_catalog_item"
    ASK_PRODUCT_CLARIFICATION = "ask_product_clarification"
    DELEGATE_KB_AVAILABILITY = "delegate_kb_availability"
    BLOCK_NON_CATALOG_SUBJECT = "block_catalog_for_non_catalog_subject"
    BLOCK_CATALOG_MISS_COMPLAINT = "block_catalog_after_catalog_miss_complaint"
    DELEGATE = "delegate"


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


def catalog_delivery_is_blocked(ctx: Any) -> bool:
    state = getattr(ctx, "state", None)
    session = dict(getattr(state, "commerce_session", None) or {})
    return bool(session.get(_BLOCK_KEY))


def _catalog_block_reason(state: Any) -> str:
    session = dict(getattr(state, "commerce_session", None) or {})
    return str(session.get(_BLOCK_KEY) or "").strip()


def _clear_catalog_delivery_block(state: Any) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session.pop(_BLOCK_KEY, None)
    ce = session.get(_SESSION_KEY)
    if isinstance(ce, dict) and ce.get("blocked"):
        session.pop(_SESSION_KEY, None)
    state.commerce_session = session


def pin_pending_catalog_send(state: Any, *, source: str) -> None:
    """Pin send-catalog confirmation so short affirmatives execute catalog send."""
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_PENDING_CATALOG_KEY] = {
        "type": "send_catalog",
        "source": str(source or "catalog_confirmation"),
        "created_at": time.time(),
    }
    state.commerce_session = session


def get_pending_catalog_send(state: Any) -> Optional[Dict[str, Any]]:
    session = dict(getattr(state, "commerce_session", None) or {})
    pending = session.get(_PENDING_CATALOG_KEY)
    if not isinstance(pending, dict):
        return None
    if str(pending.get("type") or "") != "send_catalog":
        return None
    return dict(pending)


def clear_pending_catalog_send(state: Any) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session.pop(_PENDING_CATALOG_KEY, None)
    state.commerce_session = session


def has_pending_catalog_send(state: Any) -> bool:
    return get_pending_catalog_send(state) is not None


def is_catalog_send_confirmation(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_CATALOG_SEND_CONFIRMATION_RE.search(_norm(raw)))


def is_catalog_confirmation_bot_reply(reply: str) -> bool:
    norm = _norm(reply or "")
    if not norm:
        return False
    if "الخيارات المؤكده من" in norm and "كت" in norm:
        return True
    if "تبغاني" in norm and ("ارسل" in norm or "اعرض" in norm) and "كت" in norm:
        return True
    return False


def block_catalog_delivery(state: Any, reason: str) -> None:
    """Block catalog delivery for the current commerce session."""
    _set_catalog_delivery_block(state, reason)


def _set_catalog_delivery_block(state: Any, reason: str) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_BLOCK_KEY] = str(reason or "blocked")
    session[_SESSION_KEY] = {"blocked": True, "reason": reason}
    state.commerce_session = session


def _mark_catalog_sent(state: Any, *, kind: str) -> None:
    if state is None:
        return
    session = dict(getattr(state, "commerce_session", None) or {})
    session[_LAST_SENT_KEY] = True
    session[_SESSION_KEY] = {"last_kind": kind, "sent": True}
    state.commerce_session = session


def _had_recent_catalog_delivery(state: Any) -> bool:
    session = dict(getattr(state, "commerce_session", None) or {})
    if session.get(_LAST_SENT_KEY):
        return True
    ce = session.get(_SESSION_KEY)
    if isinstance(ce, dict) and ce.get("last_kind") == CatalogDeliveryKind.SEND_CATALOG.value:
        return True
    return False


def _is_product_knowledge_or_comparison(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    if _COMPARISON_KNOWLEDGE_RE.search(raw):
        return True
    try:
        from modules.ai.brain.catalog.navigation_signals import (  # noqa: PLC0415
            evaluate_catalog_navigation_signals,
        )
        from modules.ai.brain.types import BrainContext  # noqa: PLC0415

        signals = evaluate_catalog_navigation_signals(
            BrainContext(
                tenant_id=0,
                customer_phone="",
                message=raw,
                intent=None,
                state=None,
                facts=None,
            )
        )
        if signals.advisory_or_comparison:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — signal probe is best-effort
        pass
    return False


def _is_explicit_catalog_browse_request(message: str, ctx: Any) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    if _ORDER_CONTINUATION_RE.search(raw):
        return False
    if _SEND_PRODUCT_ITEM_RE.search(raw):
        return False
    if _SEND_CATALOG_EXPLICIT_RE.search(raw):
        return True
    if _SHOW_CATEGORY_BROWSE_RE.search(raw):
        return True
    try:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            has_explicit_broad_browse_request,
            has_types_overview_ask,
        )

        if has_explicit_broad_browse_request(raw):
            return True
        if has_types_overview_ask(raw):
            subject = ""
            try:
                from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
                    extract_types_overview_query,
                    is_generic_category_noun,
                )

                subject = extract_types_overview_query(raw) or ""
                if subject and not is_generic_category_noun(subject):
                    return False
            except Exception:  # noqa: BLE001  # noqa: silent-ok — types subject probe is best-effort
                pass
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — browse probe is best-effort
        pass
    return False


def _is_explicit_catalog_only_request(message: str) -> bool:
    """True for catalog/type browse asks — not order send/continuation wording."""
    raw = (message or "").strip()
    if not raw:
        return False
    if _ORDER_CONTINUATION_RE.search(raw):
        return False
    if _SEND_CATALOG_EXPLICIT_RE.search(raw):
        return True
    norm = _norm(raw)
    if any(token in norm for token in _CATALOG_NORM_TOKENS):
        return True
    try:
        from modules.ai.brain.product_discovery_gate import (  # noqa: PLC0415
            has_explicit_broad_browse_request,
            has_types_overview_ask,
        )

        if has_explicit_broad_browse_request(raw):
            return True
        if has_types_overview_ask(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — catalog-only probe is best-effort
        pass
    return False


def _has_active_order_or_order_prep_context(ctx: Any) -> bool:
    """True when an active order funnel should beat generic catalog delivery."""
    state = getattr(ctx, "state", None)
    if state is None:
        return False

    prep = getattr(state, "order_prep", None)
    prep_product_id = str(getattr(prep, "product_id", "") or "").strip()
    if prep_product_id:
        return True

    stage = str(getattr(state, "stage", "") or "").strip().lower()
    focus = dict(getattr(state, "current_product_focus", None) or {})
    has_focus = bool(focus.get("id") or focus.get("product_id") or focus.get("title"))

    if stage in _ACTIVE_ORDER_STAGES:
        if prep is not None and (
            prep_product_id
            or getattr(prep, "customer_first_name", "")
            or getattr(prep, "city", "")
            or getattr(prep, "short_address_code", "")
        ):
            return True
        if has_focus:
            return True

    message = str(getattr(ctx, "message", "") or "").strip()
    if _ORDER_CONTINUATION_RE.search(message) and (
        prep_product_id or stage in _ACTIVE_ORDER_STAGES
    ):
        return True

    order_signals = bool(prep_product_id or has_focus or stage in _ACTIVE_ORDER_STAGES)
    if not order_signals:
        return False

    try:
        from modules.ai.brain.commerce.commerce_entry_orchestrator import (  # noqa: PLC0415
            CustomerAction,
            classify_customer_action,
        )
        from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
            extract_status_reply_quantity,
        )

        action = classify_customer_action(
            message,
            quantity_hint=extract_status_reply_quantity(message),
            has_product_focus=has_focus,
        )
        if action in {CustomerAction.BUY, CustomerAction.QUANTITY}:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — buy/qty probe is best-effort
        pass

    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name == "start_order":
        return True

    return False


def _order_context_blocks_catalog_delivery(ctx: Any, message: str) -> bool:
    """CE2 must defer when order_prep/focus expects propose_draft_order recovery."""
    if not _has_active_order_or_order_prep_context(ctx):
        return False
    if _is_status_product_delivery_request(message, state=getattr(ctx, "state", None)):
        focus = dict(getattr(getattr(ctx, "state", None), "current_product_focus", None) or {})
        if focus.get("from_status_reply") or focus.get("title"):
            return False
    return not _is_explicit_catalog_only_request(message)


def _is_status_product_delivery_request(message: str, *, state: Any = None) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    if has_pending_catalog_send(state) and is_catalog_send_confirmation(raw):
        return False
    if is_catalog_send_confirmation(raw):
        return False
    return bool(_SEND_PRODUCT_ITEM_RE.search(raw))


def _status_ce1_should_own(ctx: Any) -> bool:
    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    if not focus.get("from_status_reply"):
        try:
            from modules.ai.brain.commerce.status_reply_product_context import (  # noqa: PLC0415
                get_persisted_status_reply_context,
            )

            if not get_persisted_status_reply_context(state).get("active"):
                return False
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            return False

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


def _is_catalog_miss_complaint(ctx: Any) -> bool:
    message = str(getattr(ctx, "message", "") or "").strip()
    if not message:
        return False
    try:
        from modules.ai.brain.state.product_correction import (  # noqa: PLC0415
            parse_product_correction,
        )

        if parse_product_correction(message).detected:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    if not _CATALOG_MISS_COMPLAINT_RE.search(message):
        return False
    state = getattr(ctx, "state", None)
    return _had_recent_catalog_delivery(state)


def _try_delegate_kb_availability(ctx: Any) -> Optional[Any]:
    try:
        from modules.ai.brain.commerce.non_catalog_availability_kb_route import (  # noqa: PLC0415
            try_non_catalog_availability_kb_decision,
        )

        kb_dec = try_non_catalog_availability_kb_decision(
            ctx,
            route="commerce_entry_catalog_delivery",
        )
        if kb_dec is None:
            return None
        args = dict(getattr(kb_dec, "args", None) or {})
        args["catalog_delivery_kind"] = CatalogDeliveryKind.DELEGATE_KB_AVAILABILITY.value
        args["commerce_entry_owner"] = TOPIC_COMMERCE_ENTRY_CATALOG
        try:
            kb_dec.args = args  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001  # noqa: silent-ok — KB decision args patch is best-effort
            pass
        return kb_dec
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — KB delegate is best-effort
        logger.debug("[COMMERCE_ENTRY_CATALOG] KB delegate failed err=%s", exc)
        return None


def _match_catalog_product(ctx: Any, query: str) -> Optional[Dict[str, Any]]:
    db = getattr(ctx, "_db", None)
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    q = (query or "").strip()
    if not q or not db or not tenant_id:
        return None
    norm_q = _norm(q)
    if len(norm_q) < 2:
        return None
    try:
        from database.models import Product  # noqa: PLC0415
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        try:
            from models import Product  # noqa: PLC0415
        except Exception:  # noqa: BLE001  # noqa: silent-ok
            return None
    try:
        rows = (
            db.query(Product)
            .filter(Product.tenant_id == int(tenant_id))
            .limit(500)
            .all()
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        return None
    best = None
    best_len = 0
    for row in rows:
        title = str(getattr(row, "title", "") or "").strip()
        nt = _norm(title)
        if len(nt) < 2:
            continue
        if nt in norm_q or norm_q in nt:
            if len(nt) > best_len:
                best = row
                best_len = len(nt)
    if best is None:
        return None
    return {
        "id": getattr(best, "id", None),
        "title": str(getattr(best, "title", "") or ""),
        "price": getattr(best, "price", None),
        "meta_retailer_id": getattr(best, "meta_retailer_id", None),
        "catalog_match_confidence": "title_substring",
    }


def _extract_named_product_query(message: str) -> str:
    raw = (message or "").strip()
    if not raw:
        return ""
    try:
        from modules.ai.brain.product_discovery_gate import extract_inquiry_product_query  # noqa: PLC0415

        hit = extract_inquiry_product_query(raw)
        if hit:
            return hit.strip()
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    m = re.search(
        r"(?:هل|(?:في(?:ه)?|fيه))\s+(?:عند(?:كم|ك)\s+)?(.+?)\s*(?:متوفر|موجود|[؟?])",
        raw,
        re.UNICODE | re.IGNORECASE,
    )
    if m:
        return (m.group(1) or "").strip(" ؟?!.")
    m = re.search(
        r"(?:أ?رسل|ابع|(?:هل|في(?:ه)?|فيه))\s+(?:ال)?(.{2,50}?)(?:\s*(?:متوفر|موجود|[؟?])|$)",
        raw,
        re.UNICODE | re.IGNORECASE,
    )
    if m:
        return (m.group(1) or "").strip(" ؟?!.")
    return ""


def _decision_with_kind(decision: Any, kind: CatalogDeliveryKind) -> Any:
    args = dict(getattr(decision, "args", None) or {})
    args["catalog_delivery_kind"] = kind.value
    args["commerce_entry_owner"] = TOPIC_COMMERCE_ENTRY_CATALOG
    try:
        decision.args = args  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001  # noqa: silent-ok — decision args patch is best-effort
        pass
    return decision


def _resolve_send_catalog(ctx: Any) -> Optional[Any]:
    from modules.ai.brain.catalog.navigation import STEP_NATIVE_CATALOG_ENTRY  # noqa: PLC0415
    from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    state = getattr(ctx, "state", None)
    clear_pending_catalog_send(state)
    _clear_catalog_delivery_block(state)
    try:
        from modules.ai.brain.catalog.navigation import (  # noqa: PLC0415
            OWNER_STEP_NATIVE_CATALOG,
            PATH_NATIVE_CATALOG,
            _try_native_catalog_entry_decision,
        )

        native = _try_native_catalog_entry_decision(
            ctx,
            owner_step=OWNER_STEP_NATIVE_CATALOG,
            fallback_path=PATH_NATIVE_CATALOG,
            reason="commerce_entry_catalog — explicit catalog browse",
            confidence=0.93,
        )
        if native is not None:
            _mark_catalog_sent(getattr(ctx, "state", None), kind=CatalogDeliveryKind.SEND_CATALOG.value)
            return _decision_with_kind(native, CatalogDeliveryKind.SEND_CATALOG)
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — native catalog entry is best-effort
        logger.debug("[COMMERCE_ENTRY_CATALOG] native entry failed err=%s", exc)

    _mark_catalog_sent(getattr(ctx, "state", None), kind=CatalogDeliveryKind.SEND_CATALOG.value)
    return Decision(
        action=ACTION_CATALOG_NAVIGATE,
        args={
            "catalog_delivery_kind": CatalogDeliveryKind.SEND_CATALOG.value,
            "commerce_entry_owner": TOPIC_COMMERCE_ENTRY_CATALOG,
            "navigator_step": STEP_NATIVE_CATALOG_ENTRY,
            "turn_owner": "commerce_entry_catalog_delivery",
            "owner_locked": True,
            "chosen_path": "commerce_entry_send_catalog",
            "owner_step": "send_catalog",
        },
        reason="commerce_entry_catalog — explicit catalog browse",
        confidence=0.92,
    )


def _resolve_send_product_item(
    ctx: Any,
    product: Dict[str, Any],
    *,
    source: str = "commerce_entry_catalog_product",
) -> Any:
    from modules.ai.brain.decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: PLC0415
    from modules.ai.brain.types import Decision  # noqa: PLC0415

    title = str(product.get("title") or "").strip()
    return Decision(
        action=ACTION_SEARCH_PRODUCTS,
        args={
            "query": title,
            "source": source,
            "matched_product": dict(product),
            "catalog_delivery_kind": CatalogDeliveryKind.SEND_PRODUCT_CATALOG_ITEM.value,
            "commerce_entry_owner": TOPIC_COMMERCE_ENTRY_CATALOG,
            "force_product_card": True,
        },
        reason="commerce_entry_catalog — send specific catalog product item",
        confidence=0.93,
    )


def _resolve_specific_product_delivery(ctx: Any) -> Optional[Any]:
    message = str(getattr(ctx, "message", "") or "").strip()
    query = _extract_named_product_query(message)
    if not query:
        return None

    try:
        from modules.ai.brain.product_discovery_gate import is_generic_category_noun  # noqa: PLC0415

        if is_generic_category_noun(query) and _is_explicit_catalog_browse_request(message, ctx):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass

    product = _match_catalog_product(ctx, query)
    if product is None:
        return None

    if _CATALOG_PRODUCT_AVAIL_RE.search(message) or _SHOW_CATEGORY_BROWSE_RE.search(message):
        return _resolve_send_product_item(ctx, product)

    if (
        not is_catalog_send_confirmation(message)
        and re.search(r"(?:أ?رسل|ابع)\s+\S", message, re.I)
    ):
        return _resolve_send_product_item(ctx, product)

    norm_q = _norm(query)
    if len(norm_q.split()) >= 2 and _norm(message).find(norm_q) >= 0:
        if _CATALOG_PRODUCT_AVAIL_RE.search(message) or "متوفر" in _norm(message):
            return _resolve_send_product_item(ctx, product)

    return None


def try_commerce_entry_catalog_decision(ctx: Any) -> Optional[Any]:
    """
    Unified catalog delivery owner — returns a Decision or None to delegate.

    Blocks catalog for KB non-catalog subjects, catalog-miss complaints, and
    product-knowledge turns. Sends catalog or product items on explicit asks.
    """
    message = str(getattr(ctx, "message", "") or "").strip()
    state = getattr(ctx, "state", None)

    try:
        from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: PLC0415
            current_turn_has_payment_evidence,
        )

        if current_turn_has_payment_evidence(ctx):
            logger.info(
                "[COMMERCE_ENTRY_CATALOG] blocked payment_evidence tenant=%s",
                getattr(ctx, "tenant_id", None),
            )
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — payment block probe is best-effort
        pass

    if not message:
        return None

    facts = getattr(ctx, "facts", None)
    if facts is not None and not bool(getattr(facts, "has_products", False)):
        return None

    if has_pending_catalog_send(state) and is_catalog_send_confirmation(message):
        logger.info(
            "[COMMERCE_ENTRY_CATALOG] pending_catalog_confirmation tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return _resolve_send_catalog(ctx)

    if _is_product_knowledge_or_comparison(message):
        _set_catalog_delivery_block(state, CatalogDeliveryKind.BLOCK_NON_CATALOG_SUBJECT.value)
        logger.info(
            "[COMMERCE_ENTRY_CATALOG] blocked product_knowledge tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return None

    if _is_catalog_miss_complaint(ctx):
        _set_catalog_delivery_block(state, CatalogDeliveryKind.BLOCK_CATALOG_MISS_COMPLAINT.value)
        kb = _try_delegate_kb_availability(ctx)
        if kb is not None:
            return kb
        logger.info(
            "[COMMERCE_ENTRY_CATALOG] catalog_miss_complaint tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return None

    kb_dec = _try_delegate_kb_availability(ctx)
    if kb_dec is not None:
        _set_catalog_delivery_block(state, CatalogDeliveryKind.BLOCK_NON_CATALOG_SUBJECT.value)
        return kb_dec

    if _is_explicit_catalog_browse_request(message, ctx):
        if _catalog_block_reason(state) == "payment_evidence":
            _clear_catalog_delivery_block(state)
        return _resolve_send_catalog(ctx)

    if catalog_delivery_is_blocked(ctx):
        return None

    if _status_ce1_should_own(ctx) and not _is_status_product_delivery_request(message, state=state):
        return None

    if _order_context_blocks_catalog_delivery(ctx, message):
        logger.info(
            "[COMMERCE_ENTRY_CATALOG] defer — active order_prep/order context tenant=%s preview=%r",
            getattr(ctx, "tenant_id", None),
            message[:60],
        )
        return None

    focus = dict(getattr(state, "current_product_focus", None) or {})
    if _is_status_product_delivery_request(message, state=state) and (
        focus.get("from_status_reply") or focus.get("title")
    ):
        product = focus if focus.get("title") else None
        if product is None:
            return None
        from modules.ai.brain.commerce.commerce_entry_orchestrator import (  # noqa: PLC0415
            enrich_product_focus_from_catalog,
        )

        enriched = enrich_product_focus_from_catalog(
            getattr(ctx, "_db", None),
            int(getattr(ctx, "tenant_id", 0) or 0),
            product,
        )
        if enriched.get("title"):
            state.current_product_focus = enriched
            return _resolve_send_product_item(
                ctx,
                enriched,
                source="commerce_entry_status_product_delivery",
            )

    specific = _resolve_specific_product_delivery(ctx)
    if specific is not None:
        return specific

    return None


__all__ = [
    "TOPIC_COMMERCE_ENTRY_CATALOG",
    "CatalogDeliveryKind",
    "block_catalog_delivery",
    "catalog_delivery_is_blocked",
    "clear_pending_catalog_send",
    "get_pending_catalog_send",
    "has_pending_catalog_send",
    "is_catalog_confirmation_bot_reply",
    "is_catalog_send_confirmation",
    "pin_pending_catalog_send",
    "try_commerce_entry_catalog_decision",
]
