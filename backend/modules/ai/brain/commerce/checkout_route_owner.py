"""
checkout_route_owner.py
───────────────────────
Deterministic checkout channel ownership before staff/location routing.

Flow: purchase intent → channel choice (whatsapp_fast | store_link |
showroom_visit) → catalog/payment/shipping OR store link OR showroom policies.

Platform-wide — evidence from order_prep state + tenant capabilities, not
merchant-specific phrase lists.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.brain.checkout_route_owner")

_FLAG = "CHECKOUT_ROUTE_OWNER_ENABLED"
_FLAG_FALSY = frozenset({"0", "false", "no", "off"})

CHECKOUT_CHANNEL_WHATSAPP = "whatsapp_fast"
CHECKOUT_CHANNEL_STORE = "store_link"
CHECKOUT_CHANNEL_SHOWROOM = "showroom_visit"
CHECKOUT_CHANNEL_INQUIRY = "inquiry"

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_PURCHASE_INTENT_RE = re.compile(
    r"(?:"
    r"طلب|اطلب|اشتري|شراء|"
    r"اب(?:ي|غ(?:ى|a)?)\s*(?:اطلب|اشتري|اطلبه|اطلبها)?|"
    r"بغ(?:يت|ى)\s*(?:اطلب|اشتري)?|"
    r"اريد|أريد|ودي|بدي|"
    r"دفع|تحويل|حوال(?:ه|ة)|"
    r"payment|pay\s+now|buy|order|purchase|"
    r"كيف\s+(?:اطلب|أطلب|اشتري|أشتري|ادفع|أدفع)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_PAYMENT_ASK_RE = re.compile(
    r"(?:"
    r"طرق\s*(?:ال)?دفع|طريقة\s*(?:ال)?دفع|"
    r"كيف\s+(?:ادفع|أدفع|الدفع)|"
    r"حساب\s*(?:ال)?(?:تحويل|بنك)|"
    r"iban|آيبان|ايبان|"
    r"payment\s*method"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CHECKOUT_ENTRY_RE = re.compile(
    r"(?:"
    r"\b(?:ابي|ابغي|ابغى|أبي|أبغى|بغيت|ودي|اريد|أريد)\s*(?:اطلب|أطلب|اشتري|أشتري)\b|"
    r"\bكيف\s*(?:اطلب|أطلب|اشتري|أشتري)\b|"
    r"^\s*(?:اطلب|أطلب|طلب|اشتري|أشتري|شراء)\s*$"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CHANNEL_INQUIRY_RE = re.compile(
    r"(?:checkout_inquiry|^\s*3\s*$|استفسار|سؤال|عندي\s*استفسار|لدي\s*استفسار)",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_HELP_RE = re.compile(
    r"(?:"
    r"^\s*[\?؟]+\s*$|"
    r"وين\s*(?:هي|هو|ه|ها)?|"
    r"ما\s*(?:ظهر|وصل|طلع)|"
    r"كيف\s*(?:اختار|أختار)|"
    r"ابي\s*اشوف\s*المنتجات|أبي\s*أشوف\s*المنتجات|"
    r"ابغى\s*اشوف\s*المنتجات|أبغى\s*أشوف\s*المنتجات"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CATALOG_SEND_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:ارسل|أرسل|ابعث|أبعث|ورني|ورّني|أرني|ارني|ابي|أبي|ابغ|أبغ)\s*(?:لي\s*)?"
    r"(?:ال)?(?:كتالوج|catalog)|"
    r"(?:كتالوج|catalog)\s*(?:ارسل|أرسل|ابعث|أبعث)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CHANNEL_WHATSAPP_RE = re.compile(
    r"(?:"
    r"checkout_whatsapp_fast|"
    r"^\s*1\s*$|"
    r"واتس(?:اب|)|whatsapp|"
    r"طلب\s*سريع|"
    r"من\s*ه(?:نا|ني)|"
    r"اكمل\s*(?:ه(?:نا|ني)|بالواتس)|"
    r"أ?كمل\s*(?:ه(?:نا|ني)|بالواتس)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CHANNEL_STORE_RE = re.compile(
    r"(?:"
    r"checkout_store_link|"
    r"^\s*2\s*$|"
    r"المتجر|متجر(?:ي|كم|ك)?|"
    r"رابط\s*(?:المتجر|الموقع|الشراء|الطلب)|"
    r"store\s*link|online\s*store|website"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_CHANNEL_SHOWROOM_RE = re.compile(
    r"(?:"
    r"^\s*3\s*$|"
    r"المعرض|الفرع|"
    r"زي(?:ارة|اره)|"
    r"استلام\s*من|أ?ستلم\s*من|"
    r"showroom|branch\s*visit"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def checkout_route_owner_enabled() -> bool:
    raw = os.getenv(_FLAG, "1").strip().lower()
    return raw not in _FLAG_FALSY


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


@dataclass(frozen=True)
class CheckoutChannelCapabilities:
    whatsapp_fast: bool = True
    store_link: bool = False
    showroom_visit: bool = False
    store_url: str = ""
    store_name: str = ""


@dataclass(frozen=True)
class CheckoutRouteDecision:
    reply_text: str
    reason: str
    skip_brain: bool = True
    checkout_channel: str = ""
    persist_awaiting_channel: bool = False
    clear_awaiting_channel: bool = False
    buttons: Sequence[Dict[str, Any]] = field(default_factory=tuple)
    cta_label: str = ""
    cta_url: str = ""
    metadata_path: str = "checkout_route_owner"


def load_checkout_route_context(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
) -> Tuple[str, Dict[str, Any]]:
    """Return ``(stage, order_prep_dict)`` from persisted brain state."""
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _, brain_state = _load_brain_state(
            db,
            tenant_id=int(tenant_id or 0),
            phone=str(customer_phone or ""),
        )
        bs = brain_state or {}
        stage = str(bs.get("stage") or "").strip().lower()
        op = bs.get("order_prep") or {}
        if not isinstance(op, dict):
            op = {}
        op = dict(op)
        op["_catalog_navigation_source"] = str(bs.get("catalog_navigation_source") or "")
        op["_native_catalog_send_failed"] = bool(bs.get("native_catalog_send_failed"))
        return stage, op
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CHECKOUT_ROUTE] brain_state load skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return "", {}


def _checkout_channel(order_prep: Dict[str, Any]) -> str:
    return str(order_prep.get("checkout_channel") or "").strip().lower()


def _awaiting_channel(order_prep: Dict[str, Any]) -> bool:
    return bool(order_prep.get("awaiting_checkout_channel"))


def load_channel_capabilities(db: Any, tenant_id: int) -> CheckoutChannelCapabilities:
    store_url = ""
    store_name = ""
    try:
        from database.models import StoreKnowledgeSnapshot, TenantSettings  # noqa: PLC0415
        from core.store_display import clean_store_name  # noqa: PLC0415

        snap = (
            db.query(StoreKnowledgeSnapshot)
            .filter(StoreKnowledgeSnapshot.tenant_id == tenant_id)
            .first()
        )
        if snap and snap.store_profile:
            profile = snap.store_profile or {}
            store_url = str(profile.get("store_url") or "").strip()
            store_name = clean_store_name(profile.get("store_name", "") or "")

        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if settings:
            store_cfg = dict(settings.store_settings or {})
            if not store_url:
                store_url = str(store_cfg.get("store_url") or "").strip()
            if not store_name:
                store_name = clean_store_name(store_cfg.get("store_name") or "")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CHECKOUT_ROUTE] capabilities load skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )

    showroom = False
    try:
        from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
            structured_branch_contacts_enabled,
            tenant_has_structured_branch_data,
        )

        if structured_branch_contacts_enabled():
            showroom = tenant_has_structured_branch_data(db, int(tenant_id or 0))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional branch capability probe must not block route owner
        showroom = False

    return CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=bool(store_url),
        showroom_visit=showroom,
        store_url=store_url,
        store_name=store_name,
    )


def available_channels(caps: CheckoutChannelCapabilities) -> List[str]:
    out: List[str] = []
    if caps.whatsapp_fast:
        out.append(CHECKOUT_CHANNEL_WHATSAPP)
    if caps.store_link:
        out.append(CHECKOUT_CHANNEL_STORE)
    if caps.showroom_visit:
        out.append(CHECKOUT_CHANNEL_SHOWROOM)
    return out


def has_checkout_route_intent(message: str) -> bool:
    """True when the turn should enter checkout channel selection."""
    raw = (message or "").strip()
    if not raw:
        return False

    try:
        from modules.ai.brain.intent.rules import (  # noqa: PLC0415
            INTENT_GREETING,
            INTENT_SOCIAL,
            is_pure_greeting_without_commerce,
            match as match_intent,
        )

        if is_pure_greeting_without_commerce(raw):
            return False
        intent = match_intent(raw)
        if intent and intent.name in {INTENT_GREETING, INTENT_SOCIAL}:
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional greeting filter must not block route intent
        pass

    norm = _norm(raw)
    if _PAYMENT_ASK_RE.search(norm):
        return True

    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            has_explicit_order_select_signal,
            has_price_inquiry_signal,
            is_commerce_inquiry_turn,
        )

        if has_price_inquiry_signal(raw):
            return False
        if is_commerce_inquiry_turn(raw) and not has_explicit_order_select_signal(raw):
            return False
        if has_explicit_order_select_signal(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional order-select probe must not block route intent
        pass

    norm = _norm(raw)
    if _PURCHASE_INTENT_RE.search(norm):
        return True
    if _PAYMENT_ASK_RE.search(norm):
        return True

    try:
        from modules.ai.brain.intent import rules  # noqa: PLC0415

        intent = rules.match(raw)
        if intent and intent.name in {
            "start_order",
            "add_to_cart",
        }:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional intent rules probe must not block route intent
        pass

    return False


def has_checkout_entry_intent(message: str) -> bool:
    """True for explicit order-entry phrases that should ask channel first."""
    raw = (message or "").strip()
    if not raw:
        return False
    try:
        from modules.ai.brain.intent.rules import is_pure_greeting_without_commerce  # noqa: PLC0415

        if is_pure_greeting_without_commerce(raw):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional greeting filter must not block route intent
        pass
    return bool(_CHECKOUT_ENTRY_RE.search(_norm(raw)))


def is_catalog_visibility_question(message: str) -> bool:
    """Customer is asking where the promised catalog/options are."""
    raw = (message or "").strip()
    if not raw:
        return False
    if is_catalog_send_request(raw):
        return False
    return bool(_CATALOG_HELP_RE.search(_norm(raw)))


def is_catalog_send_request(message: str) -> bool:
    """Explicit ask to send/show the native catalog again."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_CATALOG_SEND_REQUEST_RE.search(_norm(raw)))


def _compose_prior_catalog_fallback_decision(
    db: Any,
    tenant_id: int,
    *,
    failure_reason: str = "prior_native_catalog_failed",
) -> "CheckoutRouteDecision":
    from core.native_catalog_fallback import compose_native_catalog_failure_decision  # noqa: PLC0415

    fb = compose_native_catalog_failure_decision(
        db,
        int(tenant_id or 0),
        failure_reason=failure_reason,
    )
    caps = load_channel_capabilities(db, int(tenant_id or 0))
    buttons = () if fb.cta_url else build_channel_choice_buttons(caps)
    return CheckoutRouteDecision(
        reply_text=str(fb.text or "").strip(),
        reason="catalog_visibility_help_prior_catalog",
        cta_url=str(fb.cta_url or "").strip(),
        cta_label=str(fb.cta_label or "").strip() or "فتح المتجر الإلكتروني",
        buttons=buttons,
    )


def parse_checkout_channel_choice(
    message: str,
    *,
    caps: CheckoutChannelCapabilities,
) -> Optional[str]:
    """Map inbound text to a checkout channel when awaiting choice."""
    raw = (message or "").strip()
    if not raw:
        return None
    norm = _norm(raw)

    channels = available_channels(caps)
    if CHECKOUT_CHANNEL_WHATSAPP in channels and _CHANNEL_WHATSAPP_RE.search(norm):
        return CHECKOUT_CHANNEL_WHATSAPP
    if CHECKOUT_CHANNEL_STORE in channels and _CHANNEL_STORE_RE.search(norm):
        return CHECKOUT_CHANNEL_STORE
    if _CHANNEL_INQUIRY_RE.search(norm):
        return CHECKOUT_CHANNEL_INQUIRY
    if CHECKOUT_CHANNEL_SHOWROOM in channels and _CHANNEL_SHOWROOM_RE.search(norm):
        return CHECKOUT_CHANNEL_SHOWROOM

    if len(channels) == 1 and not re.fullmatch(r"\d+", norm):
        return channels[0]

    if re.fullmatch(r"\d+", norm):
        idx = int(norm) - 1
        if 0 <= idx < len(channels):
            return channels[idx]
    return None


def build_channel_choice_prompt(caps: CheckoutChannelCapabilities) -> str:
    """Deterministic channel-choice question based on tenant capabilities."""
    lines: List[str] = []
    options: List[str] = []
    if caps.whatsapp_fast:
        options.append("1- طلب سريع عبر واتساب")
    if caps.store_link:
        options.append("2- الطلب من المتجر الإلكتروني")
    options.append("3- لدي استفسار")

    if not options:
        options.append("1- طلب سريع عبر واتساب")

    lines.append("كيف تحب تكمل؟")
    lines.extend(options)
    return "\n".join(lines)


def build_channel_choice_buttons(caps: CheckoutChannelCapabilities) -> Tuple[Dict[str, Any], ...]:
    buttons: List[Dict[str, Any]] = []
    if caps.whatsapp_fast:
        buttons.append({
            "type": "reply",
            "reply": {"id": "checkout_whatsapp_fast", "title": "طلب سريع واتساب"},
        })
    if caps.store_link:
        buttons.append({
            "type": "reply",
            "reply": {"id": "checkout_store_link", "title": "فتح المتجر"},
        })
    buttons.append({
        "type": "reply",
        "reply": {"id": "checkout_inquiry", "title": "عندي استفسار"},
    })
    return tuple(buttons[:3])


def build_catalog_visibility_reply(caps: CheckoutChannelCapabilities) -> str:
    if caps.store_link:
        return (
            "إذا ما ظهر لك الكتالوج، اختر الطريقة اللي تناسبك:\n"
            "1- أكمل طلبك هنا بالواتساب\n"
            "2- افتح المتجر الإلكتروني\n"
            "3- عندك استفسار"
        )
    return (
        "إذا ما ظهر لك الكتالوج، نكمل طلبك هنا بالواتساب.\n"
        "قل لي المنتج اللي تبيه، أو اختر عندك استفسار."
    )


def build_store_link_reply(caps: CheckoutChannelCapabilities) -> str:
    if caps.store_url:
        return T_faq_store_info(caps.store_name, caps.store_url)
    return "رابط المتجر غير مهيأ حالياً. تقدر تكمل الطلب من واتساب."


def T_faq_store_info(store_name: str, store_url: str) -> str:
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    return T.faq_store_info(
        store_name=store_name,
        store_url=store_url,
        store_description="",
    )


def persist_checkout_route_state(
    db: Any,
    *,
    tenant_id: int,
    phone: str,
    checkout_channel: str = "",
    awaiting_checkout_channel: Optional[bool] = None,
) -> bool:
    if not db or not tenant_id or not phone:
        return False
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415
        from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

        conv, bs = _load_brain_state(db, tenant_id=tenant_id, phone=phone)
        if conv is None:
            return False
        bs = dict(bs or {})
        op = dict(bs.get("order_prep") or {})
        if checkout_channel:
            op["checkout_channel"] = checkout_channel
        if awaiting_checkout_channel is not None:
            op["awaiting_checkout_channel"] = bool(awaiting_checkout_channel)
            op["checkout_route_prompt_sent"] = bool(awaiting_checkout_channel)
        bs["order_prep"] = op
        meta = dict(getattr(conv, "extra_metadata", None) or {})
        meta["brain_state"] = bs
        conv.extra_metadata = meta
        flag_modified(conv, "extra_metadata")
        db.add(conv)
        db.flush()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CHECKOUT_ROUTE] persist failed tenant=%s err=%s",
            tenant_id,
            exc,
        )
        return False


def _active_whatsapp_checkout(
    *,
    stage: str,
    order_prep: Dict[str, Any],
) -> bool:
    channel = _checkout_channel(order_prep)
    if channel == CHECKOUT_CHANNEL_WHATSAPP:
        return True
    if channel == CHECKOUT_CHANNEL_STORE:
        return False
    try:
        from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
            is_active_order_flow,
        )

        return is_active_order_flow(stage=stage, order_prep=order_prep)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional active-order probe must not block defer decision
        return False


def should_defer_staff_location_for_checkout_route(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
) -> bool:
    """
    Return True when staff/location pre-brain policies must yield to checkout route.

    Does not block explicit staff/contact asks or showroom channel selection.
    """
    if not checkout_route_owner_enabled():
        return False

    raw = (message or "").strip()
    stage, order_prep = load_checkout_route_context(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
    )
    channel = _checkout_channel(order_prep)

    try:
        from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
            has_strong_prebrain_contact_intent,
            should_yield_prebrain_to_order_flow,
        )

        if has_strong_prebrain_contact_intent(raw):
            return False
        if should_yield_prebrain_to_order_flow(
            db,
            tenant_id=int(tenant_id or 0),
            customer_phone=customer_phone or "",
            message=raw,
        ):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional arbiter probe must not block defer decision
        pass

    if channel == CHECKOUT_CHANNEL_SHOWROOM:
        return False

    if _awaiting_channel(order_prep):
        return True

    try:
        from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
            has_strong_prebrain_contact_intent as _strong_contact,
        )

        if channel == CHECKOUT_CHANNEL_STORE and not _strong_contact(raw):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional strong-contact probe must not block store defer
        if channel == CHECKOUT_CHANNEL_STORE:
            return True

    if not channel and has_checkout_entry_intent(raw):
        return True

    if _active_whatsapp_checkout(stage=stage, order_prep=order_prep):
        if channel in {"", CHECKOUT_CHANNEL_WHATSAPP}:
            return True

    return False


def evaluate_checkout_route_owner(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    message: str,
) -> Optional[CheckoutRouteDecision]:
    """Pre-brain checkout channel owner — ask channel or deliver store link."""
    try:
        from modules.ai.order_flow_v2.flags import should_skip_legacy_order_flow_reply  # noqa: PLC0415

        if should_skip_legacy_order_flow_reply():
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — V2 gate must not block legacy when import fails
        pass

    if not checkout_route_owner_enabled():
        return None

    raw = (message or "").strip()
    if not raw:
        return None

    stage, order_prep = load_checkout_route_context(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
    )
    channel = _checkout_channel(order_prep)
    caps = load_channel_capabilities(db, int(tenant_id or 0))

    if channel == CHECKOUT_CHANNEL_SHOWROOM:
        return None

    if _awaiting_channel(order_prep):
        picked = parse_checkout_channel_choice(raw, caps=caps)
        if not picked:
            catalog_help = is_catalog_visibility_question(raw)
            if not catalog_help and has_checkout_route_intent(raw):
                persist_checkout_route_state(
                    db,
                    tenant_id=tenant_id,
                    phone=customer_phone,
                    checkout_channel=CHECKOUT_CHANNEL_WHATSAPP,
                    awaiting_checkout_channel=False,
                )
                return None
            return CheckoutRouteDecision(
                reply_text=(
                    build_catalog_visibility_reply(caps)
                    if catalog_help
                    else build_channel_choice_prompt(caps)
                ),
                reason="catalog_visibility_help" if catalog_help else "channel_choice_repeat",
                persist_awaiting_channel=True,
                buttons=build_channel_choice_buttons(caps),
            )
        if picked == CHECKOUT_CHANNEL_STORE:
            persist_checkout_route_state(
                db,
                tenant_id=tenant_id,
                phone=customer_phone,
                checkout_channel=picked,
                awaiting_checkout_channel=False,
            )
            return CheckoutRouteDecision(
                reply_text=build_store_link_reply(caps),
                reason="store_link_delivered",
                checkout_channel=picked,
                clear_awaiting_channel=True,
                cta_label="فتح المتجر الإلكتروني",
                cta_url=caps.store_url,
            )
        if picked == CHECKOUT_CHANNEL_INQUIRY:
            persist_checkout_route_state(
                db,
                tenant_id=tenant_id,
                phone=customer_phone,
                awaiting_checkout_channel=False,
            )
            return None
        persist_checkout_route_state(
            db,
            tenant_id=tenant_id,
            phone=customer_phone,
            checkout_channel=picked,
            awaiting_checkout_channel=False,
        )
        if picked == CHECKOUT_CHANNEL_WHATSAPP:
            return CheckoutRouteDecision(
                reply_text="تمام، نكمل طلبك هنا بالواتساب. وش المنتج اللي تبي تطلبه؟",
                reason="whatsapp_fast_selected",
                checkout_channel=picked,
                clear_awaiting_channel=True,
            )
        if picked == CHECKOUT_CHANNEL_SHOWROOM:
            return None
        return None

    if channel == CHECKOUT_CHANNEL_STORE:
        return None

    if channel == CHECKOUT_CHANNEL_WHATSAPP:
        if is_catalog_send_request(raw):
            return None
        if is_catalog_visibility_question(raw):
            return CheckoutRouteDecision(
                reply_text=build_catalog_visibility_reply(caps),
                reason="catalog_visibility_help_active_whatsapp",
                buttons=build_channel_choice_buttons(caps),
            )
        return None

    try:
        from modules.ai.brain.commerce.prebrain_order_flow_arbiter import (  # noqa: PLC0415
            is_active_order_flow,
        )

        if is_active_order_flow(stage=stage, order_prep=order_prep):
            return None
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional active-order probe must not block channel ask
        pass
        return None

    channels = available_channels(caps)
    if not has_checkout_entry_intent(raw):
        if is_catalog_send_request(raw):
            return None
        if is_catalog_visibility_question(raw) and (
            bool(order_prep.get("checkout_route_prompt_sent"))
            or str(order_prep.get("_catalog_navigation_source") or "").strip()
            or bool(order_prep.get("_native_catalog_send_failed"))
        ):
            return _compose_prior_catalog_fallback_decision(
                db,
                int(tenant_id or 0),
            )
        return None

    persist_checkout_route_state(
        db,
        tenant_id=tenant_id,
        phone=customer_phone,
        awaiting_checkout_channel=True,
    )
    logger.info(
        "[CHECKOUT_ROUTE] ask_channel tenant=%s preview=%r channels=%s",
        tenant_id,
        raw[:80],
        channels,
    )
    return CheckoutRouteDecision(
        reply_text=build_channel_choice_prompt(caps),
        reason="ask_checkout_channel",
        persist_awaiting_channel=True,
        buttons=build_channel_choice_buttons(caps),
    )


__all__ = [
    "CHECKOUT_CHANNEL_INQUIRY",
    "CHECKOUT_CHANNEL_SHOWROOM",
    "CHECKOUT_CHANNEL_STORE",
    "CHECKOUT_CHANNEL_WHATSAPP",
    "CheckoutChannelCapabilities",
    "CheckoutRouteDecision",
    "available_channels",
    "build_catalog_visibility_reply",
    "build_channel_choice_buttons",
    "build_channel_choice_prompt",
    "checkout_route_owner_enabled",
    "evaluate_checkout_route_owner",
    "has_checkout_entry_intent",
    "has_checkout_route_intent",
    "is_catalog_send_request",
    "is_catalog_visibility_question",
    "load_channel_capabilities",
    "load_checkout_route_context",
    "parse_checkout_channel_choice",
    "persist_checkout_route_state",
    "should_defer_staff_location_for_checkout_route",
]
