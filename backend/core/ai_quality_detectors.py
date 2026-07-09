"""Deterministic quality flags for Class 9 — observe only, never block."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

_CATALOG_ACTIONS = frozenset({
    "search_products",
    "narrow",
    "catalog_navigate",
    "propose_draft_order",
    "create_order",
})

_GREETING_INTENTS = frozenset({
    "greeting",
    "social_reply",
})

_ORDER_LOOKUP_INTENTS = frozenset({
    "track_order",
})

_ORDER_LOOKUP_ACTIONS = frozenset({
    "track_order",
})

_CHECKOUT_INTENTS = frozenset({
    "start_order",
    "pay_now",
    "payment_continuation_reply",
    "send_payment_link",
})

_PICKER_MARKERS = (
    "اختر رقم",
    "اختر من",
    "اختر رقم الخيار",
    "1-",
    "2-",
    "3-",
)

_CHECKOUT_PRESSURE_MARKERS = (
    "اسمك",
    "عنوانك",
    "المدينة",
    "طريقة الدفع",
    "رقم الجوال",
    "رقم جوال",
    "الجوال المستخدم",
    "التوصيلة",
)

_CREATE_ORDER_PRESSURE = (
    "هل تريد إنشاء طلب جديد",
    "إنشاء طلب جديد",
)

_GROUNDING_REWRITE_MARKERS = (
    "ما ظهر عندي سعر مؤكد",
    "ما ظهر عندي سعر",
)

_ACK_TEMPLATE_MARKERS = (
    "تمام وصلت رسالتك",
    "وصلت رسالتك",
    "حصل خطأ مؤقت",
    "حصل خلل تقني",
)

_AWB_RE = re.compile(
    r"\b(?:AWB|awb|رقم التتبع|رقم تتبع)\s*[:#]?\s*([A-Z0-9-]{8,})\b",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_FAKE_URL_MARKERS = (
    "example.com",
    "placeholder",
    "your-link",
    "pay.example",
)


@dataclass(frozen=True)
class QualityFlag:
    flag_id: str
    reason: str


@dataclass
class QualityDetectionResult:
    flags: List[QualityFlag] = field(default_factory=list)

    @property
    def flag_ids(self) -> List[str]:
        return [f.flag_id for f in self.flags]


def detect_quality_flags(
    *,
    inbound_text: str = "",
    reply_text: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    recent_outbound_bodies: Optional[Sequence[str]] = None,
) -> QualityDetectionResult:
    """Pure rule engine — no I/O, no side effects."""
    meta = dict(metadata or {})
    body = str(reply_text or "")
    inbound = str(inbound_text or "")
    flags: List[QualityFlag] = []

    intent = str(meta.get("intent") or "").strip().lower()
    action = str(meta.get("decision_action") or "").strip().lower()
    chosen_path = str(meta.get("chosen_path") or "").strip()
    question_kind = str(meta.get("question_kind") or "").strip().lower()
    price_source = str(meta.get("price_source") or "").strip().lower()
    surface = str(meta.get("surface") or "").strip().lower()
    catalog_ids = list(meta.get("catalog_product_ids") or [])
    guards = {str(g) for g in (meta.get("guards_triggered") or [])}

    is_price_question = (
        question_kind == "price"
        or intent == "ask_price"
        or "سعر" in inbound
        or "ثمن" in inbound
    )
    is_greeting = intent in _GREETING_INTENTS or inbound.strip() in {
        "السلام عليكم",
        "مرحبا",
        "هلا",
        "صباح الخير",
    }
    is_order_lookup = (
        intent in _ORDER_LOOKUP_INTENTS
        or action in _ORDER_LOOKUP_ACTIONS
        or chosen_path.startswith("track_order")
        or "طلبي" in inbound
        or "رقم الطلب" in inbound
    )
    is_payment_link_ask = (
        "رابط الدفع" in inbound
        or "رابط دفع" in inbound
        or intent == "pay_now"
        or action == "send_payment_link"
    )
    is_tracking_ask = (
        "رقم التتبع" in inbound
        or "رابط التتبع" in inbound
        or "tracking" in inbound.lower()
    )

    if is_price_question and any(m in body for m in _PICKER_MARKERS):
        flags.append(QualityFlag(
            "price_question_with_picker",
            "price question received picker/list style reply",
        ))

    if is_greeting and (action in _CATALOG_ACTIONS or surface == "catalog_product_answer"):
        flags.append(QualityFlag(
            "greeting_with_catalog_action",
            f"greeting intent routed to action={action or surface}",
        ))

    if is_order_lookup and (
        surface == "catalog_product_answer"
        or action in _CATALOG_ACTIONS
        or any(m in body for m in _PICKER_MARKERS)
        or any(m in body for m in _CREATE_ORDER_PRESSURE)
    ):
        flags.append(QualityFlag(
            "order_lookup_with_catalog_action",
            "order status path showed catalog/picker/checkout pressure",
        ))

    if is_payment_link_ask:
        urls = _URL_RE.findall(body)
        if not urls:
            flags.append(QualityFlag(
                "payment_link_missing_or_fake",
                "payment link requested but reply has no URL",
            ))
        elif any(any(bad in u for bad in _FAKE_URL_MARKERS) for u in urls):
            flags.append(QualityFlag(
                "payment_link_missing_or_fake",
                "payment link reply contains placeholder/fake URL",
            ))

    if is_tracking_ask:
        has_awb = bool(_AWB_RE.search(body))
        shipment_evidence = bool(meta.get("shipment_evidence_ok"))
        blocked = list(meta.get("shipment_guard_blocked_claims") or [])
        if has_awb and not shipment_evidence and not blocked:
            flags.append(QualityFlag(
                "tracking_number_fake_or_missing",
                "tracking number present without shipment evidence",
            ))

    if (
        intent not in _CHECKOUT_INTENTS
        and action not in _CHECKOUT_INTENTS
        and not chosen_path.startswith("order_flow")
    ):
        if any(m in body for m in _CHECKOUT_PRESSURE_MARKERS):
            if inbound.strip() in {"شكراً", "شكرا", "مشكور", "تسلم"} or intent in {
                "social_reply",
                "greeting",
            }:
                flags.append(QualityFlag(
                    "checkout_pressure_without_order_intent",
                    "gratitude/social reply collected checkout fields",
                ))

    if (
        price_source == "catalog"
        and catalog_ids
        and any(m in body for m in _GROUNDING_REWRITE_MARKERS)
    ):
        flags.append(QualityFlag(
            "grounding_rewrite_after_grounded_price",
            "catalog-grounded price reply still contains grounding miss phrase",
        ))

    if (
        question_kind == "price"
        and price_source == "catalog"
        and "product_availability_truth_guard" in guards
    ):
        flags.append(QualityFlag(
            "availability_rewrite_after_catalog_price",
            "availability guard fired on catalog price answer",
        ))

    recent = [str(b or "").strip() for b in (recent_outbound_bodies or []) if str(b or "").strip()]
    for marker in _ACK_TEMPLATE_MARKERS:
        if marker in body and sum(1 for prev in recent if marker in prev) >= 1:
            flags.append(QualityFlag(
                "repeated_ack_template",
                f"repeated canned ack template: {marker}",
            ))
            break

    if not chosen_path and not action:
        flags.append(QualityFlag(
            "missing_metadata_for_quality",
            "outbound metadata missing chosen_path and decision_action",
        ))

    return QualityDetectionResult(flags=flags)


def quality_context_json(metadata: Dict[str, Any], flags: Sequence[QualityFlag]) -> str:
    """Compact JSON for ai_quality_events.mismatch_reason."""
    payload = {
        "quality_flags": [f.flag_id for f in flags],
        "reasons": {f.flag_id: f.reason for f in flags},
        "metadata_subset": {
            k: metadata.get(k)
            for k in (
                "chosen_path",
                "decision_action",
                "intent",
                "surface",
                "source",
                "topic",
                "question_kind",
                "price_source",
            )
            if metadata.get(k) is not None
        },
    }
    return json.dumps(payload, ensure_ascii=False)[:500]


__all__ = [
    "QualityDetectionResult",
    "QualityFlag",
    "detect_quality_flags",
    "quality_context_json",
]
