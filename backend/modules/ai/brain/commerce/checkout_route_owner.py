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
    r"(?:checkout_inquiry|استفسار|سؤال|عندي\s*استفسار|لدي\s*استفسار)",
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
    r"checkout_showroom_visit|"
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
    """Return ``(stage, order_prep_dict)`` from persisted brain state.

    ``order_prep`` may include ``_brain_state`` (full dict) for storefront
    product-url resolution — not persisted back to DB.
    """
    try:
        from core.order_flow import _load_brain_state  # noqa: PLC0415

        _, brain_state = _load_brain_state(
            db,
            tenant_id=int(tenant_id or 0),
            phone=str(customer_phone or ""),
        )
        bs = brain_state or {}
        if not isinstance(bs, dict):
            bs = {}
        stage = str(bs.get("stage") or "").strip().lower()
        op = bs.get("order_prep") or {}
        if not isinstance(op, dict):
            op = {}
        op = dict(op)
        op["_catalog_navigation_source"] = str(bs.get("catalog_navigation_source") or "")
        op["_native_catalog_send_failed"] = bool(bs.get("native_catalog_send_failed"))
        op["_brain_state"] = dict(bs)
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
            store_name = clean_store_name(profile.get("store_name", "") or "")

        settings = (
            db.query(TenantSettings)
            .filter(TenantSettings.tenant_id == tenant_id)
            .first()
        )
        if settings and not store_name:
            store_cfg = dict(settings.store_settings or {})
            store_name = clean_store_name(store_cfg.get("store_name") or "")
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CHECKOUT_ROUTE] capabilities load skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )

    try:
        from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: PLC0415
            resolve_merchant_sales_channels,
        )

        sales = resolve_merchant_sales_channels(db, int(tenant_id or 0))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[CHECKOUT_ROUTE] sales channels resolve skipped tenant=%s err=%s",
            tenant_id,
            exc,
        )
        sales = None

    if sales is not None:
        return CheckoutChannelCapabilities(
            whatsapp_fast=sales.whatsapp_quick_order.available,
            store_link=sales.online_store.available,
            showroom_visit=sales.showroom_visit.available,
            store_url=sales.store_url,
            store_name=store_name,
        )

    return CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=False,
        showroom_visit=False,
        store_url="",
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


def resolve_available_purchase_channel_facts(
    *,
    store_url: str = "",
    maps_url: str = "",
    whatsapp_available: bool = True,
    store_url_source: str = "",
    merchant_sales_channels: Any = None,
) -> List[str]:
    """Structured channel ids for navigator/compose — mirrors tenant capabilities."""
    if merchant_sales_channels is not None:
        return list(merchant_sales_channels.available_purchase_channel_ids())

    out: List[str] = []
    try:
        from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: PLC0415
            store_url_evidence_activates_channel,
        )

        if store_url_evidence_activates_channel(
            source=store_url_source,
            found=bool(str(store_url or "").strip()),
        ):
            out.append("online_store")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — evidence gate must not block legacy path
        if str(store_url or "").strip():
            out.append("online_store")
    if whatsapp_available:
        out.append("whatsapp_quick_order")
    if str(maps_url or "").strip():
        out.append("showroom_visit")
    if not out:
        out.append("whatsapp_quick_order")
    return out


def _order_prep_mapping(order_prep: Any) -> Dict[str, Any]:
    if isinstance(order_prep, dict):
        return dict(order_prep)
    to_dict = getattr(order_prep, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
        return dict(raw) if isinstance(raw, dict) else {}
    return {}


def purchase_channel_committed(order_prep: Any) -> bool:
    prep = _order_prep_mapping(order_prep)
    channel = _checkout_channel(prep)
    if channel in {
        CHECKOUT_CHANNEL_WHATSAPP,
        CHECKOUT_CHANNEL_STORE,
        CHECKOUT_CHANNEL_SHOWROOM,
        "whatsapp_quick_order",
        "whatsapp_fast",
        "online_store",
        "showroom_visit",
    }:
        return True
    return bool(prep.get("catalog_line_items_authoritative"))


def should_route_bare_start_to_channel_selection(
    *,
    order_prep: Any = None,
    store_url: str = "",
    maps_url: str = "",
    whatsapp_available: bool = True,
    store_url_source: str = "",
    merchant_sales_channels: Any = None,
) -> bool:
    """True when bare buy-intent must choose a channel before product/checkout."""
    if purchase_channel_committed(order_prep):
        return False
    channels = resolve_available_purchase_channel_facts(
        store_url=store_url,
        maps_url=maps_url,
        whatsapp_available=whatsapp_available,
        store_url_source=store_url_source,
        merchant_sales_channels=merchant_sales_channels,
    )
    return len(channels) >= 2


def should_block_bare_start_product_prompt(
    *,
    order_prep: Any = None,
    store_url: str = "",
    maps_url: str = "",
    store_url_source: str = "",
    merchant_sales_channels: Any = None,
) -> bool:
    """Block deterministic product prompts until purchase channel is chosen."""
    prep = _order_prep_mapping(order_prep)
    if purchase_channel_committed(order_prep):
        return False
    if _awaiting_channel(prep):
        return True
    return should_route_bare_start_to_channel_selection(
        order_prep=order_prep,
        store_url=store_url,
        maps_url=maps_url,
        store_url_source=store_url_source,
        merchant_sales_channels=merchant_sales_channels,
    )


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


def _should_defer_to_brain_while_awaiting_channel(message: str) -> bool:
    """Product/inquiry turns should reach the brain, not repeat channel buttons."""
    raw = (message or "").strip()
    if not raw or is_catalog_visibility_question(raw):
        return False
    if has_checkout_entry_intent(raw):
        return False
    if _CHANNEL_INQUIRY_RE.search(_norm(raw)):
        return False
    try:
        from modules.ai.brain.intent.rules import (  # noqa: PLC0415
            INTENT_GREETING,
            INTENT_SOCIAL,
            INTENT_START_ORDER,
            match as match_intent,
        )

        intent = match_intent(raw)
        intent_name = str(getattr(intent, "name", intent) or "").strip()
        if intent_name and intent_name not in {INTENT_START_ORDER, INTENT_GREETING, INTENT_SOCIAL}:
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — intent probe must not block route owner
        pass
    return False


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


_CHANNEL_LABELS: Dict[str, str] = {
    CHECKOUT_CHANNEL_WHATSAPP: "طلب سريع عبر واتساب",
    CHECKOUT_CHANNEL_STORE: "الطلب من المتجر الإلكتروني",
    CHECKOUT_CHANNEL_SHOWROOM: "زيارة المعرض",
}

_CHANNEL_BUTTONS: Dict[str, Dict[str, Any]] = {
    CHECKOUT_CHANNEL_WHATSAPP: {
        "type": "reply",
        "reply": {"id": "checkout_whatsapp_fast", "title": "طلب سريع واتساب"},
    },
    CHECKOUT_CHANNEL_STORE: {
        "type": "reply",
        "reply": {"id": "checkout_store_link", "title": "المتجر الإلكتروني"},
    },
    CHECKOUT_CHANNEL_SHOWROOM: {
        "type": "reply",
        "reply": {"id": "checkout_showroom_visit", "title": "زيارة المعرض"},
    },
}

_EXPLICIT_CHANNEL_IDS: Dict[str, str] = {
    str(spec["reply"]["id"]): channel
    for channel, spec in _CHANNEL_BUTTONS.items()
}


def _explicit_channel_title_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for channel, spec in _CHANNEL_BUTTONS.items():
        title = _norm(str(spec["reply"]["title"] or ""))
        if title:
            out[title] = channel
    return out


def resolve_explicit_purchase_channel_payload(
    message: str,
    *,
    caps: CheckoutChannelCapabilities,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Map platform chrome (button id / exact title / numbered index) only.

    Free-text paraphrases are unstructured NL and must return None so Brain
    owns semantics. Do not treat customer prose as a second semantic engine.
    """
    meta = inbound_metadata if isinstance(inbound_metadata, dict) else {}
    for key in ("button_id", "button_provenance", "list_reply_id"):
        bid = str(meta.get(key) or "").strip()
        if bid in _EXPLICIT_CHANNEL_IDS:
            return _EXPLICIT_CHANNEL_IDS[bid]

    raw = (message or "").strip()
    if not raw:
        return None
    if raw in _EXPLICIT_CHANNEL_IDS:
        return _EXPLICIT_CHANNEL_IDS[raw]

    title_map = _explicit_channel_title_map()
    titled = title_map.get(_norm(raw))
    if titled:
        return titled

    if re.fullmatch(r"[123]", raw):
        channels = available_channels(caps)
        idx = int(raw) - 1
        if 0 <= idx < len(channels):
            return channels[idx]
    return None

_PURCHASE_CHANNEL_FACT_MAP: Dict[str, str] = {
    CHECKOUT_CHANNEL_WHATSAPP: "whatsapp_quick_order",
    CHECKOUT_CHANNEL_STORE: "online_store",
    CHECKOUT_CHANNEL_SHOWROOM: "showroom_visit",
}


def build_purchase_channel_selection_facts(
    caps: CheckoutChannelCapabilities,
) -> Dict[str, Any]:
    """Structured purchase-channel facts for compose — not customer reply text."""
    channels = available_channels(caps) or [CHECKOUT_CHANNEL_WHATSAPP]
    return {
        "available_purchase_channels": [
            _PURCHASE_CHANNEL_FACT_MAP[ch] for ch in channels
        ],
    }


_CHANNEL_CHOICE_INTRO = "كيف تحب تكمل؟"

_MSG_SHOWROOM_VISIT_UNAVAILABLE = (
    "زيارة المعرض غير مهيأة حالياً. تقدر تكمل الطلب من واتساب."
)


def _branch_showroom_routing_available(db: Any, tenant_id: int) -> bool:
    try:
        from modules.operations.branch_contact_evidence import (  # noqa: PLC0415
            structured_branch_contacts_enabled,
            tenant_has_structured_branch_data,
        )

        return bool(
            structured_branch_contacts_enabled()
            and tenant_has_structured_branch_data(db, int(tenant_id or 0))
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional branch probe
        return False


def _parse_channel_switch_choice(
    raw: str,
    *,
    caps: CheckoutChannelCapabilities,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Match only explicit structured purchase-channel chrome after commit."""
    return resolve_explicit_purchase_channel_payload(
        raw,
        caps=caps,
        inbound_metadata=inbound_metadata,
    )


def _channel_switch_target(
    raw: str,
    *,
    current_channel: str,
    caps: CheckoutChannelCapabilities,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    picked = _parse_channel_switch_choice(
        raw,
        caps=caps,
        inbound_metadata=inbound_metadata,
    )
    if not picked or picked == current_channel:
        return None
    return picked


def _resolve_channel_switch_decision(
    db: Any,
    *,
    tenant_id: int,
    customer_phone: str,
    raw: str,
    caps: CheckoutChannelCapabilities,
    current_channel: str,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[CheckoutRouteDecision]:
    """Re-route when customer picks another purchase channel after commit."""
    target = _channel_switch_target(
        raw,
        current_channel=current_channel,
        caps=caps,
        inbound_metadata=inbound_metadata,
    )
    if not target:
        return None

    if target == CHECKOUT_CHANNEL_STORE:
        persist_checkout_route_state(
            db,
            tenant_id=tenant_id,
            phone=customer_phone,
            checkout_channel=target,
            awaiting_checkout_channel=False,
        )
        brain_state: Dict[str, Any] = {}
        try:
            _, op = load_checkout_route_context(
                db,
                tenant_id=int(tenant_id or 0),
                customer_phone=customer_phone or "",
            )
            raw_bs = op.get("_brain_state")
            if isinstance(raw_bs, dict):
                brain_state = raw_bs
        except Exception:  # noqa: BLE001  # noqa: silent-ok — focus load must not block store delivery
            brain_state = {}
        return _storefront_delivery_decision(
            db,
            tenant_id=int(tenant_id or 0),
            caps=caps,
            brain_state=brain_state,
        )

    if target == CHECKOUT_CHANNEL_SHOWROOM:
        persist_checkout_route_state(
            db,
            tenant_id=tenant_id,
            phone=customer_phone,
            checkout_channel=target,
            awaiting_checkout_channel=False,
        )
        if not caps.showroom_visit or not _branch_showroom_routing_available(
            db, int(tenant_id or 0),
        ):
            return CheckoutRouteDecision(
                reply_text=_MSG_SHOWROOM_VISIT_UNAVAILABLE,
                reason="showroom_visit_unavailable",
                checkout_channel=target,
                clear_awaiting_channel=True,
            )
        return None

    return None


def build_channel_choice_prompt(
    caps: CheckoutChannelCapabilities,
    *,
    include_numbered_options: bool = True,
) -> str:
    """Channel-choice body text based on tenant capabilities.

    When interactive buttons carry the channel labels, keep the body to a
    short question only — numbered options belong in text fallback paths.
    """
    if not include_numbered_options:
        return _CHANNEL_CHOICE_INTRO
    channels = available_channels(caps) or [CHECKOUT_CHANNEL_WHATSAPP]
    lines: List[str] = [_CHANNEL_CHOICE_INTRO]
    for idx, channel in enumerate(channels, start=1):
        lines.append(f"{idx}- {_CHANNEL_LABELS[channel]}")
    return "\n".join(lines)


def compose_purchase_channel_selection_goal(*, buttons_will_render: bool) -> str:
    """Compose hint for LLM channel-selection turns — not customer reply text."""
    parts = [
        "help_customer_choose_purchase_channel — short natural Saudi Arabic "
        "WhatsApp reply inviting the customer to pick how they want to "
        "continue purchase.",
        "Do NOT ask product, quantity, address, or payment yet.",
    ]
    if buttons_will_render:
        parts.append(
            "buttons_will_render=true | do_not_repeat_button_labels_in_body=true — "
            "keep the body to one brief question; interactive buttons carry "
            "channel names; no numbered list in the message body."
        )
    else:
        parts.append(
            "Present the available purchase channels clearly in natural "
            "prose or a numbered list."
        )
    return " | ".join(parts)


def build_channel_choice_buttons(caps: CheckoutChannelCapabilities) -> Tuple[Dict[str, Any], ...]:
    channels = available_channels(caps) or [CHECKOUT_CHANNEL_WHATSAPP]
    return tuple(_CHANNEL_BUTTONS[ch] for ch in channels[:3])


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


def _storefront_delivery_decision(
    db: Any,
    *,
    tenant_id: int,
    caps: CheckoutChannelCapabilities,
    brain_state: Optional[Dict[str, Any]] = None,
    store_url_source: str = "",
) -> CheckoutRouteDecision:
    """Deliver storefront completion link — product_url first, fail closed."""
    from modules.ai.brain.commerce.storefront_product_url import (  # noqa: PLC0415
        resolve_storefront_completion_link,
        truncate_wa_cta_label,
    )

    resolution = resolve_storefront_completion_link(
        db,
        tenant_id=int(tenant_id or 0),
        brain_state=brain_state,
        store_url=str(caps.store_url or ""),
        store_url_source=store_url_source or "caps.store_url",
    )
    cta_label = truncate_wa_cta_label(resolution.cta_label)
    if resolution.found and resolution.url:
        body = T_faq_store_info(caps.store_name, resolution.url)
        try:
            from core.wa_link_buttons import extract_first_cta_url  # noqa: PLC0415

            extracted = extract_first_cta_url(body)
            if extracted is not None:
                body = str(extracted.cleaned_text or "").strip() or body
        except Exception:  # noqa: BLE001  # noqa: silent-ok — CTA body strip must not block store delivery
            body = body.replace(resolution.url, "").strip() or body
        return CheckoutRouteDecision(
            reply_text=body,
            reason="store_link_delivered",
            checkout_channel=CHECKOUT_CHANNEL_STORE,
            clear_awaiting_channel=True,
            cta_label=cta_label,
            cta_url=resolution.url,
        )
    return CheckoutRouteDecision(
        reply_text=build_store_link_reply(
            CheckoutChannelCapabilities(
                whatsapp_fast=caps.whatsapp_fast,
                store_link=caps.store_link,
                showroom_visit=caps.showroom_visit,
                store_url="",
                store_name=caps.store_name,
            )
        ),
        reason=(
            "store_link_product_url_unavailable"
            if resolution.has_product_focus
            else "store_link_unavailable"
        ),
        checkout_channel=CHECKOUT_CHANNEL_STORE,
        clear_awaiting_channel=True,
        cta_label="",
        cta_url="",
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
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Optional[CheckoutRouteDecision]:
    """Pre-brain checkout channel owner — explicit structured chrome only.

    Unstructured purchase intent and pending-choice free text return None so
    Brain owns semantics. Platform still executes explicit button IDs/titles.
    """
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

    _stage, order_prep = load_checkout_route_context(
        db,
        tenant_id=int(tenant_id or 0),
        customer_phone=customer_phone or "",
    )
    channel = _checkout_channel(order_prep)
    caps = load_channel_capabilities(db, int(tenant_id or 0))

    if channel == CHECKOUT_CHANNEL_SHOWROOM:
        return None

    if _awaiting_channel(order_prep):
        picked = resolve_explicit_purchase_channel_payload(
            raw,
            caps=caps,
            inbound_metadata=inbound_metadata,
        )
        if not picked:
            return None
        if picked == CHECKOUT_CHANNEL_STORE:
            persist_checkout_route_state(
                db,
                tenant_id=tenant_id,
                phone=customer_phone,
                checkout_channel=picked,
                awaiting_checkout_channel=False,
            )
            brain_state: Dict[str, Any] = {}
            raw_bs = order_prep.get("_brain_state")
            if isinstance(raw_bs, dict):
                brain_state = raw_bs
            return _storefront_delivery_decision(
                db,
                tenant_id=int(tenant_id or 0),
                caps=caps,
                brain_state=brain_state,
            )
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
            if not caps.showroom_visit or not _branch_showroom_routing_available(
                db, int(tenant_id or 0),
            ):
                return CheckoutRouteDecision(
                    reply_text=_MSG_SHOWROOM_VISIT_UNAVAILABLE,
                    reason="showroom_visit_unavailable",
                    checkout_channel=picked,
                    clear_awaiting_channel=True,
                )
            return None
        return None

    if channel == CHECKOUT_CHANNEL_STORE:
        switch = _resolve_channel_switch_decision(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            raw=raw,
            caps=caps,
            current_channel=channel,
            inbound_metadata=inbound_metadata,
        )
        if switch is not None:
            return switch
        return None

    if channel == CHECKOUT_CHANNEL_WHATSAPP:
        switch = _resolve_channel_switch_decision(
            db,
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            raw=raw,
            caps=caps,
            current_channel=channel,
            inbound_metadata=inbound_metadata,
        )
        if switch is not None:
            return switch
        return None

    return None


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
    "build_purchase_channel_selection_facts",
    "compose_purchase_channel_selection_goal",
    "purchase_channel_committed",
    "resolve_available_purchase_channel_facts",
    "resolve_explicit_purchase_channel_payload",
    "should_block_bare_start_product_prompt",
    "should_route_bare_start_to_channel_selection",
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

