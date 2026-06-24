"""OrderFlowV2 payment helpers."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from core.merchant_payment_methods import (
    MerchantPaymentMethods,
    build_payment_method_state_patch,
    load_merchant_payment_methods,
    resolve_indexed_choice,
)
from core.tenant_payment_accounts import load_tenant_payment_accounts

from .contract import build_contract

_BANK_RE = re.compile(
    r"(?:تحويل|تحويل\s*بنك|التحويل\s*البنكي|بنك(?:ي)?|bank\s*transfer|ح(?:و|وّ)ل|آيبان|iban)",
    re.I,
)
_COD_RE = re.compile(
    r"(?:دفع\s*عند\s*(?:ال)?استلام|الدفع\s*عند\s*(?:ال)?استلام|كاش|cod\b|2\b)",
    re.I,
)
_BANK_BRANDS = (
    ("alahli", re.compile(r"(?:الاهلي|الأهلي|اهلي|أهلي|alahli|al\s*ahli|ncb|snb)", re.I)),
    ("rajhi", re.compile(r"(?:الراجحي|راجحي|rajhi|al\s*rajhi)", re.I)),
    ("alinma", re.compile(r"(?:الانماء|الإنماء|alinma)", re.I)),
    ("albilad", re.compile(r"(?:البلاد|albilad)", re.I)),
)


def requested_bank_brand(message: str) -> str:
    text = str(message or "")
    for brand, pattern in _BANK_BRANDS:
        if pattern.search(text):
            return brand
    return ""


def _canonical_bank_brand(value: str) -> str:
    text = str(value or "").strip().lower()
    for brand, pattern in _BANK_BRANDS:
        if pattern.search(text):
            return brand
    return text


def parse_payment_method_choice(message: str, methods: MerchantPaymentMethods) -> Optional[str]:
    text = str(message or "").strip()
    if not text:
        return None
    indexed = resolve_indexed_choice(text, methods)
    if indexed and not indexed.startswith("__"):
        return indexed
    if _BANK_RE.search(text) and methods.bank_transfer_enabled:
        return "bank_transfer"
    if _COD_RE.search(text) and methods.cash_on_delivery_enabled:
        return "cash_on_delivery"
    return None


def _reject_patch(reason: str, methods: MerchantPaymentMethods, *, requested_bank: str = "") -> Dict[str, Any]:
    patch: Dict[str, Any] = {
        "order_flow_v2_payment_rejected": True,
        "order_flow_v2_payment_rejection_reason": reason,
        "order_flow_v2_available_payment_methods": list(methods.available_methods),
    }
    if requested_bank:
        patch["requested_bank"] = requested_bank
    patch.update(build_contract(
        decision="ask_missing_field",
        field="payment_method",
        reason=reason,
        facts={"available_methods": list(methods.available_methods), "requested_bank": requested_bank},
    ).to_patch())
    return patch


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
    if chosen not in methods.available_methods:
        return _reject_patch("payment_method_not_enabled", methods), None
    bank = requested_bank_brand(message)
    if chosen == "bank_transfer" and bank:
        accounts = load_tenant_payment_accounts(db, tenant_id=tenant_id)
        configured = {_canonical_bank_brand(x) for x in (accounts.bank_brands or ()) if x}
        if configured and bank not in configured:
            return _reject_patch("requested_bank_not_enabled", methods, requested_bank=bank), None
    patch = build_payment_method_state_patch(chosen)
    if bank:
        patch["requested_bank"] = bank
    return patch, chosen


def default_payment_method_patch(
    db: Any,
    *,
    tenant_id: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    methods = load_merchant_payment_methods(db, tenant_id)
    if len(methods.available_methods) != 1:
        return None, None
    chosen = methods.available_methods[0]
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
