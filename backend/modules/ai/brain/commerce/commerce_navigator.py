"""
commerce_navigator.py
─────────────────────
Deterministic commerce path contract — emits facts, goals, and forbidden
actions for the LLM. Never writes customer-facing reply text.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence

logger = logging.getLogger("nahla.brain.commerce_navigator")

CommerceStage = Literal[
    "browse",
    "browse_with_purchase_intent",
    "price_objection",
    "purchase_channel_selection",
    "whatsapp_quick_order",
    "online_store_redirect",
    "showroom_visit",
    "post_purchase_tracking",
    "support",
]

PurchaseChannel = Literal["online_store", "whatsapp_quick_order", "showroom_visit"]

_ALL_CHANNELS: tuple[PurchaseChannel, ...] = (
    "online_store",
    "whatsapp_quick_order",
    "showroom_visit",
)

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_WHATSAPP_CHANNEL_RE = re.compile(
    r"(?:"
    r"(?:\u0639\u0646\s*\u0637\u0631\u064a\u0642|\u0645\u0646\s*\u062e\u0644\u0627\u0644|\bvia)\s*(?:\u0648\u0627\u062a\u0633(?:\u0627\u0628|\u0628)?|whatsapp|\u0647\u0646\u0627)\b"
    r"|\b(?:\u0648\u0627\u062a\u0633(?:\u0627\u0628|\u0628)?|whatsapp)\b"
    r"|\u0637\u0644\u0628\s*\u0633\u0631\u064a\u0639"
    r"|(?:^|\s)(?:\u062c\u0647\u0632|\u0627\u062c\u0647\u0632|\u0623\u062c\u0647\u0632)\s+(?:\u0644\u064a\s+)?\d{1,4}"
    r"|(?:^|\s)(?:\u062c\u0647\u0632|\u0627\u062c\u0647\u0632|\u0623\u062c\u0647\u0632|\u062e\u0630)\s+\u0644\u064a(?:\s+\d{1,4})?"
    r"|(?:^|\s)(?<![\u0627\u0623\u0625\u0622\u0627])\u062e\u0630\s*(?:\u0637\u0644\u0628(?:\u064a)?|(?:\u0644\u064a\s+)?\d{1,4})"
    r"|(?:\u0623?\u0631\u0633\u0644|\u0627\u0631\u0633\u0644)(?:\u064a|\u0644\u064a)?\s*(?:\u0644\u064a\s*)?(?:\u0627\u0644)?(?:\u062d\u0633\u0627\u0628|\u0641\u0627\u062a\u0648\u0631\u0629|\u0625?\u064a\u0628\u0627\u0646|\u0627\u064a\u0628\u0627\u0646)"
    r"|(?:\u0623?\u0628\u064a|\u0627\u0628\u064a|\u0623?\u0643\u0645\u0644|\u0627\u0643\u0645\u0644)\s*(?:\u0647\u0646\u0627|\u0627\u0644\u0637\u0644\u0628\s*\u0647\u0646\u0627)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ONLINE_STORE_CHANNEL_RE = re.compile(
    r"(?:"
    r"\u0645\u062a\u062c(?:\u0631|\u064a)?(?:\s*\u0627\u0644)?(?:\u0625?\u0644\u0643\u062a\u0631\u0648\u0646\u064a|\u0627\u0644\u0643\u062a\u0631\u0648\u0646\u064a|\u0627\u0644\u0627\u0644\u0643\u062a\u0631\u0648\u0646\u064a)"
    r"|\bonline\b|\bwebsite\b|store\s*link"
    r"|\u0645\u0646\s*(?:\u0627\u0644\u0645\u0648\u0642\u0639|\u0627\u0644\u0645\u062a\u062c\u0631|\u0627\u0644\u0631\u0627\u0628\u0637)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SHOWROOM_CHANNEL_RE = re.compile(
    r"(?:"
    r"\u0645\u0639\u0631\u0636|\u0627\u0644\u0641\u0631\u0639|\u0627\u0644\u0645\u062d\u0644|\u0627\u0644\u0645\u062a\u062c\u0631"
    r"|\u0632(?:\u064a|\u0649)\u0627\u0631\u0629|\u0623?\u0632\u0648\u0631|\u0627\u0632\u0648\u0631"
    r"|pickup|\u0627\u0633\u062a\u0644\u0627\u0645\s*\u0645\u0646\s*\u0627\u0644(?:\u0645\u062d\u0644|\u0645\u0639\u0631\u0636)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BROWSE_SIGNAL_RE = re.compile(
    r"(?:"
    r"\u0648\u0634\s*\u0639\u0646\u062f(?:\u0643\u0645|\u0643)?"
    r"|\u0639\u0646\u062f(?:\u0643\u0645|\u0643)?\s*(?:\u0625?\u064a\u0634|\u0627\u064a\u0634|\u0648\u0634|\u0627\u064a\u0647|\u0627\u064a\u0647)"
    r"|\u0623?\u0634\u0648\u0641|\u0627\u0634\u0648\u0641|\u0623?\u062a\u0641\u0631\u062c|\u0627\u062a\u0641\u0631\u062c|\u0645\u062a\u0648\u0641\u0631"
    r"|\u0623?\u0646\u0648\u0627\u0639|\u0627\u0646\u0648\u0627\u0639|\u0627\u0644\u062e\u064a\u0627\u0631\u0627\u062a"
    r"|\u0648\u0634\s*(?:\u0627\u0644\u0645\u0646\u062a\u062c\u0627\u062a|\u0627\u0644\u0645\u0646\u062a\u062c\u0627\u062a|\u0627\u0644\u0645\u062a\u0648\u0641\u0631)"
    r"|\u0627\u0639\u0631\u0636\s*(?:\u0627\u0644\u0645\u0646\u062a\u062c\u0627\u062a|\u0644\u064a|\u0644\u0646\u0627)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SUPPORT_INTENTS = frozenset({
    "talk_to_human",
    "complaint_refund",
    "product_feedback",
})

_TRACKING_INTENTS = frozenset({"track_order", "ask_shipping"})


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


def _order_prep_dict(order_prep: Any) -> Dict[str, Any]:
    if order_prep is None:
        return {}
    if isinstance(order_prep, dict):
        return dict(order_prep)
    if hasattr(order_prep, "to_dict"):
        try:
            return dict(order_prep.to_dict())
        except Exception:  # noqa: BLE001  # noqa: silent-ok — order_prep.to_dict() is best-effort
            pass
    return {}


def _known_fields_from_prep(prep: Dict[str, Any]) -> Dict[str, Any]:
    known: Dict[str, Any] = {}
    first = str(prep.get("customer_first_name") or "").strip()
    last = str(prep.get("customer_last_name") or "").strip()
    if first or last:
        known["name"] = " ".join(x for x in (first, last) if x).strip()
    city = str(prep.get("city") or "").strip()
    if city:
        known["city"] = city
    product = str(
        prep.get("product_name") or prep.get("product_id") or ""
    ).strip()
    if product:
        known["product"] = product
    qty = prep.get("quantity")
    if qty:
        try:
            if int(qty) > 0:
                known["quantity"] = int(qty)
        except (TypeError, ValueError):
            pass
    return known


def _whatsapp_checkout_committed(prep: Dict[str, Any]) -> bool:
    channel = str(prep.get("checkout_channel") or "").strip().lower()
    return channel in {"whatsapp_fast", "whatsapp_quick_order"}


def _catalog_order_authoritative(
    prep: Dict[str, Any],
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    if prep.get("catalog_line_items_authoritative"):
        return True
    line_items = prep.get("line_items") or []
    if isinstance(line_items, list) and line_items:
        if prep.get("catalog_checkout_total") is not None:
            return True
    meta = dict(inbound_metadata or {})
    if str(meta.get("source_type") or "").strip().lower() == "catalog_order":
        items = meta.get("product_items") or []
        if isinstance(items, list) and items:
            return True
    return False


def _enrich_catalog_order_known_fields(
    prep: Dict[str, Any],
    known: Dict[str, Any],
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not _catalog_order_authoritative(prep, inbound_metadata):
        return known
    enriched = dict(known)
    enriched["product"] = "known"
    enriched["quantity"] = "known"
    enriched["total"] = "known"
    total = prep.get("catalog_checkout_total") or prep.get("order_total")
    if total is not None:
        enriched["order_total"] = total
    return enriched


def _resolve_whatsapp_missing_fields(
    *,
    order_prep: Any,
    state: Any = None,
    whatsapp_phone: str = "",
    order_context: Any = None,
) -> List[str]:
    if order_context is not None:
        try:
            from core.order_context_prefill import (  # noqa: PLC0415
                resolve_checkout_missing_fields_legacy,
            )

            missing = resolve_checkout_missing_fields_legacy(order_context)
            prep = _order_prep_dict(order_prep)
            if _catalog_order_authoritative(prep):
                missing = [
                    m for m in missing
                    if m not in {"product", "quantity", "variant"}
                ]
            return missing
        except Exception:  # noqa: BLE001  # noqa: silent-ok — order-context missing must not block legacy path
            pass

    prep = _order_prep_dict(order_prep)
    missing: List[str] = []
    try:
        from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

        line_items = list(prep.get("line_items") or [])
        brain_state: Dict[str, Any] = {}
        focus = getattr(state, "current_product_focus", None) if state else None
        if isinstance(focus, dict) and focus.get("title"):
            brain_state["current_product_focus"] = focus
        missing = list(
            compute_wa_missing_fields(
                prep,
                brain_state=brain_state,
                whatsapp_phone=whatsapp_phone or None,
                line_items=line_items,
            )
        )
    except Exception:  # noqa: BLE001
        missing = list(prep.get("missing_fields") or [])

    if _catalog_order_authoritative(prep):
        missing = [
            m for m in missing
            if m not in {"product", "quantity", "variant"}
        ]

    has_product = "product" not in missing
    qty = prep.get("quantity")
    qty_ok = False
    try:
        qty_ok = int(qty or 0) > 0
    except (TypeError, ValueError):
        qty_ok = False
    if not qty_ok and any(
        int((li or {}).get("quantity") or 0) > 0
        for li in (prep.get("line_items") or [])
        if isinstance(li, dict)
    ):
        qty_ok = True
    if has_product and not qty_ok and "quantity" not in missing:
        missing.insert(0, "quantity")

    payment_ready = (
        has_product
        and qty_ok
        and "customer_first_name" not in missing
        and "customer_last_name" not in missing
        and "city" not in missing
        and "delivery_address" not in missing
    )
    if not payment_ready and "payment_method" not in missing:
        missing = [m for m in missing if m != "payment_method"]
    elif payment_ready and "payment_method" not in missing:
        missing.append("payment_method")
    return missing


def _forbidden_for_channel_selection() -> List[str]:
    return [
        "do_not_ask_product_yet",
        "do_not_ask_quantity_yet",
        "do_not_ask_city_yet",
        "do_not_ask_address_yet",
        "do_not_ask_payment",
        "do_not_ask_address",
        "do_not_create_order_yet",
        "do_not_append_quantity_prompt",
    ]


def _forbidden_for_price_objection() -> List[str]:
    return [
        "do_not_append_quantity_prompt",
        "do_not_create_order_yet",
        "do_not_ask_payment",
        "do_not_ask_address",
        "do_not_offer_unapproved_discount",
    ]


def _forbidden_for_browse_with_purchase_intent() -> List[str]:
    return [
        "do_not_create_order_yet",
        "do_not_ask_payment",
        "do_not_ask_address_until_product_selected",
        "do_not_append_quantity_prompt",
    ]


def _forbidden_for_browse() -> List[str]:
    return [
        "do_not_create_order_yet",
        "do_not_ask_payment",
        "do_not_ask_address",
    ]


def _forbidden_for_whatsapp(*, missing_fields: Sequence[str]) -> List[str]:
    forbidden = ["do_not_create_order_yet"]
    if missing_fields:
        forbidden.append("do_not_append_quantity_prompt")
    payment_blockers = {
        "product",
        "quantity",
        "customer_first_name",
        "customer_last_name",
        "city",
        "delivery_address",
    }
    if any(m in payment_blockers for m in missing_fields):
        forbidden.append("do_not_ask_payment")
    if "delivery_address" in missing_fields or "city" in missing_fields:
        forbidden.append("do_not_ask_address_before_channel")
    return forbidden


def _is_catalog_order(inbound_metadata: Optional[Dict[str, Any]]) -> bool:
    meta = dict(inbound_metadata or {})
    return str(meta.get("source_type") or "").strip().lower() == "catalog_order"


def _is_price_objection(
    message: str,
    *,
    intent_name: str = "",
    decision_topic: str = "",
    intent_slots: Optional[Dict[str, Any]] = None,
) -> bool:
    slots = dict(intent_slots or {})
    if slots.get("price_objection") or decision_topic == "price_objection":
        return True
    try:
        from ..state.price_objection_topic import detect_price_objection_topic_shift  # noqa: PLC0415

        return detect_price_objection_topic_shift(message)
    except Exception:  # noqa: BLE001
        return intent_name == "ask_price" and bool(slots.get("block_quantity_prompt"))


def _selected_channel(message: str) -> Optional[PurchaseChannel]:
    norm = _norm(message)
    if not norm:
        return None
    padded = f" {norm} "
    if _WHATSAPP_CHANNEL_RE.search(padded):
        return "whatsapp_quick_order"
    if _ONLINE_STORE_CHANNEL_RE.search(norm):
        return "online_store"
    if _SHOWROOM_CHANNEL_RE.search(norm):
        return "showroom_visit"
    return None


def _is_active_whatsapp_checkout(*, stage: str = "", order_prep: Any = None) -> bool:
    try:
        from .prebrain_order_flow_arbiter import is_active_order_flow  # noqa: PLC0415

        return is_active_order_flow(stage=stage, order_prep=order_prep)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class CommerceNavigatorDecision:
    stage: CommerceStage
    confidence: float
    reason: str
    next_goal: str
    available_purchase_channels: List[PurchaseChannel] = field(default_factory=list)
    known_fields: Dict[str, Any] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    forbidden_actions: List[str] = field(default_factory=list)
    customer_intent: str = ""
    style: str = "natural_saudi_brief"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "stage": self.stage,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
            "next_goal": self.next_goal,
            "known_fields": dict(self.known_fields),
            "missing_fields": list(self.missing_fields),
            "forbidden_actions": list(self.forbidden_actions),
            "style": self.style,
        }
        if self.available_purchase_channels:
            out["available_purchase_channels"] = list(self.available_purchase_channels)
        if self.customer_intent:
            out["customer_intent"] = self.customer_intent
        return out


def _enrich_navigator_known_fields(
    known: Dict[str, Any],
    *,
    merchant_sales_channels: Any = None,
    store_url: str = "",
    maps_url: str = "",
) -> Dict[str, Any]:
    enriched = dict(known)
    if merchant_sales_channels is not None:
        enriched["sales_channel_availability"] = (
            merchant_sales_channels.availability_facts()
        )
        if merchant_sales_channels.store_url:
            enriched["store_url"] = merchant_sales_channels.store_url
        if merchant_sales_channels.maps_url:
            enriched["maps_url"] = merchant_sales_channels.maps_url
    else:
        if store_url:
            enriched.setdefault("store_url", store_url)
        if maps_url:
            enriched.setdefault("maps_url", maps_url)
    return enriched


def resolve_commerce_navigator(
    *,
    message: str = "",
    intent_name: str = "",
    intent_slots: Optional[Dict[str, Any]] = None,
    decision_topic: str = "",
    stage: str = "",
    order_prep: Any = None,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    store_url: str = "",
    maps_url: str = "",
    whatsapp_phone: str = "",
    merchant_sales_channels: Any = None,
    order_context: Any = None,
) -> CommerceNavigatorDecision:
    """Pure contract resolver — never returns customer reply text."""
    msg = (message or "").strip()
    slots = dict(intent_slots or {})
    prep = _order_prep_dict(order_prep)
    known = _known_fields_from_prep(prep)
    channels: List[PurchaseChannel] = []
    if merchant_sales_channels is not None:
        channels = list(merchant_sales_channels.available_purchase_channel_ids())  # type: ignore[assignment]
    else:
        for ch in _ALL_CHANNELS:
            if ch == "online_store" and not str(store_url or "").strip():
                continue
            if ch == "showroom_visit" and not str(maps_url or "").strip():
                continue
            channels.append(ch)
    if not channels:
        channels = ["whatsapp_quick_order"]
    known = _enrich_navigator_known_fields(
        known,
        merchant_sales_channels=merchant_sales_channels,
        store_url=store_url,
        maps_url=maps_url,
    )
    meta = dict(inbound_metadata or {})
    catalog_order = _is_catalog_order(inbound_metadata)
    if catalog_order or _catalog_order_authoritative(prep, meta):
        known = _enrich_catalog_order_known_fields(prep, known, inbound_metadata=meta)

    try:
        from core.catalog_authoritative_line_items import (  # noqa: PLC0415
            is_online_store_existing_order_message,
            is_shipping_address_capture_context,
        )

        if is_shipping_address_capture_context(
            msg,
            order_prep=order_prep,
            stage=stage,
        ):
            missing = _resolve_whatsapp_missing_fields(
                order_prep=order_prep,
                state=state,
                whatsapp_phone=whatsapp_phone,
                order_context=order_context,
            )
            addr_missing = [
                m for m in missing
                if m in {
                    "city",
                    "delivery_address",
                    "address",
                    "address_line",
                    "short_address_code",
                    "google_maps_url",
                }
            ] or missing
            return CommerceNavigatorDecision(
                stage="whatsapp_quick_order",
                confidence=0.92,
                reason="customer is providing or correcting delivery address",
                next_goal="collect_or_confirm_delivery_address",
                known_fields=known,
                missing_fields=addr_missing,
                forbidden_actions=[
                    "do_not_browse",
                    "do_not_capture_product_from_message",
                    "do_not_append_quantity_prompt",
                    "do_not_push_product_list",
                ],
                customer_intent="address_correction",
            )

        if is_online_store_existing_order_message(msg):
            return CommerceNavigatorDecision(
                stage="post_purchase_tracking",
                confidence=0.88,
                reason="customer referenced an existing online-store order",
                next_goal="link_or_verify_existing_online_store_order",
                known_fields=known,
                forbidden_actions=[
                    "do_not_create_whatsapp_line_items_from_text",
                    "do_not_browse",
                    "do_not_capture_product_from_message",
                ],
                customer_intent="existing_online_store_order",
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — address/online-store guards must not block navigator
        pass

    if _is_price_objection(
        msg,
        intent_name=intent_name,
        decision_topic=decision_topic,
        intent_slots=slots,
    ):
        return CommerceNavigatorDecision(
            stage="price_objection",
            confidence=0.94,
            reason="customer expressed price or competitor comparison objection",
            next_goal="address_price_objection_without_checkout_push",
            known_fields=known,
            forbidden_actions=_forbidden_for_price_objection(),
            customer_intent="price_objection",
        )

    if intent_name in _SUPPORT_INTENTS:
        return CommerceNavigatorDecision(
            stage="support",
            confidence=0.92,
            reason=f"support intent={intent_name}",
            next_goal="handle_support_request",
            known_fields=known,
            forbidden_actions=["do_not_push_checkout"],
        )

    if intent_name in _TRACKING_INTENTS or decision_topic in {"track_order", "tracking_link_follow_up"}:
        return CommerceNavigatorDecision(
            stage="post_purchase_tracking",
            confidence=0.91,
            reason="existing-order tracking or shipping status inquiry",
            next_goal="provide_order_tracking_status",
            known_fields=known,
            forbidden_actions=["do_not_restart_checkout", "do_not_ask_address"],
        )

    channel = _selected_channel(msg)
    genuine_purchase_entry = False
    try:
        from .checkout_route_owner import (  # noqa: PLC0415
            is_genuine_purchase_channel_entry,
        )

        genuine_purchase_entry = is_genuine_purchase_channel_entry(
            message=msg,
            order_prep=order_prep,
            state=state,
            inbound_metadata=inbound_metadata,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — genuine-entry probe must not block navigator
        genuine_purchase_entry = False
    if (
        channel is None
        and genuine_purchase_entry
        and len(channels) == 1
    ):
        channel = channels[0]
    whatsapp_committed = _whatsapp_checkout_committed(prep)
    browse_in_checkout = bool(_BROWSE_SIGNAL_RE.search(_norm(msg)))
    _address_turn = False
    try:
        from core.catalog_authoritative_line_items import is_shipping_address_capture_context  # noqa: PLC0415

        _address_turn = is_shipping_address_capture_context(
            msg,
            order_prep=order_prep,
            stage=stage,
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — address context probe must not block browse routing
        _address_turn = False

    if browse_in_checkout and not _address_turn and (
        whatsapp_committed
        or channel == "whatsapp_quick_order"
        or _is_active_whatsapp_checkout(stage=stage, order_prep=order_prep)
    ):
        return CommerceNavigatorDecision(
            stage="browse_with_purchase_intent",
            confidence=0.9,
            reason="product browse inside committed whatsapp purchase path",
            next_goal="show_or_summarize_available_products_or_send_catalog",
            known_fields=known,
            forbidden_actions=_forbidden_for_browse_with_purchase_intent(),
            customer_intent="browse",
        )

    active_wa = (
        catalog_order
        or channel == "whatsapp_quick_order"
        or whatsapp_committed
        or (
            channel is None
            and _is_active_whatsapp_checkout(stage=stage, order_prep=order_prep)
        )
    )

    if channel == "online_store" or (
        intent_name == "online_store_inquiry" and not active_wa
    ):
        ch = [c for c in channels if c == "online_store"] or ["online_store"]
        return CommerceNavigatorDecision(
            stage="online_store_redirect",
            confidence=0.9,
            reason="customer chose or asked about online store channel",
            next_goal="guide_customer_to_online_store",
            available_purchase_channels=ch,  # type: ignore[arg-type]
            known_fields={**known, **({"store_url": store_url} if store_url else {})},
            forbidden_actions=[
                "do_not_force_whatsapp_checkout",
                "do_not_ask_payment_on_whatsapp",
            ],
            customer_intent="online_store",
        )

    if channel == "showroom_visit" or intent_name == "ask_location":
        ch = [c for c in channels if c == "showroom_visit"] or ["showroom_visit"]
        return CommerceNavigatorDecision(
            stage="showroom_visit",
            confidence=0.89,
            reason="customer chose or asked about showroom / branch visit",
            next_goal="guide_customer_to_showroom",
            available_purchase_channels=ch,  # type: ignore[arg-type]
            known_fields={**known, **({"maps_url": maps_url} if maps_url else {})},
            forbidden_actions=["do_not_force_whatsapp_checkout", "do_not_ask_payment"],
            customer_intent="showroom_visit",
        )

    if active_wa:
        missing = _resolve_whatsapp_missing_fields(
            order_prep=order_prep,
            state=state,
            whatsapp_phone=whatsapp_phone,
            order_context=order_context,
        )
        next_goal = "collect_next_whatsapp_order_field"
        if order_context is not None:
            try:
                from core.order_context_prefill import derive_checkout_next_goal  # noqa: PLC0415

                next_goal = derive_checkout_next_goal(
                    getattr(order_context, "missing_fields_result", None),
                    order_context.prefill,
                )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — derived goal must not block legacy navigator
                next_goal = "collect_next_whatsapp_order_field"
        elif not missing:
            next_goal = "confirm_whatsapp_order_before_payment"
        elif missing[0] == "product":
            next_goal = "collect_product_for_whatsapp_order"
        elif missing[0] == "quantity":
            next_goal = "collect_quantity_for_whatsapp_order"
        elif missing[0] in {"customer_first_name", "customer_last_name"}:
            next_goal = "collect_customer_name_for_whatsapp_order"
        elif missing[0] == "city":
            next_goal = "collect_city_for_whatsapp_order"
        elif missing[0] == "delivery_address":
            next_goal = "collect_delivery_address_for_whatsapp_order"
        elif missing[0] == "payment_method":
            next_goal = "collect_payment_method_for_whatsapp_order"
        return CommerceNavigatorDecision(
            stage="whatsapp_quick_order",
            confidence=0.93 if catalog_order else 0.88,
            reason=(
                "whatsapp catalog order event"
                if catalog_order
                else "explicit whatsapp quick-order channel or active WA checkout"
            ),
            next_goal=next_goal,
            known_fields=known,
            missing_fields=missing,
            forbidden_actions=_forbidden_for_whatsapp(missing_fields=missing),
            customer_intent="whatsapp_quick_order",
        )

    if (
        genuine_purchase_entry
        and not browse_in_checkout
        and len(channels) >= 2
    ):
        return CommerceNavigatorDecision(
            stage="purchase_channel_selection",
            confidence=0.91,
            reason="customer expressed purchase intent but purchase channel is not chosen",
            next_goal="help_customer_choose_purchase_channel",
            available_purchase_channels=channels,
            known_fields=known,
            forbidden_actions=_forbidden_for_channel_selection(),
            customer_intent="wants_to_buy",
        )

    return CommerceNavigatorDecision(
        stage="browse",
        confidence=0.75,
        reason="exploration or product inquiry without committed purchase channel",
        next_goal="help_customer_explore_products",
        known_fields=known,
        forbidden_actions=_forbidden_for_browse(),
        customer_intent="browse",
    )


def commerce_navigator_goal_directive(decision: CommerceNavigatorDecision) -> str:
    """Structured goal suffix for the LLM — not a customer reply."""
    parts = [
        f"commerce_navigator — stage={decision.stage}",
        f"next_goal={decision.next_goal}",
    ]
    if decision.customer_intent:
        parts.append(f"customer_intent={decision.customer_intent}")
    if decision.missing_fields:
        parts.append("missing=" + ",".join(decision.missing_fields))
    if decision.forbidden_actions:
        parts.append("forbidden=" + ",".join(decision.forbidden_actions))
    if decision.available_purchase_channels:
        parts.append(
            "channels=" + ",".join(decision.available_purchase_channels)
        )
    return " | ".join(parts)


__all__ = [
    "CommerceNavigatorDecision",
    "CommerceStage",
    "PurchaseChannel",
    "commerce_navigator_goal_directive",
    "resolve_commerce_navigator",
]
