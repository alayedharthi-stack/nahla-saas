"""
commerce/catalog_order_checkout.py
──────────────────────────────────
Phase 1 enforce: WhatsApp native catalog order → continue checkout.

Operational only. This module never writes customer-facing text and never
routes to staff/contact surfaces. It only turns a current WhatsApp
``type=order`` payload into the existing checkout action.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from modules.ai.brain.decision.actions import (
    ACTION_CATALOG_NAVIGATE,
    ACTION_CLARIFY,
    ACTION_LLM_REPLY,
    ACTION_NARROW,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
)
from modules.ai.brain.types import BrainContext, Decision

_FLAG = "WA_CATALOG_ORDER_CONTINUE_CHECKOUT_ENABLED"
_FALSY = frozenset({"0", "false", "no", "off"})

_BROWSE_OR_LISTING_ACTIONS = frozenset({
    ACTION_CATALOG_NAVIGATE,
    ACTION_SEARCH_PRODUCTS,
    ACTION_CLARIFY,
    ACTION_NARROW,
    ACTION_LLM_REPLY,
    ACTION_STASH_ADDRESS_PRE_PRODUCT,
})


def catalog_order_continue_checkout_enabled() -> bool:
    """Feature flag for the limited Phase 1 enforce."""
    return os.getenv(_FLAG, "true").strip().lower() not in _FALSY


def _inbound_metadata(ctx: BrainContext) -> Dict[str, Any]:
    profile = getattr(ctx, "profile", None)
    if not isinstance(profile, dict):
        return {}
    meta = profile.get("inbound_metadata") or {}
    return dict(meta) if isinstance(meta, dict) else {}


def is_current_catalog_order_submitted(ctx: BrainContext) -> bool:
    """True only for the current inbound WhatsApp native catalog order event."""
    meta = _inbound_metadata(ctx)
    message = str(getattr(ctx, "message", "") or "")
    try:
        from modules.ai.order_flow_v2.triggers import is_catalog_order_inbound  # noqa: PLC0415

        return is_catalog_order_inbound(meta, message)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — local fallback below keeps legacy behavior
        pass
    source = str(meta.get("source_type") or "").strip().lower()
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    items = meta.get("product_items") or order.get("product_items") or []
    if source in {"catalog_order", "order"} and isinstance(items, list) and bool(items):
        return True
    if "[طلب كتالوج من العميل]" in message and items:
        return True
    return False


def _salla_external_id_from_line_item(item: Dict[str, Any]) -> str:
    """Store-platform product id only — never WhatsApp ``product_retailer_id`` / SKU."""
    for key in ("salla_product_id", "store_external_id", "store_product_id"):
        val = str(item.get(key) or "").strip()
        if val:
            return val
    return ""


def _catalog_items_from_metadata(meta: Dict[str, Any]) -> list[Dict[str, Any]]:
    raw = meta.get("product_items")
    if isinstance(raw, list) and raw:
        return [dict(x) for x in raw if isinstance(x, dict)]
    order = meta.get("order") if isinstance(meta.get("order"), dict) else {}
    nested = order.get("product_items")
    if isinstance(nested, list) and nested:
        return [dict(x) for x in nested if isinstance(x, dict)]
    return []


def catalog_order_extraction_facts(ctx: BrainContext) -> Dict[str, Any]:
    """Facts visible in a catalog_order fallback text when item payload is incomplete."""
    meta = _inbound_metadata(ctx)
    facts: Dict[str, Any] = {}
    items = _catalog_items_from_metadata(meta)
    if items:
        facts["line_items_count"] = len(items)
        skus = [
            str(item.get("product_retailer_id") or item.get("sku") or "").strip()
            for item in items
            if isinstance(item, dict)
        ]
        skus = [sku for sku in skus if sku]
        if skus:
            facts["catalog_skus"] = skus
    message = str(getattr(ctx, "message", "") or "")
    try:
        from core.wa_native_catalog_order import extract_catalog_order_text_facts  # noqa: PLC0415

        facts.update(extract_catalog_order_text_facts(message))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — text fact extraction is best-effort
        pass
    return facts


def _product_from_inbound_metadata(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    meta = _inbound_metadata(ctx)
    if not is_current_catalog_order_submitted(ctx):
        return None
    items = _catalog_items_from_metadata(meta)
    if not items:
        return None
    first = items[0]
    count = len(items)
    retailer_id = str(
        first.get("product_retailer_id")
        or first.get("sku")
        or first.get("retailer_id")
        or "",
    ).strip()
    names = meta.get("product_names")
    title = ""
    if isinstance(names, list) and names:
        title = str(names[0] or "").strip()
    if not title:
        title = str(first.get("product_name") or first.get("title") or "").strip()
    try:
        total_price = float(meta.get("total_price")) if meta.get("total_price") is not None else None
    except (TypeError, ValueError):
        total_price = None
    currency = str(meta.get("currency") or first.get("currency") or "").strip()
    product: Dict[str, Any] = {
        "id": retailer_id or "catalog_order",
        "title": title or retailer_id or "catalog_order",
        "price": total_price,
        "currency": currency,
        "from_catalog_order": True,
        "from_native_catalog_order": True,
        "line_items_count": count,
        "is_multi_item": count > 1,
        "line_items": items,
    }
    if retailer_id:
        product["product_retailer_id"] = retailer_id
    store_external_id = _salla_external_id_from_line_item(first)
    if store_external_id:
        product["external_id"] = store_external_id
    return product


def _product_from_state(ctx: BrainContext) -> Optional[Dict[str, Any]]:
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None)
    line_items = list(
        getattr(prep, "line_items", None) or getattr(state, "cart_items", None) or []
    )
    if line_items:
        first = next((li for li in line_items if isinstance(li, dict)), None) or {}
        count = len(line_items)
        retailer_id = str(first.get("product_retailer_id") or first.get("sku") or "").strip()
        product: Dict[str, Any] = {
            "id": first.get("product_id") or retailer_id or "catalog_order",
            "title": first.get("product_name") or first.get("title") or retailer_id or "",
            "price": prep.catalog_checkout_total if prep is not None else first.get("unit_price"),
            "currency": (
                getattr(prep, "catalog_checkout_currency", None)
                or first.get("currency")
                or ""
            ),
            "from_catalog_order": True,
            "from_native_catalog_order": True,
            "line_items_count": count,
            "is_multi_item": count > 1,
            "line_items": [dict(x) for x in line_items if isinstance(x, dict)],
        }
        if retailer_id:
            product["product_retailer_id"] = retailer_id
        store_external_id = _salla_external_id_from_line_item(first)
        if store_external_id:
            product["external_id"] = store_external_id
        return product

    focus = getattr(state, "current_product_focus", None)
    if isinstance(focus, dict) and focus:
        product = dict(focus)
        product.setdefault("from_catalog_order", True)
        product.setdefault("from_native_catalog_order", True)
        if product.get("from_native_catalog_order"):
            ext = str(product.get("external_id") or "").strip()
            if ext and not product.get("product_retailer_id"):
                product["product_retailer_id"] = ext
            product.pop("external_id", None)
        return product

    product = _product_from_inbound_metadata(ctx)
    if product:
        return product

    return None


def _catalog_order_continue_args(
    ctx: BrainContext,
    product: Dict[str, Any],
    *,
    reason: str,
) -> Dict[str, Any]:
    return {
        "product": product,
        "forced_product": product,
        "source": "catalog_order_submitted",
        "catalog_order_submitted": True,
        "continue_checkout": True,
        "native_catalog_order": {
            "event_type": "catalog_order_submitted",
            "source": "whatsapp_catalog",
            "phone_source": "whatsapp",
            "retailer_id": product.get("product_retailer_id") or "",
            "line_items_count": product.get("line_items_count") or 1,
        },
        "use_catalog_prices_only": True,
        "skip_product_discovery": True,
    }


def try_catalog_order_continue_decision(ctx: BrainContext) -> Optional[Decision]:
    """Early route: WhatsApp catalog order beats browse/search/discovery."""
    if not catalog_order_continue_checkout_enabled():
        return None
    if not is_current_catalog_order_submitted(ctx):
        return None
    decision = try_active_catalog_checkout_continue_decision(ctx)
    if decision is None:
        return try_catalog_order_extraction_fallback_decision(ctx)
    return Decision(
        action=decision.action,
        args=dict(decision.args or {}),
        reason="catalog_order_submitted → continue_checkout",
        confidence=1.0,
    )


def try_catalog_order_extraction_fallback_decision(ctx: BrainContext) -> Optional[Decision]:
    """Keep incomplete catalog_order payloads in checkout without asking product/quantity."""
    if not catalog_order_continue_checkout_enabled():
        return None
    if not is_current_catalog_order_submitted(ctx):
        return None
    if _product_from_state(ctx):
        return None
    facts = catalog_order_extraction_facts(ctx)
    return Decision(
        action=ACTION_LLM_REPLY,
        args={
            "topic": "catalog_order_extraction_incomplete",
            "catalog_order_facts": facts,
            "response_goal": (
                "Acknowledge that a WhatsApp catalog order was received, but the "
                "item details were not fully extracted. Mention any visible SKU, "
                "quantity, line count, or total from catalog_order_facts. Ask the "
                "customer to resend the catalog order or confirm the items as shown "
                "on their side. Do not ask what product or quantity they want, do "
                "not browse, and do not claim there is no order."
            ),
        },
        reason="catalog_order_extraction_incomplete → constrained_reply",
        confidence=0.97,
    )


def _prep_dict(order_prep: Any) -> Dict[str, Any]:
    if order_prep is None:
        return {}
    if isinstance(order_prep, dict):
        return dict(order_prep)
    if hasattr(order_prep, "to_dict"):
        try:
            return dict(order_prep.to_dict())
        except Exception:  # noqa: BLE001  # noqa: silent-ok — prep to_dict probe must not block checkout detection
            pass
    return dict(getattr(order_prep, "__dict__", {}) or {})


def is_active_catalog_checkout(ctx: BrainContext) -> bool:
    """
    True when a native catalog checkout session is still in progress — including
    follow-up turns after the initial catalog_order event.
    """
    if is_current_catalog_order_submitted(ctx):
        return True
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    prep_d = _prep_dict(prep)
    if is_catalog_line_items_authoritative_from_prep(prep):
        return True
    line_items = list(prep_d.get("line_items") or [])
    if line_items and prep_d.get("catalog_checkout_total") is not None:
        return True
    if prep_d.get("order_flow_v2_active") and prep_d.get("order_flow_v2_trusted_price"):
        return True
    for item in line_items:
        if not isinstance(item, dict):
            continue
        if item.get("from_native_catalog_order") or str(item.get("source") or "") == "whatsapp_native_catalog_order":
            return True
    focus = getattr(state, "current_product_focus", None) if state else None
    if isinstance(focus, dict) and (
        focus.get("from_catalog_order") or focus.get("from_native_catalog_order")
    ):
        if line_items or prep_d.get("catalog_checkout_total") is not None:
            return True
    return False


def try_active_catalog_checkout_continue_decision(ctx: BrainContext) -> Optional[Decision]:
    """Continue checkout from order_prep / active draft — not only current-turn catalog_order."""
    if not catalog_order_continue_checkout_enabled():
        return None
    if not is_active_catalog_checkout(ctx) or not current_turn_continues_catalog_checkout(ctx):
        return None
    product = _product_from_state(ctx)
    address_like = False
    try:
        from core.wa_address_ingestion import is_address_like_delivery_text  # noqa: PLC0415

        address_like = is_address_like_delivery_text(str(getattr(ctx, "message", "") or ""))
    except Exception:  # noqa: BLE001
        address_like = False
    if not product and not address_like:
        return None
    reason = (
        "active_catalog_checkout_address_like → continue_checkout"
        if address_like
        else "active_catalog_checkout → continue_checkout"
    )
    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args=_catalog_order_continue_args(
            ctx,
            product or {},
            reason=reason,
        ),
        reason=reason,
        confidence=0.98,
    )


def is_catalog_line_items_authoritative_from_prep(order_prep: Any) -> bool:
    """True when native catalog line items must not be cleared or re-prompted."""
    if order_prep is None:
        return False
    if isinstance(order_prep, dict):
        if order_prep.get("catalog_line_items_authoritative"):
            return True
        line_items = order_prep.get("line_items") or []
        if isinstance(line_items, list) and line_items:
            if order_prep.get("catalog_checkout_total") is not None:
                return True
        return False
    if bool(getattr(order_prep, "catalog_line_items_authoritative", False)):
        return True
    line_items = list(getattr(order_prep, "line_items", None) or [])
    if line_items and getattr(order_prep, "catalog_checkout_total", None) is not None:
        return True
    return False


def is_catalog_line_items_authoritative(ctx: BrainContext) -> bool:
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    if is_catalog_line_items_authoritative_from_prep(prep):
        return True
    meta = _inbound_metadata(ctx)
    if str(meta.get("source_type") or "").strip().lower() == "catalog_order":
        items = meta.get("product_items") or []
        return isinstance(items, list) and bool(items)
    return False


def maybe_enforce_catalog_order_continue_checkout(
    ctx: BrainContext,
    decision: Decision,
) -> Decision:
    """
    Force current native catalog order events into checkout continuation.

    This prevents regressions where a submitted catalog order drifts back into
    product browse/listing ("وش المتوفر؟", sections, catalog replay) even
    though the customer already selected a concrete product in WhatsApp.
    """
    if not catalog_order_continue_checkout_enabled():
        return decision
    if not current_turn_continues_catalog_checkout(ctx):
        return decision

    from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
        decision_owned_by_existing_order_support,
    )

    if decision_owned_by_existing_order_support(decision):
        return decision

    product = _product_from_state(ctx)
    if not product:
        fallback = try_catalog_order_extraction_fallback_decision(ctx)
        return fallback or decision

    args = dict(decision.args or {})
    args.update(
        _catalog_order_continue_args(
            ctx,
            product,
            reason="catalog_order_submitted → continue_checkout",
        ),
    )

    if decision.action == ACTION_PROPOSE_DRAFT_ORDER:
        return Decision(
            action=decision.action,
            args=args,
            reason=decision.reason or "catalog_order_submitted already in checkout",
            confidence=max(decision.confidence, 0.98),
        )

    if decision.action not in _BROWSE_OR_LISTING_ACTIONS:
        # Do not hijack unrelated operational owners; only prevent browse/listing drift.
        return decision

    return Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args=args,
        reason="catalog_order_submitted → continue_checkout",
        confidence=1.0,
    )


_ADDRESS_AWAITED_FIELDS = frozenset({
    "address",
    "address_location",
    "address_line",
    "short_address_code",
    "google_maps_url",
    "delivery_address",
    "location",
    "city",
    "district",
    "street",
    "postal_code",
})

_PAYMENT_AWAITED_FIELDS = frozenset({"payment_method"})

_EXTRACTED_SLOT_TO_AWAITED = {
    "customer_first_name": frozenset({
        "customer_first_name",
        "customer_last_name",
        "customer_name",
        "name",
        "full_name",
    }),
    "customer_last_name": frozenset({
        "customer_first_name",
        "customer_last_name",
        "customer_name",
        "name",
        "full_name",
    }),
    "customer_name": frozenset({
        "customer_first_name",
        "customer_last_name",
        "customer_name",
        "name",
        "full_name",
    }),
    "name": frozenset({
        "customer_first_name",
        "customer_last_name",
        "customer_name",
        "name",
        "full_name",
    }),
    "city": frozenset({"city"}),
    "customer_phone": frozenset({
        "phone",
        "customer_phone",
        "customer_phone_number",
        "mobile",
    }),
    "phone": frozenset({
        "phone",
        "customer_phone",
        "customer_phone_number",
        "mobile",
    }),
    "short_address_code": _ADDRESS_AWAITED_FIELDS,
    "google_maps_url": _ADDRESS_AWAITED_FIELDS,
    "latitude": _ADDRESS_AWAITED_FIELDS,
    "longitude": _ADDRESS_AWAITED_FIELDS,
    "address_line": _ADDRESS_AWAITED_FIELDS,
    "delivery_address": _ADDRESS_AWAITED_FIELDS,
    "street": _ADDRESS_AWAITED_FIELDS,
    "district": _ADDRESS_AWAITED_FIELDS,
    "postal_code": _ADDRESS_AWAITED_FIELDS,
    "building_number": _ADDRESS_AWAITED_FIELDS,
    "additional_number": _ADDRESS_AWAITED_FIELDS,
    "quantity": frozenset({"quantity", "qty"}),
    "qty": frozenset({"quantity", "qty"}),
}


def _awaited_checkout_fields(order_prep: Any) -> frozenset[str]:
    if order_prep is None:
        return frozenset()
    if isinstance(order_prep, dict):
        raw = order_prep.get("missing_fields") or []
    else:
        raw = getattr(order_prep, "missing_fields", None) or []
    return frozenset(str(item or "").strip() for item in raw if str(item or "").strip())


def _inbound_is_structured_location_event(ctx: BrainContext) -> bool:
    """True for a native WhatsApp location payload, not free-text location questions."""
    meta = _inbound_metadata(ctx)
    source = str(meta.get("source_type") or meta.get("type") or "").strip().lower()
    if source in {"location", "location_pin", "whatsapp_location"}:
        return True
    loc = meta.get("location") if isinstance(meta.get("location"), dict) else {}
    if loc.get("latitude") is not None or loc.get("longitude") is not None:
        return True
    if meta.get("latitude") is not None and meta.get("longitude") is not None:
        return True
    return False


def _tenant_payment_methods(ctx: BrainContext) -> Any:
    """Load merchant payment truth. Fail closed when tenant settings are unavailable."""
    db = getattr(ctx, "_db", None) or getattr(ctx, "db", None)
    try:
        tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    except (TypeError, ValueError):
        tenant_id = 0
    if db is None or tenant_id <= 0:
        return None
    try:
        from core.merchant_payment_methods import load_merchant_payment_methods  # noqa: PLC0415

        methods = load_merchant_payment_methods(db, tenant_id)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — missing tenant payment truth must not invent ownership
        return None
    if methods is None:
        return None
    if str(getattr(methods, "source", "") or "").strip().lower() == "fallback":
        return None
    if not list(getattr(methods, "available_methods", None) or []):
        return None
    return methods


def _inbound_is_tenant_payment_method_choice(ctx: BrainContext, awaited: frozenset[str]) -> bool:
    if not (awaited & _PAYMENT_AWAITED_FIELDS):
        return False
    message = str(getattr(ctx, "message", "") or "")
    if not message.strip():
        return False
    methods = _tenant_payment_methods(ctx)
    if methods is None:
        return False
    try:
        from core.merchant_payment_methods import inbound_is_payment_method_choice  # noqa: PLC0415

        return bool(inbound_is_payment_method_choice(message, methods))
    except Exception:  # noqa: BLE001  # noqa: silent-ok — payment extractor probe must not invent checkout ownership
        return False


def _current_extracted_checkout_slots(ctx: BrainContext) -> Dict[str, Any]:
    extracted: Dict[str, Any] = {}
    intent = getattr(ctx, "intent", None)
    slots = dict(getattr(intent, "slots", None) or {})
    for key, value in slots.items():
        if key in _EXTRACTED_SLOT_TO_AWAITED and value not in (None, "", [], {}):
            extracted[key] = value
    message = str(getattr(ctx, "message", "") or "")
    try:
        from modules.ai.brain.intent.ordering_extractor import extract_ordering_slots  # noqa: PLC0415

        extracted.update(extract_ordering_slots(message) or {})
    except Exception:  # noqa: BLE001  # noqa: silent-ok — existing slot extractor must not invent ownership
        pass
    return extracted


def _extracted_slots_match_awaited(ctx: BrainContext, awaited: frozenset[str]) -> bool:
    if not awaited:
        return False
    extracted = _current_extracted_checkout_slots(ctx)
    supplied: set[str] = set()
    for key, value in extracted.items():
        if value in (None, "", [], {}):
            continue
        supplied.update(_EXTRACTED_SLOT_TO_AWAITED.get(key, ()))
    return bool(supplied & awaited)


def current_turn_continues_catalog_checkout(ctx: BrainContext) -> bool:
    """
    True when THIS inbound structurally owns checkout.

    Distinct from :func:`is_active_catalog_checkout`, which only means
    resumable checkout context exists. Does not use the broad
    awaited-slot arbiter (option confirmation / receipt catch-alls).
    """
    if is_current_catalog_order_submitted(ctx):
        return True
    if not is_active_catalog_checkout(ctx):
        return False

    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) if state else None
    awaited = _awaited_checkout_fields(prep)
    message = str(getattr(ctx, "message", "") or "")

    if _inbound_is_structured_location_event(ctx) and (awaited & _ADDRESS_AWAITED_FIELDS):
        return True

    if _extracted_slots_match_awaited(ctx, awaited):
        return True

    if awaited & _ADDRESS_AWAITED_FIELDS:
        try:
            from core.wa_address_ingestion import is_address_like_delivery_text  # noqa: PLC0415

            if is_address_like_delivery_text(message):
                return True
        except Exception:  # noqa: BLE001  # noqa: silent-ok — existing address extractor must not invent ownership
            pass

    try:
        from modules.ai.brain.commerce.commerce_turn_contract import (  # noqa: PLC0415
            is_address_on_file_claim,
            is_same_order_confirmation,
        )

        if is_same_order_confirmation(message) or is_address_on_file_claim(message):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — existing continuation contracts must not invent ownership
        pass

    if _inbound_is_tenant_payment_method_choice(ctx, awaited):
        return True
    return False


__all__ = [
    "catalog_order_extraction_facts",
    "catalog_order_continue_checkout_enabled",
    "current_turn_continues_catalog_checkout",
    "is_active_catalog_checkout",
    "is_catalog_line_items_authoritative",
    "is_catalog_line_items_authoritative_from_prep",
    "is_current_catalog_order_submitted",
    "maybe_enforce_catalog_order_continue_checkout",
    "try_catalog_order_extraction_fallback_decision",
    "try_active_catalog_checkout_continue_decision",
    "try_catalog_order_continue_decision",
]
