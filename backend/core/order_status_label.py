"""
core/order_status_label.py
──────────────────────────
Unified Arabic labels for customer-facing order status slugs.

Operational truth comes from persisted ``orders.status`` — labels describe
the slug honestly without inventing payment/shipment evidence.
"""
from __future__ import annotations

from typing import Optional

# Customer-facing order status labels (platform-wide, source-agnostic).
ORDER_STATUS_LABELS_AR: dict[str, str] = {
    "pending": "قيد الانتظار",
    "in_progress": "قيد التنفيذ",
    "under_review": "قيد المراجعة",
    "processing": "جاري المعالجة",
    "confirmed": "مؤكّد",
    "shipped": "تم الشحن",
    "on_the_way": "في الطريق",
    "out_for_delivery": "خارج للتوصيل",
    "delivered": "تم التسليم",
    "completed": "مكتمل",
    "complete": "مكتمل",
    "cancelled": "ملغي",
    "canceled": "ملغي",
    "refunded": "مُسترجع",
    "returned": "مُرتجع",
    "failed": "فشل",
    "cod": "دفع عند الاستلام",
    "payment_pending": "قيد إكمال الدفع",
    "pending_payment": "بانتظار الدفع",
    "awaiting_payment": "بانتظار الدفع",
    "unpaid": "بانتظار الدفع",
    "paid": "مدفوع",
    "abandoned": "طلب غير مكتمل",
    "draft": "قيد الإكمال",
    "creating": "قيد الإنشاء",
}


def _normalize_status_slug(status: str) -> str:
    return str(status or "").strip().lower().replace(" ", "_").replace("-", "_")


def order_status_label_ar(status: str, source: Optional[str] = None) -> str:
    """
    Return an Arabic customer-facing label for ``status``.

    ``source`` is reserved for future source-specific wording; today all
    commerce sources share the same slug → label map.
    """
    _ = source
    raw = str(status or "").strip()
    slug = _normalize_status_slug(raw)
    if not slug:
        return "حالة الطلب الحالية غير واضحة"
    mapped = ORDER_STATUS_LABELS_AR.get(slug)
    if mapped:
        return mapped
    return f"حالة الطلب الحالية: {raw}"


__all__ = [
    "ORDER_STATUS_LABELS_AR",
    "order_status_label_ar",
]
