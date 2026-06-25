"""
catalog_authoritative_line_items.py
───────────────────────────────────
Platform-wide guard: order line items require catalog evidence.

Free-text product mentions may be stored as review metadata only — they
must not become confirmed ``order_prep.line_items`` or ``Order.line_items``.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED, normalize_line_item

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_AUTHORITATIVE_SOURCES = frozenset({
    "whatsapp_native_catalog_order",
    "whatsapp_catalog_order",
    "catalog_order",
    "whatsapp_catalog",
    "merchant_dashboard",
    "catalog_picker",
    "merchant_catalog_picker",
})

_ADDRESS_CAPTURE_RE = re.compile(
    r"(?:"
    r"\u0627\u0644\u0639\u0646\u0648\u0627\u0646|\u0627\u0644\u0639\u0646\u0648\u0627\u0646\s*\u0627\u0644\u0648\u0637\u0646\u064a|"
    r"\u0627\u0644\u0631\u0645\u0632\s*\u0627\u0644\u0648\u0637\u0646\u064a|\u0647\u0630\u0627\s*\u0627\u0644\u0639\u0646\u0648\u0627\u0646\s*\u0627\u0644\u0635\u062d\u064a\u062d|"
    r"\u0627\u0644\u062d\u064a|\u0627\u0644\u0645\u062f\u064a\u0646\u0629|\u0627\u0644\u0645\u0648\u0642\u0639|\u0627\u0644\u0631\u0645\u0632\s*\u0627\u0644\u0628\u0631\u064a\u062f\u064a|"
    r"\u062a\u0635\u062d\u064a\u062d\s*\u0627\u0644\u0639\u0646\u0648\u0627\u0646|\u0639\u0634\u0627\u0646\s*\u0644\u0648"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ONLINE_STORE_ORDER_RE = re.compile(
    r"(?:"
    r"\u0637\u0644\u0628(?:\u062a|\u064a)?\s*\u0645\u0646\s*\u0627\u0644\u0645\u062a\u062c\u0631|"
    r"\u0637\u0644\u0628\s*\u0642\u0627\u0626\u0645|\u0639\u0646\u062f\u064a\s*\u0637\u0644\u0628|"
    r"\u0631\u0642\u0645\s*\u0627\u0644\u0637\u0644\u0628|\u0627\u0644\u0645\u062a\u062c\u0631\s*\u0627\u0644\u0625?\u0644\u0643\u062a\u0631\u0648\u0646\u064a|"
    r"\u0627\u0644\u0645\u062a\u062c\u0631\s*\u0627\u0644\u0627\u0644\u0643\u062a\u0631\u0648\u0646\u064a|online\s*store\s*order"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_SHIPPING_MISSING = frozenset({
    "delivery_address",
    "address",
    "address_line",
    "address_location",
    "short_address_code",
    "google_maps_url",
    "city",
})


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


def _prep_dict(order_prep: Any) -> Dict[str, Any]:
    if order_prep is None:
        return {}
    if isinstance(order_prep, dict):
        return dict(order_prep)
    if hasattr(order_prep, "to_dict"):
        try:
            return dict(order_prep.to_dict())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def line_item_has_catalog_evidence(item: Dict[str, Any]) -> bool:
    """True when a cart/order line item is backed by catalog evidence."""
    if not isinstance(item, dict) or not item:
        return False
    if item.get("from_native_catalog_order") or item.get("from_catalog_order"):
        return True
    if str(item.get("product_retailer_id") or "").strip():
        return True
    if item.get("catalog_product_id") is not None:
        return True
    source = str(item.get("source") or "").strip().lower()
    if source in _AUTHORITATIVE_SOURCES:
        return True
    if source in {"whatsapp_brain", "free_text_mention", "whatsapp"}:
        if item.get("from_native_catalog_order") or item.get("from_catalog_order"):
            return True
        if str(item.get("product_retailer_id") or "").strip():
            return True
        if item.get("catalog_product_id") is not None:
            return True
        return False
    product_id = str(item.get("product_id") or item.get("external_id") or "").strip()
    variant_id = str(item.get("variant_id") or item.get("external_variant_id") or "").strip()
    status = str(item.get("match_status") or "").strip().lower()
    has_price = item.get("unit_price") is not None or item.get("price") is not None
    if status == ITEM_STATUS_CONFIRMED and product_id and (has_price or variant_id):
        return True
    if product_id and variant_id and has_price:
        return True
    return False


def product_info_has_catalog_evidence(product_info: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(product_info, dict) or not product_info:
        return False
    if product_info.get("from_native_catalog_order") or product_info.get("from_catalog_order"):
        return True
    if str(product_info.get("product_retailer_id") or "").strip():
        return True
    if product_info.get("catalog_product_id") is not None:
        return True
    ext = str(product_info.get("external_id") or product_info.get("id") or "").strip()
    if ext and product_info.get("from_catalog_pick"):
        return True
    if ext and (product_info.get("price") is not None or product_info.get("unit_price") is not None):
        if product_info.get("from_search_pick") or product_info.get("pick_list_item"):
            return True
    return line_item_has_catalog_evidence(product_info)


def filter_authoritative_line_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        dict(item)
        for item in (items or [])
        if isinstance(item, dict) and line_item_has_catalog_evidence(item)
    ]


def authoritative_line_items_from_prep(order_prep: Any) -> List[Dict[str, Any]]:
    prep = _prep_dict(order_prep)
    if prep.get("catalog_line_items_authoritative"):
        raw = list(prep.get("line_items") or prep.get("cart_items") or [])
        return [dict(x) for x in raw if isinstance(x, dict)]
    return filter_authoritative_line_items(
        list(prep.get("line_items") or prep.get("cart_items") or [])
    )


def order_has_authoritative_products(order_prep: Any) -> bool:
    return bool(authoritative_line_items_from_prep(order_prep))


def is_shipping_address_capture_context(
    message: str = "",
    *,
    order_prep: Any = None,
    stage: str = "",
    missing_fields: Optional[Sequence[str]] = None,
) -> bool:
    """True when the turn should capture shipping/address — not products."""
    raw = (message or "").strip()
    if not raw:
        return False
    norm = _norm(raw)
    if _ADDRESS_CAPTURE_RE.search(norm):
        return True
    try:
        from services.address_resolution import extract_address_signals  # noqa: PLC0415

        signals = extract_address_signals(raw)
        if signals.get("short_address_code") or signals.get("google_maps_url"):
            return True
        if signals.get("city") and (
            "عنوان" in norm or "الوطني" in norm or len(norm.split()) <= 6
        ):
            return True
    except Exception:  # noqa: BLE001
        pass
    prep = _prep_dict(order_prep)
    miss = {str(x).strip().lower() for x in (missing_fields or prep.get("missing_fields") or [])}
    stage_norm = str(stage or prep.get("stage") or "").strip().lower()
    if stage_norm in {"checkout", "ordering"} and miss & _SHIPPING_MISSING:
        if _ADDRESS_CAPTURE_RE.search(norm) or any(
            k in norm for k in ("عنوان", "الوطني", "الحي", "المدينة", "الموقع", "صحيح")
        ):
            return True
    return False


def is_online_store_existing_order_message(message: str) -> bool:
    return bool(_ONLINE_STORE_ORDER_RE.search(_norm(message or "")))


def store_free_text_product_mention(
    prep: Any,
    *,
    mention: str,
    variant: str = "",
    catalog_match_status: str = "needs_review",
) -> None:
    """Persist free-text mention as metadata — never as a confirmed line item."""
    text = str(mention or "").strip()
    if not text:
        return
    entry = {
        "product_mention": text,
        "variant": str(variant or "").strip() or None,
        "catalog_match_status": catalog_match_status,
        "must_not_create_line_item_from_free_text": True,
    }
    if isinstance(prep, dict):
        mentions = list(prep.get("product_mentions") or [])
        if not any(str(m.get("product_mention") or "") == text for m in mentions if isinstance(m, dict)):
            mentions.append(entry)
        prep["product_mentions"] = mentions[-5:]
        return
    mentions = list(getattr(prep, "product_mentions", None) or [])
    if not any(str(m.get("product_mention") or "") == text for m in mentions if isinstance(m, dict)):
        mentions.append(entry)
    if hasattr(prep, "product_mentions"):
        prep.product_mentions = mentions[-5:]


def should_block_free_text_cart_capture(
    *,
    message: str = "",
    order_prep: Any = None,
    stage: str = "",
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    meta = dict(inbound_metadata or {})
    if str(meta.get("source_type") or "").strip().lower() == "catalog_order":
        return False
    prep = _prep_dict(order_prep)
    if prep.get("catalog_line_items_authoritative"):
        return True
    if is_online_store_existing_order_message(message):
        return True
    if is_shipping_address_capture_context(message, order_prep=order_prep, stage=stage):
        return True
    return False


def cart_add_allowed(
    *,
    intent: Dict[str, Any],
    product_info: Optional[Dict[str, Any]] = None,
    order_prep: Any = None,
) -> bool:
    if str(intent.get("product_id") or "").strip() and line_item_has_catalog_evidence(
        {"product_id": intent.get("product_id"), "match_status": ITEM_STATUS_CONFIRMED}
    ):
        return True
    if product_info_has_catalog_evidence(product_info):
        return True
    prep = _prep_dict(order_prep)
    if prep.get("catalog_line_items_authoritative"):
        return False
    return False


def sanitize_prep_line_items(prep: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """Drop non-authoritative line items from prep; return (items, changed)."""
    if isinstance(prep, dict):
        raw = list(prep.get("line_items") or [])
        kept = filter_authoritative_line_items(raw) if not prep.get("catalog_line_items_authoritative") else raw
        changed = len(kept) != len(raw)
        if changed:
            prep["line_items"] = kept
            if prep.get("cart_items"):
                prep["cart_items"] = kept
        return kept, changed
    raw = list(getattr(prep, "line_items", None) or [])
    if getattr(prep, "catalog_line_items_authoritative", False):
        return raw, False
    kept = filter_authoritative_line_items(raw)
    changed = len(kept) != len(raw)
    if changed and hasattr(prep, "line_items"):
        prep.line_items = kept
    return kept, changed


def build_blocked_add_metadata_item(intent: Dict[str, Any]) -> Dict[str, Any]:
    return normalize_line_item({
        "product_name": intent.get("product_name") or "",
        "variant": intent.get("variant") or "",
        "quantity": intent.get("quantity") or 1,
        "source": "free_text_mention",
        "match_status": "needs_review",
    })


__all__ = [
    "authoritative_line_items_from_prep",
    "cart_add_allowed",
    "filter_authoritative_line_items",
    "is_online_store_existing_order_message",
    "is_shipping_address_capture_context",
    "line_item_has_catalog_evidence",
    "order_has_authoritative_products",
    "product_info_has_catalog_evidence",
    "sanitize_prep_line_items",
    "should_block_free_text_cart_capture",
    "store_free_text_product_mention",
]
