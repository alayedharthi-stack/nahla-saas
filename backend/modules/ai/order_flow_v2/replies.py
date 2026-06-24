"""OrderFlowV2 deterministic replies — built from state, not canned templates."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .missing_fields import next_missing_field
from .state import line_items_from_state, trusted_catalog_price


def _format_item(item: Dict[str, Any]) -> str:
    name = str(item.get("product_name") or item.get("title") or item.get("name") or "منتج").strip()
    qty = int(item.get("quantity") or 1)
    price = item.get("catalog_price") or item.get("item_price") or item.get("price")
    price_txt = ""
    if price not in (None, ""):
        try:
            val = float(price)
            price_txt = f" — {val:.0f} ر.س" if val == int(val) else f" — {val:.2f} ر.س"
        except (TypeError, ValueError):
            price_txt = f" — {price}"
    if qty > 1:
        return f"• {name}{price_txt} × {qty}"
    return f"• {name}{price_txt}"


def _order_summary(order_prep: Dict[str, Any], brain_state: Dict[str, Any]) -> str:
    items = line_items_from_state(order_prep, brain_state)
    if not items:
        product = str(order_prep.get("product_name") or "").strip()
        if product:
            return f"• {product}"
        return ""
    lines = [_format_item(it) for it in items[:5]]
    total = order_prep.get("order_flow_v2_catalog_total") or order_prep.get("order_total") or order_prep.get("total")
    body = "\n".join(lines)
    if total not in (None, "", 0):
        try:
            val = float(total)
            total_txt = f"{val:.0f} ر.س" if val == int(val) else f"{val:.2f} ر.س"
            body = f"{body}\nالإجمالي: {total_txt}"
        except (TypeError, ValueError):
            body = f"{body}\nالإجمالي: {total} ر.س"
    return body.strip()


def build_next_field_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
) -> str:
    """Ask only for the next missing checkout field."""
    nxt = next_missing_field(missing_fields)
    summary = _order_summary(order_prep, brain_state)
    prefix = f"{summary}\n\n" if summary else ""

    if nxt == "product":
        return f"{prefix}وش المنتج اللي تبغاه؟".strip()
    if nxt == "customer_name":
        return f"{prefix}وش اسمك الكامل؟".strip()
    if nxt == "city":
        return f"{prefix}وش المدينة؟".strip()
    if nxt == "delivery_address":
        return (
            f"{prefix}شاركنا عنوان التوصيل: رابط Google Maps أو الرمز الوطني المختصر "
            f"(مثل RIYD1234)."
        ).strip()
    if nxt == "payment_method":
        return f"{prefix}وش طريقة الدفع المناسبة لك؟".strip()
    return f"{prefix}أكمل معي بيانات الطلب.".strip()


def build_greeting_with_pending_hint(*, has_pending: bool) -> str:
    base = "وعليكم السلام، يا هلا فيك"
    if not has_pending:
        return base
    return f"{base}\n\nعندك طلب سابق غير مكتمل — إذا حاب نكمله قل: كمل الطلب."


def build_resume_ack(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
) -> str:
    summary = _order_summary(order_prep, brain_state)
    nxt = build_next_field_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
    )
    if summary and summary in nxt:
        return nxt
    return f"تمام، نكمل طلبك.\n\n{nxt}".strip()


def build_catalog_order_start_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
) -> str:
    if trusted_catalog_price(order_prep, brain_state):
        return build_next_field_reply(
            order_prep=order_prep,
            brain_state=brain_state,
            missing_fields=missing_fields,
        )
    return build_next_field_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
    )
