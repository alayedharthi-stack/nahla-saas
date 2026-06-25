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

_GENERIC_BUY_INTENT_RE = re.compile(
    r"(?:"
    r"(?:\u0623?\u0628\u064a|\u0627\u0628\u064a|\u0623?\u0628\u063a(?:\u064a|\u0649)|\u0627\u0628\u063a(?:\u064a|\u0649)|"
    r"\u0648\u062f(?:\u064a|\u064a)?)\s*(?:\u0623?\u0637\u0644\u0628|\u0627\u0637\u0644\u0628|\u0623?\u0634\u062a\u0631\u064a|\u0627\u0634\u062a\u0631\u064a|\u0622?\u062e\u0630|\u0623?\u062e\u0630|\u0627\u062e\u0630|\u062e\u0630)"
    r"|(?:\u0643\u064a\u0641|\u0648\u0634)\s*(?:\u0623?\u0634\u062a\u0631\u064a|\u0627\u0634\u062a\u0631\u064a|\u0623?\u0637\u0644\u0628|\u0627\u0637\u0644\u0628|\u0637\u0631\u064a\u0642\u0629\s*\u0627\u0644(?:\u0637\u0644\u0628|\u0634\u0631\u0627\u0621))"
    r"|(?:\u0623?\u0628\u063a(?:\u064a|\u0649)|\u0627\u0628\u063a(?:\u064a|\u0649))\s*(?:\u0627\u0644)?(?:\u0645\u0646\u062a\u062c|\u0645\u0646\u062a\u062c\u0627\u062a|\u0647\u0630\u0627|\u0647\u0630\u0647|\u0647\u0627|\u0647\u0648)"
    r"|(?:\u0637\u0631\u064a\u0642\u0629|\u0643\u064a\u0641\u064a\u0629)\s*(?:\u0627\u0644)?(?:\u0637\u0644\u0628|\u0634\u0631\u0627\u0621|\u0627\u0644\u0637\u0644\u0628)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

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
    r"\u0639\u0646\u062f(?:\u0643\u0645|\u0643)?\s*(?:\u0625?\u064a\u0634|\u0627\u064a\u0634|\u0648\u0634|\u0627\u064a\u0647|\u0627\u064a\u0647)"
    r"|\u0623?\u0634\u0648\u0641|\u0627\u0634\u0648\u0641|\u0623?\u062a\u0641\u0631\u062c|\u0627\u062a\u0641\u0631\u062c|\u0645\u062a\u0648\u0641\u0631"
    r"|\u0623?\u0646\u0648\u0627\u0639|\u0627\u0646\u0648\u0627\u0639|\u0627\u0644\u062e\u064a\u0627\u0631\u0627\u062a"
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
        except Exception:  # noqa: BLE001
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


def _resolve_whatsapp_missing_fields(
    *,
    order_prep: Any,
    state: Any = None,
    whatsapp_phone: str = "",
) -> List[str]:
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


def _is_generic_buy_intent(message: str, *, intent_name: str = "") -> bool:
    if intent_name == "start_order":
        return True
    return bool(_GENERIC_BUY_INTENT_RE.search(_norm(message)))


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
) -> CommerceNavigatorDecision:
    """Pure contract resolver — never returns customer reply text."""
    msg = (message or "").strip()
    slots = dict(intent_slots or {})
    prep = _order_prep_dict(order_prep)
    known = _known_fields_from_prep(prep)
    channels = list(_ALL_CHANNELS)

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

    catalog_order = _is_catalog_order(inbound_metadata)
    channel = _selected_channel(msg)
    active_wa = (
        catalog_order
        or channel == "whatsapp_quick_order"
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
        )
        next_goal = "collect_next_whatsapp_order_field"
        if not missing:
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

    if _is_generic_buy_intent(msg, intent_name=intent_name) and not _BROWSE_SIGNAL_RE.search(_norm(msg)):
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
