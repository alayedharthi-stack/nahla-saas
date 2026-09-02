"""
core/merchant_payment_methods.py
────────────────────────────────
Tenant-scoped payment method availability for WhatsApp checkout.

Reads existing settings only — no parallel config system:
  * ``TenantSettings.extra_metadata['payment_methods']`` — explicit flags
  * ``TenantSettings.ai_settings`` — COD legacy keys
  * ``extra_metadata['ai_sales_agent']`` — ``allow_cod_confirmation_flow``
  * ``extra_metadata['moyasar']`` — Moyasar gateway readiness
  * ``core.tenant_payment_accounts`` — bank KB evidence for default bank transfer
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.order_payment_policy import (
    PAYMENT_METHOD_BANK_TRANSFER,
    PAYMENT_METHOD_CASH_ON_DELIVERY,
    PAYMENT_METHOD_MANUAL,
    PAYMENT_METHOD_MOYASAR,
)

logger = logging.getLogger("nahla.merchant_payment_methods")

METHOD_LABELS_AR: Dict[str, str] = {
    PAYMENT_METHOD_BANK_TRANSFER:    "تحويل بنكي",
    PAYMENT_METHOD_CASH_ON_DELIVERY: "دفع عند الاستلام",
    PAYMENT_METHOD_MOYASAR:          "دفع إلكتروني",
    PAYMENT_METHOD_MANUAL:           "دفع يدوي",
}

_COD_TEXT_RE = re.compile(
    r"(?:دفع\s*عند\s*(?:ال)?استلام|الدفع\s*عند\s*(?:ال)?استلام|"
    r"كاش|نقد(?:ي)?|cod\b|cash\s*on\s*delivery)",
    re.I,
)
_BANK_TEXT_RE = re.compile(
    r"(?:تحويل\s*بنك|التحويل\s*البنكي|بنك(?:ي)?|bank\s*transfer|"
    r"ح(?:و|وّ)ل|آيبان|iban)",
    re.I,
)
_MOYASAR_TEXT_RE = re.compile(
    r"(?:ميسر|دفع\s*إلكترون|الدفع\s*الإلكترون|اون\s*لاين|أون\s*لاين|"
    r"online\s*pay|visa|مد(?:ى)?|بطاق(?:ة)?|moyasar)",
    re.I,
)


@dataclass(frozen=True)
class MerchantPaymentMethods:
    bank_transfer_enabled: bool
    cash_on_delivery_enabled: bool
    moyasar_enabled: bool
    moyasar_checkout_ready: bool
    manual_payment_enabled: bool
    available_methods: List[str] = field(default_factory=list)
    source: str = "tenant_settings"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bank_transfer_enabled":     self.bank_transfer_enabled,
            "cash_on_delivery_enabled":  self.cash_on_delivery_enabled,
            "moyasar_enabled":           self.moyasar_enabled,
            "moyasar_checkout_ready":    self.moyasar_checkout_ready,
            "manual_payment_enabled":    self.manual_payment_enabled,
            "available_methods":         list(self.available_methods),
            "source":                    self.source,
        }


def _meta_bool(container: Dict[str, Any], key: str) -> Optional[bool]:
    if key not in container:
        return None
    return bool(container.get(key))


def moyasar_checkout_ready(moyasar_cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = moyasar_cfg or {}
    return bool(cfg.get("enabled") and cfg.get("secret_key"))


def resolve_merchant_payment_methods(
    *,
    ai_settings: Optional[Dict[str, Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    moyasar_cfg: Optional[Dict[str, Any]] = None,
    has_bank_kb: bool = False,
) -> MerchantPaymentMethods:
    ai = dict(ai_settings or {})
    meta = dict(extra_metadata or {})
    pm_flags = dict(meta.get("payment_methods") or {})
    sales = dict(meta.get("ai_sales_agent") or {})
    moyasar = dict(moyasar_cfg or meta.get("moyasar") or {})

    bank_explicit = _meta_bool(pm_flags, "bank_transfer_enabled")
    if bank_explicit is not None:
        bank_enabled = bank_explicit
    elif _meta_bool(ai, "bank_transfer_enabled") is not None:
        bank_enabled = bool(ai.get("bank_transfer_enabled"))
    else:
        # Legacy default: enabled when merchant configured bank KB, else on.
        bank_enabled = bool(has_bank_kb) if has_bank_kb else True

    cod_explicit = _meta_bool(pm_flags, "cash_on_delivery_enabled")
    if cod_explicit is not None:
        cod_enabled = cod_explicit
    else:
        cod_enabled = ai.get("cash_on_delivery_enabled")
        if cod_enabled is None:
            cod_enabled = ai.get("cod_enabled")
        if cod_enabled is None:
            cod_enabled = sales.get("allow_cod_confirmation_flow")
        if cod_enabled is None:
            cod_enabled = ai.get("allow_cod_confirmation_flow")
        cod_enabled = bool(cod_enabled) if cod_enabled is not None else False

    moy_explicit = _meta_bool(pm_flags, "moyasar_enabled")
    moy_enabled = bool(moy_explicit) if moy_explicit is not None else bool(
        moyasar.get("enabled")
    )
    moy_ready = moyasar_checkout_ready(moyasar) if moy_enabled else False

    manual_explicit = _meta_bool(pm_flags, "manual_payment_enabled")
    manual_enabled = bool(manual_explicit) if manual_explicit is not None else False

    available: List[str] = []
    if moy_ready:
        available.append(PAYMENT_METHOD_MOYASAR)
    if bank_enabled:
        available.append(PAYMENT_METHOD_BANK_TRANSFER)
    if cod_enabled:
        available.append(PAYMENT_METHOD_CASH_ON_DELIVERY)
    if manual_enabled:
        available.append(PAYMENT_METHOD_MANUAL)

    return MerchantPaymentMethods(
        bank_transfer_enabled=bool(bank_enabled),
        cash_on_delivery_enabled=bool(cod_enabled),
        moyasar_enabled=bool(moy_enabled),
        moyasar_checkout_ready=bool(moy_ready),
        manual_payment_enabled=bool(manual_enabled),
        available_methods=available,
    )


def load_merchant_payment_methods(db: Any, tenant_id: int) -> MerchantPaymentMethods:
    """Load payment method flags for a tenant from persisted settings."""
    try:
        from core.tenant import get_or_create_settings  # noqa: PLC0415
        from core.billing import get_moyasar_settings  # noqa: PLC0415
        from core.tenant_payment_accounts import load_tenant_payment_accounts  # noqa: PLC0415

        settings = get_or_create_settings(db, tenant_id)
        ai = settings.ai_settings or {}
        meta = settings.extra_metadata or {}
        moy_cfg = get_moyasar_settings(db, tenant_id)
        accounts = load_tenant_payment_accounts(db, tenant_id=tenant_id)
        has_bank_kb = bool(accounts.has_accounts)
        return resolve_merchant_payment_methods(
            ai_settings=ai,
            extra_metadata=meta,
            moyasar_cfg=moy_cfg,
            has_bank_kb=has_bank_kb,
        )
    except Exception as exc:
        logger.warning(
            "[merchant_payment_methods] load failed tenant=%s err=%s — conservative defaults",
            tenant_id,
            exc,
        )
        return MerchantPaymentMethods(
            bank_transfer_enabled=True,
            cash_on_delivery_enabled=False,
            moyasar_enabled=False,
            moyasar_checkout_ready=False,
            manual_payment_enabled=False,
            available_methods=[PAYMENT_METHOD_BANK_TRANSFER],
            source="fallback",
        )


def format_method_labels_ar(methods: List[str]) -> List[str]:
    return [METHOD_LABELS_AR.get(m, m) for m in methods]


def build_payment_options_lines(methods: MerchantPaymentMethods) -> List[str]:
    labels = format_method_labels_ar(methods.available_methods)
    if not labels:
        return []
    lines = ["طريقة الدفع المتاحة:"]
    for idx, label in enumerate(labels, start=1):
        lines.append(f"{idx}. {label}")
    return lines


def build_payment_method_prompt_ar(methods: MerchantPaymentMethods) -> str:
    lines = build_payment_options_lines(methods)
    if not lines:
        return (
            "طرق الدفع غير مفعّلة حالياً في المتجر. "
            "سيتم تحويل طلبك لفريق المتجر لإكماله."
        )
    return "كيف تفضّل الدفع؟\n" + "\n".join(lines[1:]) + "\nاكتب اختيارك."


def parse_payment_method_from_text(text: str) -> Optional[str]:
    raw = str(text or "").strip()
    if not raw:
        return None
    if _COD_TEXT_RE.search(raw):
        return PAYMENT_METHOD_CASH_ON_DELIVERY
    if _BANK_TEXT_RE.search(raw):
        return PAYMENT_METHOD_BANK_TRANSFER
    if _MOYASAR_TEXT_RE.search(raw):
        return PAYMENT_METHOD_MOYASAR
    if re.search(r"^\s*1\s*$", raw) or raw in ("١", "الأول", "الاول"):
        return "__choice_index_1__"
    if re.search(r"^\s*2\s*$", raw) or raw in ("٢", "الثاني"):
        return "__choice_index_2__"
    if re.search(r"^\s*3\s*$", raw) or raw in ("٣", "الثالث"):
        return "__choice_index_3__"
    return None


def inbound_is_payment_method_choice(
    text: str,
    methods: MerchantPaymentMethods,
) -> Optional[str]:
    """Return a canonical method when the inbound is a method *choice*.

    Uses existing method labels / indexed choices. Does not treat
    completion claims such as ``تم التحويل`` as a method selection.
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 120:
        return None
    compact = re.sub(r"\s+", " ", raw).strip()
    for method in methods.available_methods:
        label = METHOD_LABELS_AR.get(method, method)
        if compact == method or compact == label:
            return method
        tokens = [tok for tok in str(label).split() if len(tok) >= 3]
        if compact in tokens:
            return method
    parsed = parse_payment_method_from_text(compact)
    if parsed in ("__choice_index_1__", "__choice_index_2__", "__choice_index_3__"):
        indexed = resolve_indexed_choice(compact, methods)
        if indexed and not str(indexed).startswith("__"):
            return indexed
    try:
        from core.payment_intent import detect_payment_confirmation_text  # noqa: PLC0415

        if detect_payment_confirmation_text(raw):
            return None
    except Exception:
        logger.exception(
            "[PAYMENT_METHODS] confirmation-vs-choice probe failed"
        )
    if parsed and not str(parsed).startswith("__") and parsed in methods.available_methods:
        return parsed
    return None


def resolve_indexed_choice(text: str, methods: MerchantPaymentMethods) -> Optional[str]:
    parsed = parse_payment_method_from_text(text)
    if parsed not in ("__choice_index_1__", "__choice_index_2__", "__choice_index_3__"):
        return parsed
    idx_map = {
        "__choice_index_1__": 0,
        "__choice_index_2__": 1,
        "__choice_index_3__": 2,
    }
    pos = idx_map.get(parsed or "")
    if pos is None or pos >= len(methods.available_methods):
        return None
    return methods.available_methods[pos]


def validate_payment_method_choice(
    method: str,
    methods: MerchantPaymentMethods,
) -> Optional[str]:
    """
    Return an Arabic rejection message when ``method`` is not allowed,
    or ``None`` when the choice is valid.
    """
    if method == PAYMENT_METHOD_CASH_ON_DELIVERY and not methods.cash_on_delivery_enabled:
        avail = format_method_labels_ar(methods.available_methods)
        suffix = f" المتاح الآن: {'، '.join(avail)}." if avail else ""
        return f"الدفع عند الاستلام غير متاح حالياً لهذا المتجر.{suffix}"

    if method == PAYMENT_METHOD_BANK_TRANSFER and not methods.bank_transfer_enabled:
        avail = format_method_labels_ar(methods.available_methods)
        suffix = f" المتاح الآن: {'، '.join(avail)}." if avail else ""
        return f"التحويل البنكي غير متاح حالياً لهذا المتجر.{suffix}"

    if method == PAYMENT_METHOD_MOYASAR:
        if not methods.moyasar_checkout_ready:
            avail = format_method_labels_ar(
                [m for m in methods.available_methods if m != PAYMENT_METHOD_MOYASAR]
            )
            suffix = f" المتاح الآن: {'، '.join(avail)}." if avail else ""
            return f"الدفع الإلكتروني غير متاح حالياً.{suffix}"

    if method not in methods.available_methods:
        avail = format_method_labels_ar(methods.available_methods)
        if not avail:
            return (
                "طرق الدفع غير مفعّلة حالياً في المتجر. "
                "سيتم تحويل طلبك لفريق المتجر لإكماله."
            )
        return f"هذه الطريقة غير متاحة. المتاح الآن: {'، '.join(avail)}."

    return None


def build_payment_method_state_patch(method: str) -> Dict[str, Any]:
    """Persist chosen payment method into order_prep."""
    from core.order_payment_policy import (  # noqa: PLC0415
        PAYMENT_STATUS_COD_PENDING,
        PAYMENT_STATUS_PENDING,
    )

    patch: Dict[str, Any] = {"payment_method": method}
    if method == PAYMENT_METHOD_CASH_ON_DELIVERY:
        patch.update({
            "payment_status":      PAYMENT_STATUS_COD_PENDING,
            "payment_confirmed":   False,
            "order_status":        "cod_pending",
            "awaiting_payment_receipt": False,
        })
    elif method == PAYMENT_METHOD_BANK_TRANSFER:
        patch.update({
            "payment_status":           PAYMENT_STATUS_PENDING,
            "payment_confirmed":        False,
            "order_status":             "pending_payment",
            "awaiting_payment_receipt": True,
        })
    elif method == PAYMENT_METHOD_MOYASAR:
        patch.update({
            "payment_provider":         PAYMENT_METHOD_MOYASAR,
            "payment_confirmed":        False,
            "order_status":             "pending_payment",
            "awaiting_payment_receipt": False,
        })
    return patch


__all__ = [
    "MerchantPaymentMethods",
    "METHOD_LABELS_AR",
    "build_payment_method_prompt_ar",
    "build_payment_method_state_patch",
    "build_payment_options_lines",
    "format_method_labels_ar",
    "load_merchant_payment_methods",
    "moyasar_checkout_ready",
    "inbound_is_payment_method_choice",
    "parse_payment_method_from_text",
    "resolve_indexed_choice",
    "resolve_merchant_payment_methods",
    "validate_payment_method_choice",
]
