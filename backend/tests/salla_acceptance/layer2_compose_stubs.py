"""
Layer 2 compose stubs — CI-safe DefaultComposer._llm_compose replacements.

Reflect trusted facts from decision/result when present; never invent tracking,
ETA, staff replies, or warranty claims.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from modules.ai.brain.types import BrainContext, Decision


@dataclass
class Layer2ComposeCapture:
    call_count: int = 0
    last_snapshot: Dict[str, Any] = field(default_factory=dict)


COMPOSE_CAPTURE = Layer2ComposeCapture()


def _topic(decision: Optional[Decision]) -> str:
    if decision is None:
        return ""
    return str((decision.args or {}).get("topic") or "")


def _result_data(result: Any) -> Dict[str, Any]:
    data = getattr(result, "data", None)
    return dict(data) if isinstance(data, dict) else {}


def _first_product(data: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("catalog_fact_products", "products", "product"):
        raw = data.get(key)
        if isinstance(raw, list) and raw:
            first = raw[0]
            return dict(first) if isinstance(first, dict) else {}
        if isinstance(raw, dict):
            return dict(raw)
    return {}


def _product_title(product: Dict[str, Any]) -> str:
    return str(
        product.get("title")
        or product.get("product_name")
        or product.get("name")
        or ""
    ).strip()


def _product_price(product: Dict[str, Any], data: Dict[str, Any]) -> str:
    prices = list(data.get("catalog_fact_price_values") or [])
    if prices:
        return str(prices[0])
    for key in ("price", "catalog_price", "unit_price"):
        val = product.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _product_url(product: Dict[str, Any], data: Dict[str, Any]) -> str:
    for key in ("product_url", "url", "public_url"):
        val = product.get(key) or data.get(key)
        if val:
            return str(val)
    meta = product.get("extra_metadata") or data.get("extra_metadata") or {}
    if isinstance(meta, dict) and meta.get("product_url"):
        return str(meta["product_url"])
    return ""


def _knowledge_body(data: Dict[str, Any], args: Dict[str, Any]) -> str:
    for key in ("knowledge_body", "kb_body", "section_body", "body"):
        val = data.get(key) or args.get(key)
        if val:
            return str(val).strip()
    sections = data.get("knowledge_sections") or args.get("knowledge_sections") or []
    if isinstance(sections, list) and sections:
        first = sections[0]
        if isinstance(first, dict) and first.get("body"):
            return str(first["body"]).strip()
    return ""


def _tracking_number(data: Dict[str, Any], args: Dict[str, Any]) -> str:
    for src in (data, args):
        for key in ("tracking_number", "tracking", "tracking_id"):
            val = src.get(key)
            if val:
                return str(val).strip()
    shipment = data.get("shipment") or args.get("shipment") or {}
    if isinstance(shipment, dict):
        val = shipment.get("tracking_number") or shipment.get("tracking")
        if val:
            return str(val).strip()
    return ""


def _order_status(data: Dict[str, Any]) -> str:
    for key in ("status", "status_label_ar", "order_status"):
        val = data.get(key)
        if val:
            return str(val).strip()
    return ""


def _availability_phrase(product: Dict[str, Any], data: Dict[str, Any]) -> str:
    if data.get("in_stock") is False or product.get("in_stock") is False:
        return "غير متوفر حالياً"
    stock = product.get("stock_quantity")
    if stock is not None and int(stock or 0) <= 0:
        return "غير متوفر حالياً"
    if product or data.get("catalog_product_ids"):
        return "متوفر"
    return ""


def layer2_stub_llm_reply(
    ctx: BrainContext,
    *,
    result: Any = None,
    decision: Optional[Decision] = None,
) -> str:
    """Build a short Arabic reply from trusted compose context only."""
    message = (ctx.message or "").strip()
    topic = _topic(decision)
    args = dict(getattr(decision, "args", None) or {})
    data = _result_data(result)
    product = _first_product(data)
    title = _product_title(product)
    price = _product_price(product, data)
    url = _product_url(product, data)
    kb_body = _knowledge_body(data, args)
    tracking = _tracking_number(data, args)
    status = _order_status(data)
    question_kind = str(data.get("question_kind") or args.get("question_kind") or "")

    COMPOSE_CAPTURE.last_snapshot = {
        "message": message[:120],
        "topic": topic,
        "decision_action": str(getattr(decision, "action", "") or ""),
        "question_kind": question_kind,
        "catalog_product_ids": list(data.get("catalog_product_ids") or []),
        "price_source": data.get("price_source"),
        "knowledge_source": data.get("knowledge_source"),
        "product_title": title,
        "has_tracking": bool(tracking),
    }

    if topic in {"employee_handoff", "staff_contact", "human_handoff"} or "موظف" in message:
        return "تم تسجيل طلب التواصل مع فريق المتجر."

    if topic in {"track_order", "shipment_status", "order_status"} or "طلب" in message or "شحن" in message:
        if tracking:
            parts = [f"رقم التتبع: {tracking}"]
            if status:
                parts.insert(0, f"حالة الطلب: {status}")
            return " — ".join(parts)
        if status:
            return f"حالة الطلب: {status}"
        if "متى يوصل" in message or "متى يصل" in message:
            return "ما عندي وقت وصول مؤكد لهذا الطلب."
        if "وين طلبي" in message or "أين طلبي" in message:
            return "أتحقق من حالة طلبك من السجل المتاح."
        return "أحتاج رقم الطلب أو معلومات أكثر للمتابعة."

    if "كود خصم" in message or topic in {"coupon", "apply_coupon"}:
        if data.get("coupon_applied") or args.get("coupon_applied"):
            code = str(data.get("coupon_code") or args.get("coupon_code") or "")
            return f"تم تطبيق الكوبون {code}." if code else "تم تطبيق الكوبون."
        return "ما قدرت أطبق هذا الكود — تحقق منه أو جرّب كوداً آخر."

    if "شحن" in message or topic in {"shipping", "shipping_cost", "delivery_cost"}:
        if kb_body:
            return kb_body[:180]
        if "الرياض" in message or args.get("city") == "الرياض":
            return "الشحن للرياض 25 ريال."
        return "حدّد المدينة عشان أعطيك تكلفة الشحن."

    if "ضمان" in message or "warranty" in topic:
        if kb_body:
            return kb_body[:180]
        return "ما عندي معلومة مؤكدة عن هذا."

    if question_kind == "price" or "سعر" in message or "كم" in message:
        if title and price:
            return f"{title}: {price} ريال"
        if price:
            return f"السعر {price} ريال"
        if title:
            return f"المنتج {title} — ما عندي سعر مؤكد الآن."

    if "رابط" in message or "لينك" in message or topic in {"product_link", "catalog_link"}:
        if url:
            return f"رابط المنتج: {url}"
        if title:
            return f"ما عندي رابط مؤكد لـ {title}."

    avail = _availability_phrase(product, data)
    if "متوفر" in message or question_kind == "availability" or topic in {"availability", "stock"}:
        if title and avail:
            return f"{title} — {avail}"
        if avail:
            return avail

    if title:
        if price:
            return f"{title} — {price} ريال"
        return f"نعم، عندنا {title}."

    if topic:
        return f"بخصوص {topic} — كيف أقدر أساعدك؟"

    if message:
        return f"وصلت رسالتك: {message[:80]}"

    return "كيف أقدر أساعدك؟"


async def layer2_stub_llm_compose(
    _composer_self: Any,
    ctx: BrainContext,
    result: Any,
    *,
    decision: Optional[Decision] = None,
    **_kwargs: Any,
) -> str:
    COMPOSE_CAPTURE.call_count += 1
    return layer2_stub_llm_reply(ctx, result=result, decision=decision)


async def layer2_stub_legacy_llm_compose(
    _composer_self: Any,
    ctx: BrainContext,
    result: Any,
    *,
    decision: Optional[Decision] = None,
    **_kwargs: Any,
) -> str:
    COMPOSE_CAPTURE.call_count += 1
    return layer2_stub_llm_reply(ctx, result=result, decision=decision)


async def layer2_stub_extract_slots(_message: str, _history: Optional[list] = None) -> Dict[str, Any]:
    return {}


__all__ = [
    "COMPOSE_CAPTURE",
    "Layer2ComposeCapture",
    "layer2_stub_extract_slots",
    "layer2_stub_legacy_llm_compose",
    "layer2_stub_llm_compose",
    "layer2_stub_llm_reply",
]
