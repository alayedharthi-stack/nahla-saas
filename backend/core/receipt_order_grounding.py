"""
receipt_order_grounding.py
──────────────────────────
Platform-wide evidence for linking payment receipts to confirmed orders.

A receipt proves transfer media + amount/bank/date only — never product,
quantity, or address requirement from stale browse focus alone.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.catalog_authoritative_line_items import (
    authoritative_line_items_from_prep,
    order_has_authoritative_products,
)
from core.wa_cart_line_items import ITEM_STATUS_CONFIRMED

logger = logging.getLogger("nahla.receipt_order_grounding")

_DIA = re.compile(r"[\u064B-\u065F\u0670]")
_WS = re.compile(r"\s+")

RECEIPT_UNLINKED_ORDER_ACK_AR = (
    "وصل إيصال التحويل، الله يعافيك.\n"
    "أحتاج أتأكد من الطلب المرتبط بهذا التحويل."
)

RECEIPT_AMOUNT_MISMATCH_ACK_AR = (
    "وصل إيصال التحويل، الله يعافيك.\n"
    "المبلغ يحتاج مراجعة من التاجر قبل تأكيد الطلب."
)

_REMAINING_PAYMENT_RE = re.compile(
    r"(?:"
    r"هذا\s*الباقي|"
    r"باقي\s*المبلغ|"
    r"حولت\s*الباقي|"
    r"حول\s*الباقي|"
    r"الباقي\s*حولته|"
    r"باقي\s*التحويل"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _norm(text: str) -> str:
    t = _DIA.sub("", (text or "").strip().lower())
    return (
        t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ى", "ي").replace("ة", "ه")
    )


def _prep_dict(brain_state: Dict[str, Any]) -> Dict[str, Any]:
    op = brain_state.get("order_prep") or brain_state.get("order_preparation") or {}
    return dict(op) if isinstance(op, dict) else {}


def _brain_state_dict(state: Any) -> Dict[str, Any]:
    if state is None:
        return {}
    if isinstance(state, dict):
        bs = state.get("brain_state") if "order_prep" not in state else state
        return dict(bs) if isinstance(bs, dict) else {}
    try:
        if hasattr(state, "to_dict"):
            raw = state.to_dict() or {}
            return dict(raw) if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        logger.exception("[RECEIPT_GROUNDING] brain_state_to_dict_failed")
    out: Dict[str, Any] = {}
    op = getattr(state, "order_prep", None)
    if op is not None:
        if hasattr(op, "to_dict"):
            try:
                out["order_prep"] = dict(op.to_dict() or {})
            except Exception:  # noqa: BLE001
                out["order_prep"] = {}
        elif isinstance(op, dict):
            out["order_prep"] = dict(op)
    for key in ("draft_order_id", "active_order_id", "current_product_focus"):
        val = getattr(state, key, None)
        if val is not None:
            out[key] = val
    return out


def _item_quantity(item: Dict[str, Any]) -> Optional[float]:
    raw = item.get("quantity")
    if raw is None:
        return None
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def _confirmed_line_items(prep: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not order_has_authoritative_products(prep):
        return []
    items = authoritative_line_items_from_prep(prep)
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product_id = str(
            item.get("product_id") or item.get("external_id") or item.get("catalog_product_id") or ""
        ).strip()
        if not product_id:
            continue
        if _item_quantity(item) is None:
            continue
        status = str(item.get("match_status") or "").strip().lower()
        if status and status not in {ITEM_STATUS_CONFIRMED, "confirmed"}:
            if not (
                item.get("from_native_catalog_order")
                or item.get("from_catalog_order")
                or prep.get("catalog_line_items_authoritative")
            ):
                continue
        out.append(dict(item))
    return out


def _format_product_label(line_items: Sequence[Dict[str, Any]]) -> str:
    names: List[str] = []
    for item in line_items:
        name = str(
            item.get("product_name") or item.get("title") or item.get("name") or ""
        ).strip()
        qty = _item_quantity(item)
        if not name:
            continue
        if qty is not None and abs(qty - 1.0) > 0.001:
            label = f"{name} × {int(qty) if qty.is_integer() else qty}"
        else:
            label = name
        if label not in names:
            names.append(label)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return " + ".join(names[:5])


def _resolve_expected_total(prep: Dict[str, Any], line_items: Sequence[Dict[str, Any]]) -> Optional[float]:
    for key in (
        "catalog_checkout_total",
        "order_total",
        "order_flow_v2_catalog_total",
    ):
        raw = prep.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
    if line_items:
        try:
            from core.wa_cart_line_items import cart_total_amount  # noqa: PLC0415

            total = cart_total_amount(list(line_items))
            if total is not None:
                return float(total)
        except Exception:  # noqa: BLE001
            logger.exception("[RECEIPT_GROUNDING] evidence_parse_failed")
    return None


def _parse_receipt_amount(inbound_metadata: Optional[Dict[str, Any]]) -> Optional[float]:
    md = inbound_metadata or {}
    for key in ("confirmed_payment_amount", "amount", "parsed_amount"):
        raw = md.get(key)
        if raw is not None:
            try:
                return float(str(raw).replace(",", "").strip())
            except (TypeError, ValueError):
                pass
    try:
        from core.payment_receipt_submission import parse_inbound_receipt  # noqa: PLC0415

        parsed = parse_inbound_receipt(md)
        amt = (parsed.fields or {}).get("amount")
        if amt is not None:
            return float(str(amt).replace(",", "").strip())
    except Exception:  # noqa: BLE001
        logger.exception("[RECEIPT_GROUNDING] receipt_amount_parse_failed")
    return None


def _address_fields_missing(prep: Dict[str, Any]) -> bool:
    if str(prep.get("short_address_code") or prep.get("google_maps_url") or "").strip():
        return False
    if str(prep.get("delivery_address_url") or prep.get("address_line") or "").strip():
        return False
    missing = {str(x).strip().lower() for x in (prep.get("missing_fields") or [])}
    if missing & {"delivery_address", "address", "address_line", "location", "city"}:
        return True
    if not str(prep.get("city") or "").strip():
        return True
    return not bool(
        str(prep.get("short_address_code") or "").strip()
        or str(prep.get("google_maps_url") or "").strip()
    )


def _has_order_ownership(brain_state: Dict[str, Any], prep: Dict[str, Any]) -> bool:
    if str(brain_state.get("draft_order_id") or "").strip():
        return True
    if str(brain_state.get("active_order_id") or "").strip():
        return True
    if prep.get("catalog_line_items_authoritative"):
        return True
    try:
        from modules.ai.brain.commerce.catalog_order_checkout import (  # noqa: PLC0415
            is_catalog_line_items_authoritative_from_prep,
        )

        if is_catalog_line_items_authoritative_from_prep(prep):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[RECEIPT_GROUNDING] catalog_authority_probe_failed")
    status = str(prep.get("order_status") or "").strip().lower()
    if status in {
        "awaiting_payment",
        "awaiting_receipt",
        "awaiting_payment_receipt",
        "pending_payment",
        "payment_pending",
    } and prep.get("awaiting_payment_receipt"):
        return True
    return False


@dataclass
class ReceiptOrderEvidence:
    has_confirmed_order: bool = False
    can_mention_product: bool = False
    can_request_address: bool = False
    line_items: List[Dict[str, Any]] = field(default_factory=list)
    selected_product_label: str = ""
    expected_total: Optional[float] = None
    receipt_amount: Optional[float] = None
    amount_mismatch: bool = False
    address_missing: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def evaluate_receipt_order_grounding(
    brain_state: Dict[str, Any],
    *,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> ReceiptOrderEvidence:
    """Return structured evidence — ``current_product_focus`` alone is never enough."""
    bs = dict(brain_state or {})
    prep = _prep_dict(bs)
    line_items = _confirmed_line_items(prep)
    has_ownership = _has_order_ownership(bs, prep)
    has_confirmed = bool(line_items) and has_ownership

    label = _format_product_label(line_items) if has_confirmed else ""
    expected = _resolve_expected_total(prep, line_items) if has_confirmed else None
    receipt_amount = _parse_receipt_amount(inbound_metadata)
    amount_mismatch = False
    if has_confirmed and expected is not None and receipt_amount is not None:
        amount_mismatch = abs(expected - receipt_amount) > 0.01

    address_missing = _address_fields_missing(prep) if has_confirmed else False
    can_mention = has_confirmed and bool(label) and not amount_mismatch
    can_address = (
        has_confirmed
        and address_missing
        and not amount_mismatch
        and can_mention
    )

    reason = "no_confirmed_order"
    if has_confirmed and amount_mismatch:
        reason = "amount_mismatch"
    elif has_confirmed and can_address:
        reason = "confirmed_order_address_missing"
    elif has_confirmed:
        reason = "confirmed_order"
    elif bool(bs.get("current_product_focus")) and not line_items:
        reason = "stale_product_focus_only"

    return ReceiptOrderEvidence(
        has_confirmed_order=has_confirmed,
        can_mention_product=can_mention,
        can_request_address=can_address,
        line_items=line_items,
        selected_product_label=label,
        expected_total=expected,
        receipt_amount=receipt_amount,
        amount_mismatch=amount_mismatch,
        address_missing=address_missing,
        reason=reason,
    )


def evaluate_receipt_order_grounding_from_state(state: Any) -> ReceiptOrderEvidence:
    return evaluate_receipt_order_grounding(_brain_state_dict(state))


def apply_receipt_grounding_to_summary(
    summary: Dict[str, Any],
    evidence: ReceiptOrderEvidence,
) -> Dict[str, Any]:
    """Mask ungrounded product/price fields for receipt replies."""
    out = dict(summary or {})
    out["receipt_order_evidence"] = evidence.to_dict()
    out["can_mention_receipt_product"] = evidence.can_mention_product
    out["can_request_receipt_address"] = evidence.can_request_address
    out["receipt_amount_mismatch"] = evidence.amount_mismatch
    if evidence.can_mention_product:
        out["selected_product"] = evidence.selected_product_label
        out["price"] = evidence.expected_total
    else:
        out["selected_product"] = ""
        out["selected_product_id"] = ""
        out["price"] = None
    return out


def _format_price(value: Any, currency: str = "SAR") -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    amount_str = str(int(amount)) if amount.is_integer() else f"{amount:.2f}".rstrip("0").rstrip(".")
    symbol = {"SAR": "ر.س", "USD": "$", "AED": "د.إ"}.get((currency or "").upper(), currency)
    return f"{amount_str} {symbol}".strip()


def _missing_shipping_fields(summary: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    if not str(summary.get("customer_first_name") or "").strip():
        missing.append("first_name")
    if not str(summary.get("customer_last_name") or "").strip():
        missing.append("last_name")
    if not str(summary.get("city") or "").strip():
        missing.append("city")
    has_location = bool(
        str(summary.get("short_address_code") or "").strip()
        or str(summary.get("google_maps_url") or "").strip()
    )
    if not has_location:
        missing.append("location")
    return missing


def _compose_address_interview(missing: List[str]) -> str:
    if not missing:
        return ""
    lines = ["عشان نوصّل طلبك بأسرع وقت، أرسل لنا:"]
    if "first_name" in missing or "last_name" in missing:
        lines.append("• الاسم الأول والأخير")
    if "city" in missing:
        lines.append("• مدينة التوصيل")
    if "location" in missing:
        lines.append("• الموقع: رابط قوقل ماب أو العنوان الوطني (٤ أحرف + ٤ أرقام)")
    return "\n".join(lines)


def compose_grounded_receipt_ack(summary: Dict[str, Any]) -> str:
    """Deterministic receipt ack respecting order evidence."""
    if summary.get("receipt_amount_mismatch"):
        return RECEIPT_AMOUNT_MISMATCH_ACK_AR
    if not summary.get("can_mention_receipt_product"):
        return RECEIPT_UNLINKED_ORDER_ACK_AR

    lines: List[str] = [
        "وصلنا إيصال التحويل، شكراً لك 🌷",
        "تم استلام الطلب وسيتم مراجعته وتجهيزه بإذن الله.",
    ]
    detail_bits: List[str] = []
    prod = str(summary.get("selected_product") or "").strip()
    if prod:
        price_str = _format_price(summary.get("price"), summary.get("currency") or "SAR")
        if price_str:
            detail_bits.append(f"الطلب: {prod} ({price_str})")
        else:
            detail_bits.append(f"الطلب: {prod}")
    if summary.get("short_address_code"):
        detail_bits.append(f"العنوان الوطني: {summary['short_address_code']}")
    elif summary.get("city"):
        detail_bits.append(f"المدينة: {summary['city']}")
    if detail_bits:
        lines.append("")
        lines.extend(detail_bits)

    if summary.get("can_request_receipt_address"):
        interview = _compose_address_interview(_missing_shipping_fields(summary))
        if interview:
            lines.append("")
            lines.append(interview)
    return "\n".join(lines)


def receipt_payment_context_active(
    summary: Optional[Dict[str, Any]],
    *,
    brain_state: Optional[Dict[str, Any]] = None,
) -> bool:
    """Payment context for receipt routing — stale focus alone is insufficient."""
    s = summary or {}
    if bool(s.get("awaiting_payment_receipt")):
        return True
    if bool(s.get("payment_receipt_received")):
        return True
    if brain_state:
        ev = evaluate_receipt_order_grounding(brain_state)
        if ev.has_confirmed_order:
            return True
    ev_raw = s.get("receipt_order_evidence")
    if isinstance(ev_raw, dict) and ev_raw.get("has_confirmed_order"):
        return True
    status = str(s.get("order_status") or "").strip().lower()
    if status in {
        "awaiting_payment",
        "awaiting_payment_receipt",
        "awaiting_receipt",
        "payment_submitted",
        "under_review",
        "pending_review",
        "awaiting_address",
    }:
        return True
    if str(s.get("payment_method") or "").strip().lower() in {
        "bank_transfer",
        "transfer",
        "iban",
    }:
        return True
    return False


def is_remaining_payment_balance_message(message: str) -> bool:
    return bool(_REMAINING_PAYMENT_RE.search(_norm(message or "")))


__all__ = [
    "ReceiptOrderEvidence",
    "RECEIPT_AMOUNT_MISMATCH_ACK_AR",
    "RECEIPT_UNLINKED_ORDER_ACK_AR",
    "apply_receipt_grounding_to_summary",
    "compose_grounded_receipt_ack",
    "evaluate_receipt_order_grounding",
    "evaluate_receipt_order_grounding_from_state",
    "is_remaining_payment_balance_message",
    "receipt_payment_context_active",
]
