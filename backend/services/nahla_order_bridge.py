"""
services/nahla_order_bridge.py
──────────────────────────────
Phase 1+2 — WhatsApp funnel → internal Nahla orders (draft → paid).

Additive bridge only. Does NOT touch the brain, payment classifier,
or store adapters.

Phase 1: confirmed receipt → ``status=paid``
Phase 2: checkout funnel   → ``status=pending_payment`` (draft), then promote
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nahla.order_bridge")

_NAHL_WA_EXT_PREFIX = "nahla-wa-"

_PAID_STATUSES = frozenset({
    "paid", "completed", "complete", "confirmed", "delivered",
    "delivering", "shipped", "out_for_delivery", "fulfilled",
})

_SYNC_FIELDS = (
    "product_id",
    "quantity",
    "line_items_fingerprint",
    "stage",
    "order_status",
    "awaiting_payment_receipt",
    "payment_receipt_received",
    "customer_first_name",
    "customer_last_name",
    "city",
    "short_address_code",
    "google_maps_url",
    "address_line",
    "lifecycle",
)

_PAID_ENRICHMENT_FIELDS = frozenset({
    "customer_first_name",
    "customer_last_name",
    "city",
    "short_address_code",
    "google_maps_url",
    "address_line",
})

_RECEIPT_TEXT_KEYS = (
    "vision_text",
    "frame_vision_text",
    "ocr_text",
    "pdf_text_preview",
    "pdf_text_full",
    "caption",
    "filename",
)


def nahla_wa_external_id(tenant_id: int, conversation_id: int) -> str:
    return f"{_NAHL_WA_EXT_PREFIX}{tenant_id}-{conversation_id}"


def _draft_bridge_enabled(tenant_id: int) -> bool:
    raw = (os.environ.get("NAHLA_ORDER_DRAFT_BRIDGE_ENABLED") or "").strip().lower()
    if raw not in ("1", "true", "yes", "on"):
        return False
    allowlist = (os.environ.get("NAHLA_ORDER_DRAFT_BRIDGE_TENANTS") or "").strip()
    if allowlist:
        allowed = {t.strip() for t in allowlist.split(",") if t.strip()}
        return str(tenant_id) in allowed
    return True


def _assert_tenant_ownership(
    *,
    tenant_id: int,
    conversation: Any,
    customer: Any = None,
    existing_order: Any = None,
) -> bool:
    tid = int(tenant_id)
    conv_tid = getattr(conversation, "tenant_id", None)
    if conv_tid is None or int(conv_tid) != tid:
        logger.error(
            "[NAHLA_ORDER_BRIDGE] action=skip skip_reason=tenant_ownership_mismatch "
            "param_tenant=%s conv_tenant=%s conv=%s",
            tid, conv_tid, getattr(conversation, "id", None),
        )
        return False
    cust = customer if customer is not None else getattr(conversation, "customer", None)
    if cust is not None:
        cust_tid = getattr(cust, "tenant_id", None)
        if cust_tid is None or int(cust_tid) != tid:
            logger.error(
                "[NAHLA_ORDER_BRIDGE] action=skip skip_reason=tenant_ownership_mismatch "
                "param_tenant=%s customer_tenant=%s customer=%s",
                tid, cust_tid, getattr(cust, "id", None),
            )
            return False
        conv_cid = getattr(conversation, "customer_id", None)
        cust_id = getattr(cust, "id", None)
        if conv_cid is not None and cust_id is not None and int(conv_cid) != int(cust_id):
            logger.error(
                "[NAHLA_ORDER_BRIDGE] action=skip skip_reason=customer_conversation_mismatch "
                "conv=%s conv_customer_id=%s customer_id=%s",
                getattr(conversation, "id", None), conv_cid, cust_id,
            )
            return False
    if existing_order is not None:
        order_tid = getattr(existing_order, "tenant_id", None)
        if order_tid is None or int(order_tid) != tid:
            logger.error(
                "[NAHLA_ORDER_BRIDGE] action=skip skip_reason=tenant_ownership_mismatch "
                "param_tenant=%s order_tenant=%s order_id=%s",
                tid, order_tid, getattr(existing_order, "id", None),
            )
            return False
    return True


def _is_paid(order: Any) -> bool:
    return str(getattr(order, "status", "") or "").lower() in _PAID_STATUSES


def _has_selected_product(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
) -> bool:
    for container in (order_prep, brain_state):
        if not isinstance(container, dict):
            continue
        for key in ("line_items", "cart_items", "items"):
            raw = container.get(key)
            if isinstance(raw, list) and raw:
                return True
    product_id = str(order_prep.get("product_id") or "").strip()
    if product_id:
        return True
    focus = brain_state.get("current_product_focus") or {}
    return isinstance(focus, dict) and bool(focus.get("id") or focus.get("title"))


def _draft_eligible(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
) -> Tuple[bool, str]:
    from core.wa_order_lifecycle import has_payment_submission  # noqa: PLC0415

    if has_payment_submission(order_prep):
        return True, "payment_submission_path"

    if order_prep.get("cart_deltas"):
        return True, "cart_deltas"

    if isinstance(order_prep.get("line_items"), list):
        return True, "explicit_line_items"

    if isinstance(order_prep.get("cart_items"), list) and order_prep.get("cart_items"):
        return True, "cart_items"

    if isinstance(brain_state.get("cart_items"), list) and brain_state.get("cart_items"):
        return True, "brain_cart_items"

    product_id = str(order_prep.get("product_id") or "").strip()
    stage = str(brain_state.get("stage") or "")
    awaiting = bool(order_prep.get("awaiting_payment_receipt"))
    order_status = str(order_prep.get("order_status") or "")

    if awaiting or order_status in ("awaiting_payment", "awaiting_receipt"):
        if product_id or _has_selected_product(order_prep, brain_state):
            return True, "awaiting_payment_receipt+product"
        return False, "awaiting_payment_no_product"

    if _has_selected_product(order_prep, brain_state):
        if stage in ("ordering", "deciding", "checkout", ""):
            return True, "product_selected"
        focus = brain_state.get("current_product_focus") or {}
        if isinstance(focus, dict) and focus.get("title"):
            return True, "product_focus"

    return False, "not_in_funnel"


def _scope_ok(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
) -> Tuple[bool, str]:
    from core.wa_order_lifecycle import has_payment_submission, is_payment_verified  # noqa: PLC0415

    if has_payment_submission(order_prep):
        return True, "payment_submission"
    if is_payment_verified(order_prep):
        return True, "paid_promotion"
    if str(brain_state.get("checkout_url") or "").strip():
        return False, "salla_checkout_active"
    return True, "nahla_native_funnel"


def _build_sync_snapshot(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    *,
    lifecycle: str,
    line_items_fingerprint: str = "",
) -> Dict[str, Any]:
    return {
        "product_id":               str(order_prep.get("product_id") or ""),
        "quantity":                 order_prep.get("quantity") or 1,
        "line_items_fingerprint":   line_items_fingerprint,
        "stage":                    str(brain_state.get("stage") or ""),
        "order_status":             str(order_prep.get("order_status") or ""),
        "awaiting_payment_receipt": bool(order_prep.get("awaiting_payment_receipt")),
        "payment_receipt_received": bool(order_prep.get("payment_receipt_received")),
        "customer_first_name":      str(order_prep.get("customer_first_name") or ""),
        "customer_last_name":       str(order_prep.get("customer_last_name") or ""),
        "city":                     str(order_prep.get("city") or ""),
        "short_address_code":       str(order_prep.get("short_address_code") or ""),
        "google_maps_url":          str(order_prep.get("google_maps_url") or ""),
        "address_line":             str(order_prep.get("address_line") or ""),
        "lifecycle":                lifecycle,
    }


def _meaningful_delta(
    prev: Optional[Dict[str, Any]],
    curr: Dict[str, Any],
) -> Tuple[bool, str]:
    if not prev:
        return True, "first_sync"
    for key in _SYNC_FIELDS:
        if prev.get(key) != curr.get(key):
            return True, f"changed:{key}"
    return False, "no_material_change"


def _should_update_paid_order(
    prev_snap: Optional[Dict[str, Any]],
    curr_snap: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> Tuple[bool, str]:
    if curr_snap.get("lifecycle") == "whatsapp_draft":
        return False, "paid_immutable:lifecycle_downgrade"

    if bool(order_prep.get("payment_receipt_received")):
        return True, "lifecycle:promote_paid"

    if not prev_snap:
        return False, "paid_immutable:no_snapshot"

    changed = {k for k in _SYNC_FIELDS if prev_snap.get(k) != curr_snap.get(k)}
    if not changed:
        return False, "paid_immutable:no_material_change"

    blocked = changed - _PAID_ENRICHMENT_FIELDS - {"payment_receipt_received", "lifecycle"}
    if blocked:
        return False, f"paid_immutable:fields={','.join(sorted(blocked))}"

    if changed & _PAID_ENRICHMENT_FIELDS:
        return True, "paid_enrichment:address_or_customer"

    return False, "paid_immutable:no_allowed_change"


def _resolve_sync_action(
    *,
    existing: Any,
    is_paid_path: bool,
    prev_snap: Optional[Dict[str, Any]],
    curr_snap: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> Tuple[bool, str, str]:
    if is_paid_path:
        if existing is None:
            return True, "lifecycle:promote_paid", "create"
        if _is_paid(existing):
            ok, reason = _should_update_paid_order(prev_snap, curr_snap, order_prep)
            return ok, reason, "update" if ok else "skip"
        return True, "lifecycle:promote_paid", "promote_paid"

    if existing is None:
        return True, "first_sync", "create"

    if _is_paid(existing):
        ok, reason = _should_update_paid_order(prev_snap, curr_snap, order_prep)
        return ok, reason, "update" if ok else "skip"

    ok, reason = _meaningful_delta(prev_snap, curr_snap)
    return ok, reason, "update" if ok else "skip"


def _log_bridge(
    *,
    external_id: str,
    tenant_id: int,
    conversation_id: Any,
    action: str,
    reason: str,
    status: str = "n/a",
    lifecycle: str = "n/a",
    eligibility_reason: str = "n/a",
    skip_reason: str = "",
    trigger: str = "unknown",
    **extra: Any,
) -> None:
    parts = [
        f"[NAHLA_ORDER_BRIDGE] external_id={external_id}",
        f"tenant={tenant_id} conv={conversation_id}",
        f"action={action}",
        f"reason={reason}",
        f"status={status}",
        f"lifecycle={lifecycle}",
        f"eligibility_reason={eligibility_reason}",
        f"trigger={trigger}",
    ]
    if skip_reason:
        parts.append(f"skip_reason={skip_reason}")
    for key, val in extra.items():
        parts.append(f"{key}={val}")
    logger.info(" ".join(parts))


def _looks_like_phone(text: str) -> bool:
    if not text:
        return False
    digits = text.lstrip("+").replace(" ", "").replace("-", "")
    return digits.isdigit() and len(digits) >= 7


def _parse_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        amt = float(value)
        return amt if amt > 0 else None
    text = str(value).replace("ر.س", "").replace("SAR", "").replace(",", "").strip()
    if not text:
        return None
    try:
        amt = float(text.split()[0])
        return amt if amt > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_receipt_amount(receipt_metadata: Dict[str, Any]) -> Optional[float]:
    try:
        from core.receipt_extraction import compute_receipt_fields  # noqa: PLC0415

        fields = compute_receipt_fields(metadata=receipt_metadata or {})
        for extracted in fields.amounts or ():
            amt = _parse_amount(getattr(extracted, "value", None))
            if amt is not None:
                return amt
    except Exception as exc:  # noqa: BLE001
        logger.debug("[NAHLA_ORDER_BRIDGE] receipt amount extraction failed: %s", exc)
    return None


def _explicit_payment_amount(
    *,
    receipt_metadata: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> Optional[float]:
    for container in (receipt_metadata, order_prep):
        for key in (
            "confirmed_payment_amount",
            "payment_amount",
            "amount",
            "total_amount",
            "receipt_amount",
        ):
            amt = _parse_amount(container.get(key))
            if amt is not None:
                return amt
    return None


def _enrich_receipt_metadata(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: int,
    receipt_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    merged = dict(receipt_metadata or {})
    if any(merged.get(k) for k in _RECEIPT_TEXT_KEYS):
        return merged

    wa_id = str(merged.get("wa_message_id") or "").strip()
    if not wa_id or db is None:
        return merged

    try:
        from models import MessageEvent  # noqa: PLC0415

        events = (
            db.query(MessageEvent)
            .filter(
                MessageEvent.tenant_id == tenant_id,
                MessageEvent.conversation_id == conversation_id,
                MessageEvent.direction == "inbound",
            )
            .order_by(MessageEvent.id.desc())
            .limit(40)
            .all()
        )
        for ev in events:
            em = getattr(ev, "extra_metadata", None) or {}
            ni = em.get("normalized_inbound") if isinstance(em.get("normalized_inbound"), dict) else {}
            nim = ni.get("metadata") if isinstance(ni, dict) and isinstance(ni.get("metadata"), dict) else {}
            for candidate in (nim, ni, em):
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("wa_message_id") or "").strip() != wa_id:
                    continue
                for key in _RECEIPT_TEXT_KEYS:
                    if candidate.get(key) and not merged.get(key):
                        merged[key] = candidate[key]
                return merged
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[NAHLA_ORDER_BRIDGE] receipt message lookup failed tenant=%s conv=%s: %s",
            tenant_id, conversation_id, exc,
        )
    return merged


def _resolve_order_amount(
    *,
    db: Any,
    tenant_id: int,
    conversation_id: int,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    receipt_metadata: Dict[str, Any],
    line_items: List[Dict[str, Any]],
    is_paid_path: bool,
) -> Tuple[Optional[float], bool, str]:
    enriched_receipt = _enrich_receipt_metadata(
        db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        receipt_metadata=receipt_metadata,
    )

    if is_paid_path:
        receipt_amt = _extract_receipt_amount(enriched_receipt)
        if receipt_amt is not None:
            return receipt_amt, False, "receipt_extraction"

        explicit_amt = _explicit_payment_amount(
            receipt_metadata=enriched_receipt,
            order_prep=order_prep,
        )
        if explicit_amt is not None:
            return explicit_amt, False, "confirmed_payment_amount"

    for key in ("total_price", "price"):
        amt = _parse_amount(order_prep.get(key))
        if amt is not None:
            return amt, False, "order_prep_total_price"

    focus = brain_state.get("current_product_focus") or {}
    if isinstance(focus, dict):
        amt = _parse_amount(focus.get("price"))
        if amt is not None:
            return amt, False, "product_focus_price"

    from core.wa_cart_line_items import cart_total_amount  # noqa: PLC0415

    cart_total = cart_total_amount(line_items)
    if cart_total is not None:
        return cart_total, False, "line_items"

    return None, not is_paid_path, "unknown"


def _enrich_line_item_titles(
    *,
    db: Any,
    tenant_id: int,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        name = str(item.get("product_name") or item.get("title") or "").strip()
        if name and name != "منتج":
            enriched.append(item)
            continue
        looked_up = _lookup_catalog_product_title(
            db, tenant_id, item.get("product_id") or item.get("catalog_id"),
        )
        if looked_up:
            item["product_name"] = looked_up
            item["title"] = looked_up
            item["name"] = looked_up
            item["display_name"] = looked_up
        enriched.append(item)
    return enriched


def _build_line_items(
    *,
    db: Any,
    tenant_id: int,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    existing_meta: Optional[Dict[str, Any]] = None,
    existing_line_items: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], str, List[Dict[str, Any]]]:
    from core.wa_cart_line_items import (  # noqa: PLC0415
        build_line_items_from_order_prep,
    )

    items, primary_title, cart_events = build_line_items_from_order_prep(
        order_prep=order_prep,
        brain_state=brain_state,
        existing_meta=existing_meta,
        existing_line_items=existing_line_items,
    )
    if db is not None and items:
        from core.wa_cart_catalog_resolver import resolve_cart_line_items  # noqa: PLC0415

        resolution = resolve_cart_line_items(db, tenant_id, items)
        items = resolution.items
        order_prep["wa_cart_catalog_resolution"] = {
            "needs_clarification": resolution.needs_clarification,
            "clarification_question": resolution.clarification_question,
            "variant_unavailable": resolution.variant_unavailable,
            "unmatched_items": resolution.unmatched_items,
            "closest_suggestions": resolution.closest_suggestions,
        }
    items = _enrich_line_item_titles(db=db, tenant_id=tenant_id, items=items)
    if not primary_title or primary_title == "منتج":
        primary_title = _resolve_product_title(
            db=db,
            tenant_id=tenant_id,
            order_prep=order_prep,
            brain_state=brain_state,
            existing_meta=existing_meta,
        )
    return items, primary_title, cart_events


def _lookup_catalog_product_title(
    db: Any,
    tenant_id: int,
    product_ref: Any,
) -> Optional[str]:
    if not product_ref or db is None:
        return None
    ref = str(product_ref).strip()
    if not ref:
        return None
    try:
        from models import Product  # noqa: PLC0415

        q = db.query(Product).filter(Product.tenant_id == tenant_id)
        row = None
        if ref.isdigit():
            row = q.filter(Product.id == int(ref)).first()
        if row is None:
            row = q.filter(
                (Product.external_id == ref) | (Product.sku == ref)
            ).first()
        if row and row.title:
            title = str(row.title).strip()
            return title or None
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[NAHLA_ORDER_BRIDGE] product title lookup failed tenant=%s ref=%s: %s",
            tenant_id, ref, exc,
        )
    return None


def _collect_cart_item_titles(
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    existing_meta: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Titles from cart/line_items on prep, brain state, or prior order metadata."""
    titles: List[str] = []
    for container in (order_prep, brain_state, existing_meta or {}):
        if not isinstance(container, dict):
            continue
        for key in ("line_items", "cart_items", "items"):
            raw_items = container.get(key)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                for field in ("title", "product_name", "name"):
                    name = str(item.get(field) or "").strip()
                    if name and name != "منتج":
                        titles.append(name)
    return titles


def _resolve_product_title(
    *,
    db: Any,
    tenant_id: int,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    existing_meta: Optional[Dict[str, Any]] = None,
) -> str:
    focus = brain_state.get("current_product_focus") or {}
    if not isinstance(focus, dict):
        focus = {}
    meta = existing_meta or {}

    product_name = str(order_prep.get("product_name") or "").strip()
    if product_name and product_name != "منتج":
        return product_name

    for raw in (focus.get("title"), focus.get("name")):
        name = str(raw or "").strip()
        if name and name != "منتج":
            return name

    for title in _collect_cart_item_titles(order_prep, brain_state, meta):
        return title

    for raw in (
        order_prep.get("product_title"),
        order_prep.get("selected_product"),
        meta.get("product_title"),
    ):
        name = str(raw or "").strip()
        if name and name != "منتج":
            return name

    product_ref = focus.get("id") or order_prep.get("product_id")
    looked_up = _lookup_catalog_product_title(db, tenant_id, product_ref)
    if looked_up:
        return looked_up
    return "منتج"


def _format_total_sar(amount: Optional[float]) -> Optional[str]:
    if amount is None or amount <= 0:
        return None
    return f"{amount:.2f} ر.س"


def _allocate_nhl_number(db: Any, tenant_id: int) -> str:
    from sqlalchemy import func  # noqa: PLC0415
    from models import Order  # noqa: PLC0415

    existing_count = (
        db.query(func.count(Order.id))
        .filter(
            Order.tenant_id == tenant_id,
            Order.external_id.like(f"{_NAHL_WA_EXT_PREFIX}{tenant_id}-%"),
        )
        .scalar()
    ) or 0
    seq = int(existing_count) + 1
    return f"NHL-{tenant_id}-{seq:06d}"


def _resolve_customer_name(
    conversation: Any,
    order_prep: Dict[str, Any],
) -> Optional[str]:
    candidates: List[str] = []

    first = str(order_prep.get("customer_first_name") or "").strip()
    last = str(order_prep.get("customer_last_name") or "").strip()
    prep_name = " ".join(p for p in (first, last) if p).strip()
    if prep_name:
        candidates.append(prep_name)

    customer = getattr(conversation, "customer", None)
    if customer is not None:
        cust_meta = getattr(customer, "extra_metadata", None) or {}
        if isinstance(cust_meta, dict):
            for key in ("wa_profile_name", "profile_name", "whatsapp_name", "display_name"):
                candidates.append(str(cust_meta.get(key) or "").strip())
        candidates.append(str(getattr(customer, "name", None) or "").strip())

    conv_meta = getattr(conversation, "extra_metadata", None) or {}
    if isinstance(conv_meta, dict):
        for key in ("customer_name", "contact_name", "wa_profile_name", "profile_name"):
            candidates.append(str(conv_meta.get(key) or "").strip())

    for raw in candidates:
        name = (raw or "").strip()
        if not name or _looks_like_phone(name):
            continue
        return name
    return None


def _conversation_phone(conversation: Any, order_prep: Dict[str, Any]) -> Optional[str]:
    customer = getattr(conversation, "customer", None)
    phone = None
    if customer is not None:
        phone = getattr(customer, "phone", None) or getattr(customer, "mobile", None)
    conv_meta = getattr(conversation, "extra_metadata", None) or {}
    if not phone and isinstance(conv_meta, dict):
        phone = conv_meta.get("phone")
    if not phone:
        phone = order_prep.get("customer_phone")
    return str(phone).strip() if phone else None


def _append_status_timeline(
    meta: Dict[str, Any],
    *,
    from_status: str,
    to_status: str,
    reason: str,
    now_iso: str,
) -> None:
    if from_status == to_status:
        return
    timeline = list(meta.get("status_timeline") or [])
    timeline.append({
        "from": from_status or "none",
        "to":   to_status,
        "at":   now_iso,
        "reason": reason,
    })
    meta["status_timeline"] = timeline[-50:]


def _append_cart_timeline(meta: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
    if not events:
        return
    timeline = list(meta.get("cart_timeline") or [])
    timeline.extend(events)
    meta["cart_timeline"] = timeline[-100:]


def _customer_payload(
    conversation: Any,
    order_prep: Dict[str, Any],
) -> Tuple[Optional[str], Dict[str, Any]]:
    phone = _conversation_phone(conversation, order_prep)

    display_name = _resolve_customer_name(conversation, order_prep)
    if not display_name:
        logger.info(
            "[ORDER_NAME_FALLBACK] prep_first=%r prep_last=%r phone=%r "
            "conv_id=%s",
            order_prep.get("customer_first_name"),
            order_prep.get("customer_last_name"),
            phone,
            getattr(conversation, "id", None),
        )
    customer_info = {
        "name":           display_name,
        "phone":          phone,
        "shipping_phone": phone,
        "city":           order_prep.get("city"),
    }
    if phone:
        customer_info["mobile"] = phone
    return display_name, customer_info


def _base_metadata(
    *,
    conversation_id: int,
    lifecycle: str,
    is_paid_path: bool,
    receipt_metadata: Dict[str, Any],
    amount: Optional[float],
    amount_source: str,
    needs_review: bool,
    fallback_used: bool,
    customer_name: Optional[str],
    product_title: Optional[str],
    confirmed_at: str,
    now_iso: str,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "source_kind":          "nahla_order",
        "lifecycle":            lifecycle,
        "origin":               "whatsapp_ai",
        "created_by":           "ai_assistant",
        "source":               "ai_sales_agent",
        "created_via":          "nahla_order_bridge",
        "conversation_id":      conversation_id,
        "needs_amount_review":  needs_review,
        "amount_source":        amount_source,
        "amount_value":         amount,
        "amount_fallback_used": fallback_used,
        "counts_in_revenue":    is_paid_path,
        "last_synced_at":       now_iso,
    }
    if is_paid_path:
        meta["payment_confirmed_at"] = confirmed_at
        meta["created_at"] = confirmed_at
    else:
        meta["draft_created_at"] = now_iso
        meta["counts_in_revenue"] = False
    if receipt_metadata:
        meta["payment_receipt_metadata"] = receipt_metadata
        if not is_paid_path:
            meta["payment_evidence_on_draft"] = True
    if customer_name:
        meta["customer_name"] = customer_name
    if product_title and product_title != "منتج":
        meta["product_title"] = product_title
    return meta


def sync_nahla_wa_order(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
    trigger: str = "unknown",
    customer: Any = None,
) -> Optional[Any]:
    """
    Upsert a Nahla-native WhatsApp order — draft (pending_payment) or paid.

    Idempotent on ``external_id = nahla-wa-{tenant_id}-{conversation_id}``.
    Never raises.
    """
    conversation_id = getattr(conversation, "id", None)
    if not conversation_id:
        _log_bridge(
            external_id="n/a",
            tenant_id=tenant_id,
            conversation_id="n/a",
            action="skip",
            reason="missing_conversation_id",
            skip_reason="missing_conversation_id",
            trigger=trigger,
        )
        return None

    cust = customer if customer is not None else getattr(conversation, "customer", None)
    if not _assert_tenant_ownership(
        tenant_id=tenant_id,
        conversation=conversation,
        customer=cust,
    ):
        _log_bridge(
            external_id=nahla_wa_external_id(tenant_id, int(conversation_id)),
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action="skip",
            reason="tenant_ownership_mismatch",
            skip_reason="tenant_ownership_mismatch",
            trigger=trigger,
        )
        return None

    external_id = nahla_wa_external_id(tenant_id, int(conversation_id))
    from core.wa_order_lifecycle import (  # noqa: PLC0415
        has_payment_submission,
        is_payment_verified,
    )

    is_payment_submitted_path = has_payment_submission(order_prep)
    is_paid_path = is_payment_submitted_path and (
        is_payment_verified(order_prep)
        or bool(order_prep.get("payment_verified"))
    )
    eligibility_reason = "n/a"

    if is_payment_submitted_path:
        eligibility_reason = "payment_submitted_path" if not is_paid_path else "paid_promotion"
    else:
        if not _draft_bridge_enabled(tenant_id):
            _log_bridge(
                external_id=external_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="skip",
                reason="draft_bridge_disabled",
                skip_reason="draft_bridge_disabled",
                trigger=trigger,
            )
            return None
        eligible, eligibility_reason = _draft_eligible(order_prep, brain_state)
        if not eligible:
            _log_bridge(
                external_id=external_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="skip",
                reason="not_eligible",
                eligibility_reason=eligibility_reason,
                skip_reason="not_eligible",
                trigger=trigger,
            )
            return None
        scope_ok, scope_reason = _scope_ok(order_prep, brain_state)
        if not scope_ok:
            _log_bridge(
                external_id=external_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="skip",
                reason=scope_reason,
                eligibility_reason=eligibility_reason,
                skip_reason=scope_reason,
                trigger=trigger,
            )
            return None

    lifecycle = "paid" if is_paid_path else "whatsapp_draft"
    wa_phone = _conversation_phone(conversation, order_prep)
    from core.wa_order_lifecycle import (  # noqa: PLC0415
        ADDRESS_REQUIRED_TYPE,
        STATUS_DRAFT,
        compute_wa_missing_fields,
        has_accepted_delivery_address,
        resolve_wa_order_status,
    )
    from core.wa_cart_line_items import (  # noqa: PLC0415
        format_cart_summary_ar,
        line_items_fingerprint,
    )

    try:
        from models import Order  # noqa: PLC0415

        existing = (
            db.query(Order)
            .filter_by(tenant_id=tenant_id, external_id=external_id)
            .first()
        )

        if existing is not None and not _assert_tenant_ownership(
            tenant_id=tenant_id,
            conversation=conversation,
            customer=cust,
            existing_order=existing,
        ):
            _log_bridge(
                external_id=external_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="skip",
                reason="tenant_ownership_mismatch",
                skip_reason="order_tenant_mismatch",
                trigger=trigger,
            )
            return None

        existing_meta = dict(existing.extra_metadata or {}) if existing is not None else {}
        raw_existing_items = getattr(existing, "line_items", None) if existing is not None else None
        existing_line_items = list(raw_existing_items or []) if existing is not None else None
        line_items, product_title, cart_events = _build_line_items(
            db=db,
            tenant_id=tenant_id,
            order_prep=order_prep,
            brain_state=brain_state,
            existing_meta=existing_meta,
            existing_line_items=existing_line_items,
        )

        resolved_status, missing_fields, delivery_address_status = resolve_wa_order_status(
            order_prep,
            brain_state,
            whatsapp_phone=wa_phone,
            payment_verified=is_paid_path,
            line_items=line_items,
        )
        if resolved_status is None and not is_payment_submitted_path:
            if existing is None:
                _log_bridge(
                    external_id=external_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    action="skip",
                    reason="no_product_selected",
                    eligibility_reason=eligibility_reason,
                    skip_reason="no_product_selected",
                    trigger=trigger,
                )
                return None
            resolved_status = STATUS_DRAFT
            missing_fields = compute_wa_missing_fields(
                order_prep,
                brain_state=brain_state,
                whatsapp_phone=wa_phone,
                line_items=line_items,
            )
            delivery_address_status = (
                "accepted"
                if has_accepted_delivery_address(order_prep)
                else "required"
            )

        items_fp = line_items_fingerprint(line_items)
        curr_snap = _build_sync_snapshot(
            order_prep,
            brain_state,
            lifecycle=lifecycle,
            line_items_fingerprint=items_fp,
        )

        prev_snap = existing_meta.get("last_sync_snapshot") if existing else None
        sync_ok, sync_reason, bridge_action = _resolve_sync_action(
            existing=existing,
            is_paid_path=is_paid_path,
            prev_snap=prev_snap,
            curr_snap=curr_snap,
            order_prep=order_prep,
        )

        if not sync_ok:
            _log_bridge(
                external_id=external_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action="skip",
                reason=sync_reason,
                status=str(getattr(existing, "status", "n/a") if existing else "n/a"),
                lifecycle=str((existing.extra_metadata or {}).get("lifecycle", "n/a") if existing else "n/a"),
                eligibility_reason=eligibility_reason,
                skip_reason=sync_reason,
                trigger=trigger,
            )
            return existing

        receipt_metadata = dict(order_prep.get("payment_receipt_metadata") or {})
        amount, needs_review, amount_source = _resolve_order_amount(
            db=db,
            tenant_id=tenant_id,
            conversation_id=int(conversation_id),
            order_prep=order_prep,
            brain_state=brain_state,
            receipt_metadata=receipt_metadata,
            line_items=line_items,
            is_paid_path=is_paid_path or is_payment_submitted_path,
        )
        total_str = _format_total_sar(amount)
        customer_name, customer_info = _customer_payload(conversation, order_prep)
        fallback_used = amount_source not in (
            "receipt_extraction",
            "confirmed_payment_amount",
        )
        confirmed_at = (
            str(order_prep.get("payment_receipt_at") or "").strip()
            or datetime.now(timezone.utc).isoformat()
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        from core.order_payment_policy import (  # noqa: PLC0415
            enrich_order_payment_metadata,
            guard_wa_target_status,
        )

        target_status = resolved_status or ("paid" if is_paid_path else "draft")
        target_status = guard_wa_target_status(
            target_status,
            order_prep,
            existing_meta if existing is not None else {},
        )
        base_meta = _base_metadata(
            conversation_id=int(conversation_id),
            lifecycle=lifecycle,
            is_paid_path=is_paid_path,
            receipt_metadata=receipt_metadata,
            amount=amount,
            amount_source=amount_source,
            needs_review=needs_review,
            fallback_used=fallback_used,
            customer_name=customer_name,
            product_title=product_title,
            confirmed_at=confirmed_at,
            now_iso=now_iso,
        )
        base_meta["last_sync_snapshot"] = curr_snap
        base_meta["missing_fields"] = missing_fields
        base_meta["delivery_address_status"] = delivery_address_status
        base_meta["address_required_type"] = ADDRESS_REQUIRED_TYPE
        if wa_phone:
            base_meta["customer_phone_source"] = "whatsapp_conversation"
        base_meta["counts_in_revenue"] = target_status == "paid"
        cart_summary = format_cart_summary_ar(line_items)
        if cart_summary:
            base_meta["cart_summary_ar"] = cart_summary
        _append_cart_timeline(base_meta, cart_events)
        if is_payment_submitted_path:
            from core.wa_payment_submission import build_payment_submission_order_metadata  # noqa: PLC0415

            submission_type = str(
                order_prep.get("payment_submission_type")
                or ("receipt" if order_prep.get("payment_receipt_received") else "text_claim")
            )
            base_meta.update(build_payment_submission_order_metadata(
                submission_type=submission_type,
                trigger=trigger,
            ))
            base_meta["payment_confirmed"] = bool(is_paid_path)
            if is_paid_path:
                base_meta["payment_verification_status"] = "confirmed"

        base_meta = enrich_order_payment_metadata(
            base_meta,
            order_prep=order_prep,
            target_status=target_status,
        )

        if existing is not None:
            meta = dict(existing.extra_metadata or {})
            prev_status = str(existing.status or "")
            base_meta["status_timeline"] = list(meta.get("status_timeline") or [])
            _append_status_timeline(
                base_meta,
                from_status=prev_status,
                to_status=target_status,
                reason=sync_reason,
                now_iso=now_iso,
            )
            if _is_paid(existing) and not is_paid_path:
                _log_bridge(
                    external_id=external_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    action="skip",
                    reason="paid_immutable",
                    status=existing.status,
                    lifecycle=meta.get("lifecycle", "paid"),
                    skip_reason="paid_immutable",
                    trigger=trigger,
                )
                return existing

            meta.update(base_meta)
            existing.status = target_status
            existing.source = "whatsapp"
            existing.is_abandoned = False
            if total_str and (is_paid_path or not _is_paid(existing)):
                existing.total = total_str
            if is_paid_path and total_str:
                meta["needs_amount_review"] = False
            existing.line_items = line_items
            if customer_name:
                existing.customer_name = customer_name
            if customer_info:
                existing.customer_info = {**(existing.customer_info or {}), **customer_info}
            existing.extra_metadata = meta
            db.add(existing)
            _log_bridge(
                external_id=external_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                action=bridge_action,
                reason=sync_reason,
                status=target_status,
                lifecycle=lifecycle,
                eligibility_reason=eligibility_reason,
                trigger=trigger,
                amount_source=amount_source,
                amount_value=amount if amount is not None else "unknown",
            )
            return existing

        _append_status_timeline(
            base_meta,
            from_status="none",
            to_status=target_status,
            reason=sync_reason,
            now_iso=now_iso,
        )
        order = Order(
            tenant_id             = tenant_id,
            external_id           = external_id,
            external_order_number = _allocate_nhl_number(db, tenant_id),
            status                = target_status,
            total                 = total_str,
            customer_name         = customer_name,
            customer_info         = customer_info,
            line_items            = line_items,
            checkout_url          = None,
            is_abandoned          = False,
            source                = "whatsapp",
            extra_metadata        = base_meta,
        )
        db.add(order)
        db.flush()
        _log_bridge(
            external_id=external_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            action=bridge_action,
            reason=sync_reason,
            status=target_status,
            lifecycle=lifecycle,
            eligibility_reason=eligibility_reason,
            trigger=trigger,
            amount_source=amount_source,
            amount_value=amount if amount is not None else "unknown",
            order_id=order.id,
            number=order.external_order_number,
        )
        return order
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[NAHLA_ORDER_BRIDGE] external_id=%s action=skip skip_reason=upsert_failed "
            "tenant=%s conv=%s trigger=%s err=%s",
            external_id, tenant_id, conversation_id, trigger, exc,
        )
        return None


def upsert_nahla_paid_order(
    db: Any,
    *,
    tenant_id: int,
    conversation: Any,
    brain_state: Dict[str, Any],
    order_prep: Dict[str, Any],
) -> Optional[Any]:
    """Explicit verified payment → ``paid`` (staff/system confirmation only)."""
    from core.wa_order_lifecycle import has_payment_submission  # noqa: PLC0415

    if not (
        bool(order_prep.get("payment_confirmed"))
        or bool(order_prep.get("verified_by_staff"))
        or bool(order_prep.get("payment_verified"))
    ):
        return None
    if not has_payment_submission(order_prep) and not order_prep.get("payment_receipt_received"):
        return None
    enriched = dict(order_prep)
    enriched.setdefault("payment_confirmed", True)
    return sync_nahla_wa_order(
        db,
        tenant_id=tenant_id,
        conversation=conversation,
        brain_state=brain_state,
        order_prep=enriched,
        trigger="paid_upsert",
    )


def compute_kpi_totals(orders: List[Any]) -> Dict[str, float]:
    """
    Mirror store_sync revenue rules for tests.

    Draft ``pending_payment`` rows count in ``orders_count`` but never in
    ``revenue`` or ``ai_revenue``.
    """
    PAID = frozenset({
        "paid", "completed", "complete", "confirmed", "delivered",
        "delivering", "shipped", "out_for_delivery", "fulfilled",
    })
    WA_SOURCES = frozenset({"whatsapp", "ai_sales_agent", "ai_sales", "ai"})

    orders_count = 0
    revenue = 0.0
    ai_revenue = 0.0

    for order in orders:
        orders_count += 1
        raw_status = str(getattr(order, "status", "") or "").lower()
        status = "paid" if raw_status in PAID else "pending"
        total = getattr(order, "total", None) or ""
        try:
            amt = float(str(total).replace(",", "").split()[0])
        except Exception:
            amt = 0.0
        src = (getattr(order, "source", None) or "").strip().lower()
        if status == "paid":
            revenue += amt
            if src in WA_SOURCES:
                ai_revenue += amt

    return {
        "orders_count": float(orders_count),
        "revenue":      revenue,
        "ai_revenue":   ai_revenue,
    }
