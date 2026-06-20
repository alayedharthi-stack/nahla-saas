"""
core/order_payment_policy.py
────────────────────────────
Platform-wide payment method / status policy for Nahla orders.

Operational rules (PR-2 extension):
  * Bank transfer → ``payment_submitted`` until merchant confirms.
  * Provider-confirmed (e.g. Moyasar webhook) → may become ``paid``.
  * COD → ``cod_pending`` — never ``paid`` until collected.
  * No shipment/fulfillment without verified bank transfer or allowed COD.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ── Payment methods (extensible) ────────────────────────────────────────────
PAYMENT_METHOD_BANK_TRANSFER = "bank_transfer"
PAYMENT_METHOD_CASH_ON_DELIVERY = "cash_on_delivery"
PAYMENT_METHOD_MOYASAR = "moyasar"
PAYMENT_METHOD_MOYASAR_LINK = "moyasar_payment_link"
PAYMENT_METHOD_MOYASAR_WA = "moyasar_whatsapp_checkout"
PAYMENT_METHOD_CARD = "card"
PAYMENT_METHOD_APPLE_PAY = "apple_pay"
PAYMENT_METHOD_MADA = "mada"
PAYMENT_METHOD_STC_PAY = "stc_pay"
PAYMENT_METHOD_MANUAL = "manual_payment"

PAYMENT_METHOD_LABELS_AR: Dict[str, str] = {
    PAYMENT_METHOD_BANK_TRANSFER:     "تحويل بنكي",
    PAYMENT_METHOD_CASH_ON_DELIVERY:  "دفع عند الاستلام",
    PAYMENT_METHOD_MOYASAR:           "ميسر",
    PAYMENT_METHOD_MOYASAR_LINK:      "رابط دفع (ميسر)",
    PAYMENT_METHOD_MOYASAR_WA:        "دفع واتساب (ميسر)",
    PAYMENT_METHOD_CARD:              "بطاقة",
    PAYMENT_METHOD_APPLE_PAY:         "Apple Pay",
    PAYMENT_METHOD_MADA:              "مدى",
    PAYMENT_METHOD_STC_PAY:           "STC Pay",
    PAYMENT_METHOD_MANUAL:            "دفع يدوي",
}

# ── Payment status (distinct from order.status) ───────────────────────────────
PAYMENT_STATUS_PENDING = "pending"
PAYMENT_STATUS_PENDING_VERIFICATION = "pending_verification"
PAYMENT_STATUS_SUBMITTED = "submitted"
PAYMENT_STATUS_PAID = "paid"
PAYMENT_STATUS_COD_PENDING = "cod_pending"
PAYMENT_STATUS_FAILED = "failed"
PAYMENT_STATUS_REFUNDED = "refunded"

# ── Order statuses (future-ready) ───────────────────────────────────────────
ORDER_STATUS_PAYMENT_SUBMITTED = "payment_submitted"
ORDER_STATUS_COD_PENDING = "cod_pending"
ORDER_STATUS_READY_TO_PROCESS = "ready_to_process"
ORDER_STATUS_READY_TO_SHIP = "ready_to_ship"
ORDER_STATUS_SHIPMENT_CREATED = "shipment_created"
ORDER_STATUS_LABEL_GENERATED = "label_generated"

FULFILLMENT_ORDER_STATUSES = frozenset({
    ORDER_STATUS_READY_TO_PROCESS,
    ORDER_STATUS_READY_TO_SHIP,
    ORDER_STATUS_SHIPMENT_CREATED,
    ORDER_STATUS_LABEL_GENERATED,
    "processing",
    "shipped",
    "delivered",
})

BANK_TRANSFER_MERCHANT_ALERT = (
    "⚠️ يجب التحقق من وصول مبلغ التحويل البنكي فعلياً قبل تجهيز الطلب أو شحنه. "
    "لا تعتمد على الإيصال وحده، فقد يخطئ الذكاء الاصطناعي أو يرسل العميل إيصالاً غير مكتمل."
)

COD_MERCHANT_NOTICE = (
    "دفع عند الاستلام — الطلب غير مدفوع بعد. يمكن التجهيز بعد اكتمال العنوان والبيانات "
    "إذا كان خيار COD مفعّلاً للمتجر."
)


def _meta_bool(meta: Dict[str, Any], key: str) -> bool:
    return bool(meta.get(key))


def infer_payment_method(
    order_prep: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Best-effort payment method from prep or persisted metadata."""
    for container in (order_prep or {}, meta or {}):
        raw = str(container.get("payment_method") or "").strip().lower()
        if raw:
            return raw
        if container.get("payment_provider") == PAYMENT_METHOD_MOYASAR:
            return PAYMENT_METHOD_MOYASAR
    brain_pm = str((order_prep or {}).get("payment_method") or "").strip().lower()
    if brain_pm:
        return brain_pm
    if _meta_bool(order_prep or {}, "payment_receipt_received") or _meta_bool(
        order_prep or {}, "payment_submission_received"
    ):
        return PAYMENT_METHOD_BANK_TRANSFER
    return PAYMENT_METHOD_BANK_TRANSFER


def is_payment_explicitly_confirmed(
    order_prep: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """True only on explicit merchant/system confirmation."""
    for container in (order_prep or {}, meta or {}):
        if _meta_bool(container, "payment_confirmed"):
            return True
        if _meta_bool(container, "verified_by_staff"):
            return True
        if _meta_bool(container, "payment_verified"):
            return True
    return False


def is_provider_payment_confirmed(meta: Optional[Dict[str, Any]] = None) -> bool:
    """
    Trusted payment-provider confirmation (e.g. Moyasar webhook).

    Requires provider id + provider status paid + payment_confirmed flag.
    """
    m = meta or {}
    provider = str(m.get("payment_provider") or "").strip().lower()
    if not provider:
        return False
    provider_status = str(m.get("payment_provider_status") or "").strip().lower()
    if provider_status not in ("paid", "captured", "completed", "success"):
        return False
    return _meta_bool(m, "payment_confirmed")


def resolve_payment_status(
    *,
    order_status: str,
    payment_method: str,
    order_prep: Optional[Dict[str, Any]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """Derive ``payment_status`` for order metadata."""
    prep = order_prep or {}
    m = meta or {}
    if payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
        return PAYMENT_STATUS_COD_PENDING
    if is_provider_payment_confirmed(m) or is_payment_explicitly_confirmed(prep, m):
        return PAYMENT_STATUS_PAID
    norm = str(order_status or "").strip().lower()
    if norm == ORDER_STATUS_PAYMENT_SUBMITTED:
        return PAYMENT_STATUS_PENDING_VERIFICATION
    if prep.get("payment_submission_received") or prep.get("payment_receipt_received"):
        return PAYMENT_STATUS_PENDING_VERIFICATION
    if norm in ("pending_payment", "pending"):
        return PAYMENT_STATUS_PENDING
    if norm == "paid":
        return PAYMENT_STATUS_PAID
    return PAYMENT_STATUS_PENDING


def enrich_order_payment_metadata(
    base_meta: Dict[str, Any],
    *,
    order_prep: Dict[str, Any],
    target_status: str,
) -> Dict[str, Any]:
    """Merge normalized payment fields into order ``extra_metadata``."""
    payment_method = infer_payment_method(order_prep, base_meta)
    payment_status = resolve_payment_status(
        order_status=target_status,
        payment_method=payment_method,
        order_prep=order_prep,
        meta=base_meta,
    )
    confirmed = is_payment_explicitly_confirmed(order_prep, base_meta) or is_provider_payment_confirmed(
        base_meta
    )
    out = dict(base_meta)
    out["payment_method"] = payment_method
    out["payment_status"] = payment_status
    if confirmed or is_provider_payment_confirmed(out):
        out["payment_confirmed"] = True
        out["payment_verification_status"] = (
            "provider_confirmed" if is_provider_payment_confirmed(out) else "confirmed"
        )
    else:
        out["payment_confirmed"] = False
        prep_status = str(order_prep.get("payment_verification_status") or "").strip().lower()
        if prep_status == "pending_merchant_review":
            out["payment_verification_status"] = "pending_merchant_review"
        elif order_prep.get("payment_receipt_received") or order_prep.get(
            "payment_submission_received"
        ):
            out["payment_verification_status"] = prep_status or "pending"
    for flag_key in (
        "payment_receipt_received",
        "payment_receipt_parsed",
        "manual_verification_required",
        "shipping_blocked_reason",
    ):
        if flag_key in order_prep:
            out[flag_key] = order_prep[flag_key]
    receipt_meta = order_prep.get("payment_receipt_metadata")
    if isinstance(receipt_meta, dict):
        out["payment_receipt_metadata"] = dict(receipt_meta)
        parsed_fields = receipt_meta.get("parsed_receipt_fields")
        if isinstance(parsed_fields, dict):
            out["parsed_receipt_fields"] = dict(parsed_fields)
            out["receipt_data"] = parsed_fields.get("receipt_data") or receipt_meta.get(
                "receipt_data"
            )
    if payment_method == PAYMENT_METHOD_BANK_TRANSFER and not out.get("payment_provider"):
        out["payment_provider"] = None
    return out


def guard_wa_target_status(
    target_status: str,
    order_prep: Dict[str, Any],
    meta: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Block premature ``paid`` / fulfillment statuses for unverified bank transfer.
    """
    norm = str(target_status or "").strip().lower()
    payment_method = infer_payment_method(order_prep, meta)
    confirmed = is_payment_explicitly_confirmed(order_prep, meta) or is_provider_payment_confirmed(
        meta or {}
    )

    if payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
        if norm == "paid":
            return ORDER_STATUS_COD_PENDING
        return norm

    if payment_method == PAYMENT_METHOD_BANK_TRANSFER and not confirmed:
        if norm in FULFILLMENT_ORDER_STATUSES or norm in ("paid", "processing"):
            if order_prep.get("payment_submission_received") or order_prep.get(
                "payment_receipt_received"
            ):
                return ORDER_STATUS_PAYMENT_SUBMITTED
            return "pending_payment"

    if not confirmed and norm == "paid":
        if order_prep.get("payment_submission_received") or order_prep.get(
            "payment_receipt_received"
        ):
            return ORDER_STATUS_PAYMENT_SUBMITTED

    return norm


def can_create_shipment(
    *,
    order_status: str,
    meta: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    cod_enabled: bool = True,
) -> bool:
    """Shipment/label creation guard (future shipping PR)."""
    m = meta or {}
    prep = order_prep or {}
    payment_method = infer_payment_method(prep, m)
    confirmed = is_payment_explicitly_confirmed(prep, m) or is_provider_payment_confirmed(m)

    if payment_method == PAYMENT_METHOD_BANK_TRANSFER:
        return confirmed and str(order_status or "").lower() == "paid"

    if payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
        if not cod_enabled:
            return False
        return str(order_status or "").lower() in (
            ORDER_STATUS_COD_PENDING,
            ORDER_STATUS_READY_TO_PROCESS,
            ORDER_STATUS_READY_TO_SHIP,
        )

    if is_provider_payment_confirmed(m):
        return True

    return confirmed and str(order_status or "").lower() == "paid"


def build_merchant_payment_alert(
    *,
    raw_status: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """
    Return a merchant-facing alert dict for dashboard rendering, or ``None``.
    """
    m = meta or {}
    payment_method = infer_payment_method(None, m)
    norm_status = str(raw_status or "").strip().lower()
    confirmed = is_payment_explicitly_confirmed(None, m) or is_provider_payment_confirmed(m)

    if payment_method == PAYMENT_METHOD_BANK_TRANSFER:
        if norm_status == ORDER_STATUS_PAYMENT_SUBMITTED and not confirmed:
            return {
                "key":     "bank_transfer_verify_before_ship",
                "level":   "red",
                "label":   BANK_TRANSFER_MERCHANT_ALERT,
                "message": BANK_TRANSFER_MERCHANT_ALERT,
            }
        return None

    if payment_method == PAYMENT_METHOD_CASH_ON_DELIVERY:
        return {
            "key":     "cash_on_delivery",
            "level":   "blue",
            "label":   COD_MERCHANT_NOTICE,
            "message": COD_MERCHANT_NOTICE,
        }

    if is_provider_payment_confirmed(m):
        return None

    return None


def build_merchant_payment_alerts(
    *,
    raw_status: str,
    meta: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    alert = build_merchant_payment_alert(raw_status=raw_status, meta=meta)
    return [alert] if alert else []


__all__ = [
    "BANK_TRANSFER_MERCHANT_ALERT",
    "COD_MERCHANT_NOTICE",
    "FULFILLMENT_ORDER_STATUSES",
    "ORDER_STATUS_COD_PENDING",
    "ORDER_STATUS_PAYMENT_SUBMITTED",
    "PAYMENT_METHOD_BANK_TRANSFER",
    "PAYMENT_METHOD_CASH_ON_DELIVERY",
    "PAYMENT_METHOD_LABELS_AR",
    "PAYMENT_METHOD_MOYASAR",
    "PAYMENT_STATUS_COD_PENDING",
    "PAYMENT_STATUS_PAID",
    "PAYMENT_STATUS_PENDING_VERIFICATION",
    "build_merchant_payment_alert",
    "build_merchant_payment_alerts",
    "can_create_shipment",
    "enrich_order_payment_metadata",
    "guard_wa_target_status",
    "infer_payment_method",
    "is_payment_explicitly_confirmed",
    "is_provider_payment_confirmed",
    "resolve_payment_status",
]
