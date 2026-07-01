"""OrderFlowV2 deterministic replies — built from state, not canned templates."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.order_context_prefill import MODE_CONFIRM

from .missing_fields import next_missing_field
from .state import line_items_from_state, trusted_catalog_price


def _format_item(item: Dict[str, Any]) -> str:
    from modules.ai.brain.commerce.catalog_order_resilience import (  # noqa: PLC0415
        format_line_item_quantity,
        safe_line_item_quantity,
    )

    name = str(item.get("product_name") or item.get("title") or item.get("name") or "منتج").strip()
    qty = safe_line_item_quantity(item.get("quantity"))
    qty_txt = format_line_item_quantity(qty)
    price = item.get("catalog_price") or item.get("item_price") or item.get("price")
    price_txt = ""
    if price not in (None, ""):
        try:
            val = float(price)
            price_txt = f" — {val:.0f} ر.س" if val == int(val) else f" — {val:.2f} ر.س"
        except (TypeError, ValueError):
            price_txt = f" — {price}"
    if qty > 1 or qty != int(qty):
        return f"• {name}{price_txt} × {qty_txt}"
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


def _build_saved_address_confirm_reply(
    prefix: str,
    *,
    known_previous: Optional[Dict[str, str]],
    order_prep: Dict[str, Any],
) -> str:
    known = dict(known_previous or {})
    city = known.get("city") or str(order_prep.get("city") or "").strip()
    short_code = (
        known.get("short_address")
        or str(order_prep.get("short_address_code") or "").strip()
    )
    if not city and not short_code:
        return ""

    if city and short_code:
        body = (
            f"العنوان المسجل عندنا في {city}، والرمز المختصر {short_code}.\n"
            "هل نعتمد نفس العنوان؟"
        )
    elif city:
        body = f"أشوف المدينة المسجلة عندنا: {city}. أرسل لي الرمز المختصر أو رابط الموقع حتى نكمل الطلب."
    else:
        body = "هل نعتمد عنوان التوصيل المحفوظ عندنا؟"

    return f"{prefix}{body}".strip() if prefix else body.strip()


def _salam_line(*, first_name: str = "") -> str:
    name = str(first_name or "").strip()
    if name:
        return f"وعليكم السلام ورحمة الله وبركاته يا {name}."
    return "وعليكم السلام ورحمة الله وبركاته."


def _build_no_saved_address_collect_reply(prefix: str = "") -> str:
    body = (
        "ما ظهر لي عنوان محفوظ الآن. "
        "أرسل لي المدينة والرمز المختصر أو رابط الموقع."
    )
    return f"{prefix}{body}".strip() if prefix else body.strip()


def _build_checkout_progress_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
    field_modes: Optional[Dict[str, str]] = None,
    known_previous: Optional[Dict[str, str]] = None,
    intro: str = "",
    include_order_summary: bool = True,
    address_on_file_claim: bool = False,
) -> str:
    known = dict(known_previous or {})
    has_saved = bool(
        known.get("city") or known.get("short_address") or known.get("maps_url")
    )
    if address_on_file_claim and not has_saved:
        return _build_no_saved_address_collect_reply(intro)

    summary = _order_summary(order_prep, brain_state) if include_order_summary else ""
    summary_prefix = f"{summary}\n\n" if summary else ""
    prefix = f"{intro}{summary_prefix}" if intro else summary_prefix

    return build_next_field_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
        field_modes=field_modes,
        known_previous=known_previous,
        prefix_override=prefix,
    )


def build_next_field_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
    field_modes: Optional[Dict[str, str]] = None,
    known_previous: Optional[Dict[str, str]] = None,
    prefix_override: Optional[str] = None,
) -> str:
    """Ask only for the next missing checkout field."""
    modes = dict(field_modes or {})
    nxt = next_missing_field(missing_fields)
    summary = _order_summary(order_prep, brain_state)
    if prefix_override is not None:
        prefix = prefix_override
    else:
        prefix = f"{summary}\n\n" if summary else ""

    if nxt in {"city", "delivery_address"} and modes.get(nxt) == MODE_CONFIRM:
        confirm = _build_saved_address_confirm_reply(
            prefix,
            known_previous=known_previous,
            order_prep=order_prep,
        )
        if confirm:
            return confirm

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


def build_greeting_checkout_resume_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
    field_modes: Optional[Dict[str, str]] = None,
    known_previous: Optional[Dict[str, str]] = None,
    first_name: str = "",
    address_on_file_claim: bool = False,
) -> str:
    intro = f"{_salam_line(first_name=first_name)}\nنكمل طلبك السابق.\n"
    return _build_checkout_progress_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
        field_modes=field_modes,
        known_previous=known_previous,
        intro=intro,
        include_order_summary=False,
        address_on_file_claim=address_on_file_claim,
    )


def build_address_on_file_collect_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
    field_modes: Optional[Dict[str, str]] = None,
    known_previous: Optional[Dict[str, str]] = None,
) -> str:
    return _build_checkout_progress_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
        field_modes=field_modes,
        known_previous=known_previous,
        address_on_file_claim=True,
    )


def build_greeting_with_pending_hint(
    *,
    has_pending: bool,
    first_name: str = "",
) -> str:
    name = str(first_name or "").strip()
    if name:
        base = f"وعليكم السلام ورحمة الله وبركاته يا {name}، كيف أقدر أخدمك؟"
    else:
        base = "وعليكم السلام ورحمة الله وبركاته 🙏\nكيف أساعدك؟"
    if not has_pending:
        return base
    return f"{base}\n\nعندك طلب سابق غير مكتمل — إذا حاب نكمله قل: كمل الطلب."


def build_resume_ack(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
    field_modes: Optional[Dict[str, str]] = None,
    known_previous: Optional[Dict[str, str]] = None,
) -> str:
    summary = _order_summary(order_prep, brain_state)
    nxt = build_next_field_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
        field_modes=field_modes,
        known_previous=known_previous,
    )
    if summary and summary in nxt:
        return nxt
    return f"تمام، نكمل طلبك.\n\n{nxt}".strip()


def build_catalog_order_start_reply(
    *,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    missing_fields: List[str],
    field_modes: Optional[Dict[str, str]] = None,
    known_previous: Optional[Dict[str, str]] = None,
) -> str:
    return build_next_field_reply(
        order_prep=order_prep,
        brain_state=brain_state,
        missing_fields=missing_fields,
        field_modes=field_modes,
        known_previous=known_previous,
    )


def build_catalog_order_extraction_fallback_reply(*, order_prep: Dict[str, Any]) -> str:
    """Explain an incomplete WhatsApp catalog payload without asking product/quantity."""
    details: List[str] = []
    skus = [str(s).strip() for s in (order_prep.get("catalog_skus") or []) if str(s).strip()]
    if skus:
        details.append("SKU: " + "، ".join(skus[:3]))
    qty = order_prep.get("catalog_total_quantity") or order_prep.get("quantity")
    if qty not in (None, "", 0):
        details.append(f"الكمية: {qty}")
    total = order_prep.get("order_flow_v2_catalog_total") or order_prep.get("order_total")
    if total not in (None, "", 0):
        details.append(f"الإجمالي: {total} SAR")
    visible = f"\nالظاهر عندي: {' | '.join(details)}" if details else ""
    return (
        "وصلني طلبك من كتالوج واتساب، لكن تفاصيل الأصناف لم تظهر كاملة عندي."
        f"{visible}\n"
        "أعد إرسال الطلب من الكتالوج أو أكّد الأصناف كما ظهرت عندك عشان أكمله."
    )
