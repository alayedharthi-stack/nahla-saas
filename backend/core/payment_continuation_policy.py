"""
payment_continuation_policy.py
──────────────────────────────
Deterministic payment continuation guidance from order evidence +
merchant payment capabilities.

Phase 1: read-only — no Moyasar link creation, no new sync.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from core.local_order_resolver import (
    LocalOrderSnapshot,
    _fetch_tenant_orders_for_customer,
    _is_paid_status,
    _match_explicit_order_number,
    _phone_lookup_keys,
    resolve_customer_order_context,
)
from core.merchant_payment_methods import (
    MerchantPaymentMethods,
    load_merchant_payment_methods,
)
from core.tenant_payment_accounts import load_tenant_payment_accounts

logger = logging.getLogger("nahla.payment_continuation_policy")

CASE_PAID = "paid"
CASE_PAYMENT_LINK = "payment_link"
CASE_BANK_TRANSFER = "bank_transfer"
CASE_COD = "cod"
CASE_NO_CAPABILITY = "no_capability"
CASE_DISAMBIGUATE = "disambiguate"
CASE_DEFER_WA_DRAFT = "defer_wa_draft"

_PAYMENT_PENDING_STATUSES = frozenset({
    "payment_pending",
    "pending_payment",
    "awaiting_payment",
    "unpaid",
})

_ORDER_REF_RE = re.compile(
    r"(?:رقم|طلب|order|#)\s*[:#]?\s*(\d{6,})",
    re.IGNORECASE,
)

_ADDRESS_FIELD_KEYS = frozenset({
    "city",
    "address",
    "address_line",
    "recipient_name",
    "district",
    "street",
    "short_address_code",
})


@dataclass(frozen=True)
class PaymentContinuationFacts:
    case: str
    order_ref: str
    order_id: int = 0
    order_source: str = ""
    order_status: str = ""
    payment_url: str = ""
    bank_instructions: str = ""
    cod_enabled: bool = False
    bank_transfer_enabled: bool = False
    has_verified_bank: bool = False


@dataclass(frozen=True)
class PaymentContinuationResult:
    handled: bool
    case: str = ""
    reply: str = ""
    payment_url: str = ""
    use_send_payment_link: bool = False
    defer_to_existing_flow: bool = False


def _is_payment_pending_status(status: str, *, source: str = "") -> bool:
    slug = str(status or "").strip().lower()
    if slug in _PAYMENT_PENDING_STATUSES:
        return True
    src = str(source or "").strip().lower()
    if src == "whatsapp" and slug in {"draft", "pending"}:
        return True
    return False


def _extract_order_number(
    *,
    message: str = "",
    intent_slots: Optional[Dict[str, Any]] = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    slots = dict(intent_slots or {})
    for key in ("order_number", "order_reference", "reference", "order_id"):
        raw = str(slots.get(key) or "").strip().lstrip("#")
        if raw:
            return raw

    try:
        from core.active_order_context import resolve_order_reference  # noqa: PLC0415

        ref, _mode = resolve_order_reference(
            commerce_bundle=commerce_bundle,
            state=None,
            history=history,
        )
        if ref:
            return str(ref).strip().lstrip("#")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order ref from history best-effort
        pass

    text = str(message or "")
    match = _ORDER_REF_RE.search(text)
    if match:
        return match.group(1)

    if history:
        for turn in reversed(history):
            body = str((turn or {}).get("body") or "")
            m = _ORDER_REF_RE.search(body)
            if m:
                return m.group(1)
    return ""


def _load_order_row(db: Any, tenant_id: int, order_id: int) -> Any:
    if db is None or not order_id:
        return None
    try:
        from models import Order  # noqa: PLC0415

        return (
            db.query(Order)
            .filter(Order.tenant_id == int(tenant_id), Order.id == int(order_id))
            .first()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[payment_continuation] order load failed: %s", exc)
        return None


def _verified_payment_url(db: Any, tenant_id: int, order_row: Any) -> str:
    if order_row is None:
        return ""
    url = str(getattr(order_row, "checkout_url", "") or "").strip()
    if url:
        return url
    meta = dict(getattr(order_row, "extra_metadata", None) or {})
    for key in (
        "checkout_url",
        "payment_link",
        "moyasar_checkout_url",
        "payment_url",
        "salla_payment_url",
    ):
        candidate = str(meta.get(key) or "").strip()
        if candidate:
            return candidate
    oid = int(getattr(order_row, "id", 0) or 0)
    if db is None or not oid:
        return ""
    try:
        from models import PaymentSession  # noqa: PLC0415

        session = (
            db.query(PaymentSession)
            .filter(
                PaymentSession.tenant_id == int(tenant_id),
                PaymentSession.order_id == oid,
                PaymentSession.payment_link.isnot(None),
            )
            .order_by(PaymentSession.id.desc())
            .first()
        )
        if session is not None:
            link = str(getattr(session, "payment_link", "") or "").strip()
            if link:
                return link
    except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — payment_session lookup best-effort
        logger.debug("[payment_continuation] payment_session lookup failed: %s", exc)
    return ""


def _pending_orders_for_customer(
    orders: Sequence[Any],
    phone: str,
) -> List[Any]:
    keys = _phone_lookup_keys(phone)
    pending: List[Any] = []
    for row in orders:
        if keys and not _order_matches_phone_row(row, keys):
            continue
        status = str(getattr(row, "status", "") or "")
        source = str(getattr(row, "source", "") or "")
        if _is_payment_pending_status(status, source=source):
            pending.append(row)
    return pending


def _order_matches_phone_row(order: Any, keys: set) -> bool:
    from core.local_order_resolver import _order_matches_phone  # noqa: PLC0415

    return _order_matches_phone(order, keys)


def _wa_draft_needs_address_before_payment(state: Any, order_row: Any) -> bool:
    if order_row is None:
        return False
    if str(getattr(order_row, "source", "") or "").strip().lower() != "whatsapp":
        return False
    prep = getattr(state, "order_prep", None)
    if prep is None:
        return False

    missing = getattr(prep, "missing_fields", None) or []
    if isinstance(missing, (list, tuple)):
        if any(str(f).strip().lower() in _ADDRESS_FIELD_KEYS for f in missing):
            return True

    stage = str(getattr(state, "stage", "") or "").strip().lower()
    if stage in {"ordering", "checkout", "deciding"}:
        city = str(getattr(prep, "city", "") or "").strip()
        address = str(
            getattr(prep, "address_line", "")
            or getattr(prep, "street", "")
            or ""
        ).strip()
        if not city and not address:
            return True
    return False


def _display_ref(snapshot: Optional[LocalOrderSnapshot]) -> str:
    if snapshot is None:
        return ""
    return str(snapshot.display_reference or "").strip()


def render_paid_reply(order_ref: str) -> str:
    ref = str(order_ref or "").strip()
    if ref:
        return f"طلبك رقم {ref} ظاهر عندنا مدفوع، ولا يحتاج دفع جديد."
    return "طلبك ظاهر عندنا مدفوع، ولا يحتاج دفع جديد."


def render_payment_link_reply(order_ref: str, url: str) -> str:
    ref = str(order_ref or "").strip()
    link = str(url or "").strip()
    if ref:
        return (
            f"طلبك رقم {ref} قيد إكمال الدفع. "
            f"تقدر تكمل الدفع من هذا الرابط: {link}"
        )
    return f"طلبك قيد إكمال الدفع. تقدر تكمل الدفع من هذا الرابط: {link}"


def render_cod_reply(order_ref: str) -> str:
    ref = str(order_ref or "").strip()
    if ref:
        return (
            f"طلبك رقم {ref} قيد إكمال الدفع. "
            "تقدر تختار الدفع عند الاستلام إذا كان متاحًا لهذا الطلب."
        )
    return (
        "طلبك قيد إكمال الدفع. "
        "تقدر تختار الدفع عند الاستلام إذا كان متاحًا لهذا الطلب."
    )


def render_no_capability_reply(order_ref: str) -> str:
    ref = str(order_ref or "").strip()
    if ref:
        return (
            f"طلبك رقم {ref} قيد إكمال الدفع. "
            "حالياً ما يظهر عندي رابط دفع جاهز للإرسال، "
            "ونقدر نكمل الدفع حسب طريقة الدفع المتاحة في المتجر."
        )
    return (
        "طلبك قيد إكمال الدفع. "
        "حالياً ما يظهر عندي رابط دفع جاهز للإرسال، "
        "ونقدر نكمل الدفع حسب طريقة الدفع المتاحة في المتجر."
    )


def render_disambiguate_reply() -> str:
    return "عندك أكثر من طلب يحتاج دفع. اكتب رقم الطلب الذي تريد إكمال دفعه."


def render_bank_transfer_reply(order_ref: str, bank_block: str) -> str:
    ref = str(order_ref or "").strip()
    block = str(bank_block or "").strip()
    prefix = (
        f"طلبك رقم {ref} قيد إكمال الدفع.\n"
        if ref
        else "طلبك قيد إكمال الدفع.\n"
    )
    return f"{prefix}{block}".strip()


def build_payment_continuation_facts(
    *,
    order_ref: str,
    order_row: Any,
    methods: MerchantPaymentMethods,
    accounts_has_verified: bool,
    payment_url: str,
    bank_block: str = "",
) -> PaymentContinuationFacts:
    status = str(getattr(order_row, "status", "") or "")
    source = str(getattr(order_row, "source", "") or "")
    oid = int(getattr(order_row, "id", 0) or 0)

    if _is_paid_status(status):
        case = CASE_PAID
    elif str(payment_url or "").strip():
        case = CASE_PAYMENT_LINK
    elif methods.bank_transfer_enabled and accounts_has_verified and bank_block.strip():
        case = CASE_BANK_TRANSFER
    elif methods.cash_on_delivery_enabled:
        case = CASE_COD
    else:
        case = CASE_NO_CAPABILITY

    return PaymentContinuationFacts(
        case=case,
        order_ref=order_ref,
        order_id=oid,
        order_source=source,
        order_status=status,
        payment_url=str(payment_url or "").strip(),
        bank_instructions=bank_block,
        cod_enabled=bool(methods.cash_on_delivery_enabled),
        bank_transfer_enabled=bool(methods.bank_transfer_enabled),
        has_verified_bank=bool(accounts_has_verified),
    )


def render_payment_continuation_reply(facts: PaymentContinuationFacts) -> str:
    case = str(facts.case or "").strip().lower()
    ref = str(facts.order_ref or "").strip()

    if case == CASE_PAID:
        return render_paid_reply(ref)
    if case == CASE_PAYMENT_LINK:
        return render_payment_link_reply(ref, facts.payment_url)
    if case == CASE_BANK_TRANSFER:
        return render_bank_transfer_reply(ref, facts.bank_instructions)
    if case == CASE_COD:
        return render_cod_reply(ref)
    if case == CASE_DISAMBIGUATE:
        return render_disambiguate_reply()
    return render_no_capability_reply(ref)


def evaluate_payment_continuation(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    message: str = "",
    state: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    intent_slots: Optional[Dict[str, Any]] = None,
) -> PaymentContinuationResult:
    """
    Evaluate whether ``pay_now`` can be answered deterministically.

    Returns ``handled=False`` when the existing WA draft / address flow
    should continue, or when no order context exists.
    """
    if db is None or not tenant_id:
        return PaymentContinuationResult(handled=False)

    resolved_phone = str(phone or "").strip()
    explicit_ref = _extract_order_number(
        message=message,
        intent_slots=intent_slots,
        commerce_bundle=commerce_bundle,
        history=history,
    )

    customer_rows = _fetch_tenant_orders_for_customer(
        db,
        tenant_id=int(tenant_id),
        phone=resolved_phone,
        customer_id=customer_id,
    )
    pending_rows = _pending_orders_for_customer(customer_rows, resolved_phone)

    if len(pending_rows) > 1 and not explicit_ref:
        return PaymentContinuationResult(
            handled=True,
            case=CASE_DISAMBIGUATE,
            reply=render_disambiguate_reply(),
        )

    ctx = resolve_customer_order_context(
        db,
        tenant_id=int(tenant_id),
        conversation_id=conversation_id,
        customer_id=customer_id,
        phone=resolved_phone,
        intent="pay_now",
        order_number=explicit_ref or None,
    )

    target_row: Any = None
    if explicit_ref:
        target_row = _match_explicit_order_number(customer_rows, explicit_ref)
    elif len(pending_rows) == 1:
        target_row = pending_rows[0]
    elif ctx.selected_order is not None:
        target_row = _load_order_row(db, int(tenant_id), ctx.selected_order.order_id)

    if target_row is None:
        return PaymentContinuationResult(handled=False)

    snapshot = ctx.selected_order
    if snapshot is None or int(getattr(target_row, "id", 0) or 0) != int(snapshot.order_id or 0):
        from core.local_order_resolver import _snapshot_from_order  # noqa: PLC0415

        snapshot = _snapshot_from_order(target_row)

    order_ref = _display_ref(snapshot) or explicit_ref
    status = str(getattr(target_row, "status", "") or "")
    source = str(getattr(target_row, "source", "") or "")

    if _wa_draft_needs_address_before_payment(state, target_row):
        return PaymentContinuationResult(
            handled=False,
            case=CASE_DEFER_WA_DRAFT,
            defer_to_existing_flow=True,
        )

    if _is_paid_status(status):
        return PaymentContinuationResult(
            handled=True,
            case=CASE_PAID,
            reply=render_paid_reply(order_ref),
        )

    if not _is_payment_pending_status(status, source=source):
        return PaymentContinuationResult(handled=False)

    methods = load_merchant_payment_methods(db, int(tenant_id))
    accounts = load_tenant_payment_accounts(db, tenant_id=int(tenant_id))
    has_verified_bank = bool(accounts.has_accounts)

    payment_url = _verified_payment_url(db, int(tenant_id), target_row)
    bank_block = ""
    if methods.bank_transfer_enabled and has_verified_bank:
        try:
            from modules.ai.brain.postprocess.payment_credential_guard import (  # noqa: PLC0415
                compose_verified_bank_transfer_block,
            )

            bank_block = compose_verified_bank_transfer_block(
                db,
                tenant_id=int(tenant_id),
            )
            if bank_block and "غير مفعّلة" in bank_block:
                bank_block = ""
        except Exception as exc:  # noqa: BLE001
            logger.debug("[payment_continuation] bank block compose failed: %s", exc)
            bank_block = ""

    facts = build_payment_continuation_facts(
        order_ref=order_ref,
        order_row=target_row,
        methods=methods,
        accounts_has_verified=has_verified_bank and bool(bank_block.strip()),
        payment_url=payment_url,
        bank_block=bank_block,
    )

    if facts.case == CASE_PAYMENT_LINK:
        return PaymentContinuationResult(
            handled=True,
            case=CASE_PAYMENT_LINK,
            reply=render_payment_link_reply(order_ref, payment_url),
            payment_url=payment_url,
            use_send_payment_link=True,
        )

    return PaymentContinuationResult(
        handled=True,
        case=facts.case,
        reply=render_payment_continuation_reply(facts),
    )


def resolve_payment_continuation_reply(
    db: Any,
    *,
    tenant_id: int,
    conversation_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    phone: Optional[str] = None,
    message: str = "",
    state: Any = None,
    history: Optional[List[Dict[str, Any]]] = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    intent_slots: Optional[Dict[str, Any]] = None,
) -> str:
    result = evaluate_payment_continuation(
        db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        phone=phone,
        message=message,
        state=state,
        history=history,
        commerce_bundle=commerce_bundle,
        intent_slots=intent_slots,
    )
    return str(result.reply or "").strip()


__all__ = [
    "CASE_BANK_TRANSFER",
    "CASE_COD",
    "CASE_DEFER_WA_DRAFT",
    "CASE_DISAMBIGUATE",
    "CASE_NO_CAPABILITY",
    "CASE_PAID",
    "CASE_PAYMENT_LINK",
    "PaymentContinuationFacts",
    "PaymentContinuationResult",
    "build_payment_continuation_facts",
    "evaluate_payment_continuation",
    "render_payment_continuation_reply",
    "resolve_payment_continuation_reply",
]
