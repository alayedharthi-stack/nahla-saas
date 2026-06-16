"""
core/wa_checkout_reply.py
─────────────────────────
Deterministic Arabic checkout replies after address ingestion for WA orders.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.merchant_payment_methods import (
    MerchantPaymentMethods,
    build_payment_options_lines,
)
from core.wa_order_lifecycle import compute_wa_missing_fields


def _prep_str(prep: Dict[str, Any], key: str) -> str:
    return str(prep.get(key) or "").strip()


def _format_line_item(item: Dict[str, Any]) -> str:
    name = (
        item.get("product_name")
        or item.get("title")
        or item.get("name")
        or "منتج"
    )
    qty = int(item.get("quantity") or 1)
    variant = str(
        item.get("variant_label") or item.get("size") or item.get("variant") or ""
    ).strip()
    edition = str(item.get("edition") or item.get("production") or "").strip()
    detail_parts = [p for p in (variant, edition) if p]
    detail = f" — {' '.join(detail_parts)}" if detail_parts else ""
    if qty > 1:
        return f"• {name}{detail} × {qty}"
    return f"• {name}{detail}"


def _format_total(prep: Dict[str, Any], brain_state: Dict[str, Any]) -> Optional[str]:
    for container in (prep, brain_state or {}):
        for key in ("order_total", "total", "total_sar", "amount_sar"):
            raw = container.get(key)
            if raw in (None, ""):
                continue
            text = str(raw).replace("ر.س", "").replace(",", "").strip()
            try:
                val = float(text)
                if val > 0:
                    return f"{val:.0f} ريال" if val == int(val) else f"{val:.2f} ريال"
            except (TypeError, ValueError):
                if text:
                    return f"{text} ريال"
    return None


def _format_shipping(prep: Dict[str, Any], brain_state: Dict[str, Any]) -> Optional[str]:
    for container in (prep, brain_state or {}):
        shipping = container.get("shipping_cost") or container.get("shipping_fee")
        if shipping is None:
            label = str(container.get("shipping_label") or "").strip()
            if label:
                return label
            if container.get("free_shipping"):
                return "مجاني"
            continue
        try:
            val = float(shipping)
            if val <= 0:
                return "مجاني"
            return f"{val:.0f} ريال"
        except (TypeError, ValueError):
            text = str(shipping).strip()
            if text:
                return text
    if prep.get("free_shipping") or (brain_state or {}).get("free_shipping"):
        return "مجاني"
    return None


def build_order_summary_lines(
    order_prep: Dict[str, Any],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    bs = brain_state or {}
    items = line_items or list(order_prep.get("line_items") or bs.get("cart_items") or [])
    lines: List[str] = ["طلبك:"]
    if items:
        for item in items:
            if isinstance(item, dict):
                lines.append(_format_line_item(item))
    else:
        product = _prep_str(order_prep, "product_title") or _prep_str(bs, "selected_product")
        if product:
            lines.append(f"• {product}")

    total = _format_total(order_prep, bs)
    if total:
        lines.append(f"• الإجمالي: {total}")

    shipping = _format_shipping(order_prep, bs)
    if shipping:
        lines.append(f"• الشحن: {shipping}")

    return lines


def build_checkout_payment_options_reply(
    order_prep: Dict[str, Any],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    line_items: Optional[List[Dict[str, Any]]] = None,
    payment_methods: MerchantPaymentMethods,
) -> str:
    """Full reply after address accepted and cart is complete."""
    summary = build_order_summary_lines(
        order_prep,
        brain_state=brain_state,
        line_items=line_items,
    )
    option_lines = build_payment_options_lines(payment_methods)
    if not option_lines:
        return (
            "وصل الموقع وتم تسجيله ✅\n\n"
            + "\n".join(summary)
            + "\n\nطلبك جاهز، لكن طرق الدفع غير مفعلة حالياً. "
            "سيتم تحويل الطلب للمتجر لإكماله."
        )
    return "وصل الموقع وتم تسجيله ✅\n\n" + "\n".join(summary) + "\n\n" + "\n".join(option_lines)


def build_incomplete_cart_address_reply() -> str:
    return (
        "وصل الموقع وتم تسجيله ✅\n"
        "باقي تحدد المنتج أو الكمية عشان نكمل الطلب."
    )


def compose_address_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    line_items: Optional[list] = None,
    payment_methods: Optional[MerchantPaymentMethods] = None,
) -> str:
    from core.wa_order_lifecycle import compute_wa_missing_fields  # noqa: PLC0415

    missing = compute_wa_missing_fields(
        order_prep,
        brain_state=brain_state or {},
        line_items=line_items,
    )
    if "product" in missing:
        return build_incomplete_cart_address_reply()

    if "delivery_address" in missing:
        return (
            "وصل الموقع وتم تسجيله ✅\n"
            "باقي نكمل بيانات التوصيل عشان نثبت الطلب."
        )

    if payment_methods is None:
        from core.merchant_payment_methods import resolve_merchant_payment_methods  # noqa: PLC0415

        payment_methods = resolve_merchant_payment_methods()

    return build_checkout_payment_options_reply(
        order_prep,
        brain_state=brain_state,
        line_items=line_items,
        payment_methods=payment_methods,
    )


__all__ = [
    "build_checkout_payment_options_reply",
    "build_incomplete_cart_address_reply",
    "build_order_summary_lines",
    "compose_address_reply",
]
