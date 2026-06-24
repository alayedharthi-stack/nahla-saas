"""OrderFlowV2 payment helpers."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from core.merchant_payment_methods import (
    MerchantPaymentMethods,
    build_payment_method_state_patch,
    load_merchant_payment_methods,
    validate_payment_method_choice,
)

_BANK_RE = re.compile(
    r"(?:تحويل\s*بنك|التحويل\s*البنكي|بنك(?:ي)?|bank\s*transfer|ح(?:و|وّ)ل|آيبان|iban|1\b)",
    re.I,
)
_COD_RE = re.compile(
    r"(?:دفع\s*عند\s*(?:ال)?استلام|الدفع\s*عند\s*(?:ال)?استلام|كاش|cod\b|2\b)",
    re.I,
)


def parse_payment_method_choice(message: str, methods: MerchantPaymentMethods) -> Optional[str]:
    text = str(message or "").strip()
    if not text:
        return None
    if _BANK_RE.search(text) and methods.bank_transfer_enabled:
        return "bank_transfer"
    if _COD_RE.search(text) and methods.cash_on_delivery_enabled:
        return "cash_on_delivery"
    if text in {"1", "٢", "2"}:
        if methods.bank_transfer_enabled:
            return "bank_transfer"
        if methods.cash_on_delivery_enabled:
            return "cash_on_delivery"
    return None


def apply_payment_method_selection(
    db: Any,
    *,
    tenant_id: int,
    message: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    methods = load_merchant_payment_methods(db, tenant_id)
    chosen = parse_payment_method_choice(message, methods)
    if not chosen:
        return None, None
    rejection = validate_payment_method_choice(chosen, methods)
    if rejection:
        return None, rejection
    return build_payment_method_state_patch(chosen), chosen


def build_payment_instruction_reply(
    db: Any,
    *,
    tenant_id: int,
    order_prep: Dict[str, Any],
    brain_state: Dict[str, Any],
    payment_method: str,
) -> str:
    from core.order_payment_policy import (  # noqa: PLC0415
        PAYMENT_METHOD_BANK_TRANSFER,
        PAYMENT_METHOD_CASH_ON_DELIVERY,
        PAYMENT_METHOD_MOYASAR,
    )
    from core.wa_checkout_reply import build_order_summary_lines  # noqa: PLC0415

    method = str(payment_method or "").strip().lower()
    summary_txt = "\n".join(
        build_order_summary_lines(order_prep, brain_state=brain_state)
    ).strip()
    prefix = f"{summary_txt}\n\n" if summary_txt else ""

    if method in {PAYMENT_METHOD_BANK_TRANSFER, "bank_transfer", "transfer"}:
        return (
            f"{prefix}تم اختيار التحويل البنكي.\n"
            "بعد التحويل، أرسل صورة الإيصال أو إثبات الدفع."
        ).strip()
    if method in {PAYMENT_METHOD_CASH_ON_DELIVERY, "cash_on_delivery", "cod"}:
        return f"{prefix}تم اختيار الدفع عند الاستلام.".strip()
    if method in {PAYMENT_METHOD_MOYASAR, "moyasar"}:
        return f"{prefix}تم اختيار الدفع الإلكتروني.\nسنرسل لك رابط الدفع قريباً.".strip()
    return f"{prefix}تم تسجيل طريقة الدفع.".strip()
