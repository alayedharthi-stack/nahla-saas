"""
Active in-session order amount facts — not DB order tracking.

Operational only: resolves totals from catalog_order / order_prep / cart state.
Never emits customer-facing prose.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")

_CURRENT_AMOUNT_RE = re.compile(
    r"(?:"
    r"(?:كم|ما)\s*(?:ال)?(?:قيمة|مبلغ|سعر|اجمال|إجمال|إجمالي|مجموع|طلع|حساب|صار|صارلي|صار لي)"
    r"|"
    r"(?:عارف|تدري|تعرف|تعرفين|تعرفون).{0,24}(?:قيمة|مبلغ|سعر|اجمال|إجمال|مجموع|طلع)"
    r"|"
    r"(?:قيمة|مبلغ|سعر|اجمال|إجمال|مجموع)\s*(?:ال)?(?:طلب|طلبي|طلبيتي|الطلب)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_TRACKING_ONLY_RE = re.compile(
    r"(?:"
    r"وين\s*(?:طلبي|طلبيتي|الشحنة|الطلب)|"
    r"فين\s*(?:طلبي|طلبيتي|الشحنة|الطلب)|"
    r"متى\s*(?:يوصل|توصل|يجي|يصل)\s*(?:طلبي|طلبيتي|الطلب)|"
    r"حالة\s*(?:ال)?طلب|"
    r"رقم\s*(?:ال)?تتبع|"
    r"رابط\s*(?:ال)?تتبع|"
    r"تتبع\s*(?:ال)?طلب"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_ACTIVE_ORDER_STATUSES = frozenset({
    "awaiting_address",
    "awaiting_payment",
    "awaiting_receipt",
    "under_review",
})


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return re.sub(r"\s+", " ", s.lower()).strip()


def _prep_dict(prep: Any) -> Dict[str, Any]:
    if prep is None:
        return {}
    if isinstance(prep, dict):
        return dict(prep)
    if hasattr(prep, "to_dict"):
        try:
            return dict(prep.to_dict())
        except Exception:  # noqa: BLE001  # noqa: silent-ok — prep to_dict probe must not block amount facts
            pass
    return {}


def _line_items_from_state(state: Any) -> List[Dict[str, Any]]:
    if state is None:
        return []
    prep = _prep_dict(getattr(state, "order_prep", None))
    items = list(prep.get("line_items") or [])
    if items:
        return items
    cart = list(getattr(state, "cart_items", None) or [])
    if cart:
        return cart
    return []


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_line_items(line_items: List[Dict[str, Any]]) -> tuple[Optional[float], str]:
    total = 0.0
    found = False
    currency = ""
    for item in line_items:
        if not isinstance(item, dict):
            continue
        qty = int(float(item.get("quantity") or 1))
        price = _as_float(
            item.get("unit_price")
            or item.get("price")
            or item.get("catalog_price")
            or item.get("item_price")
        )
        if price is not None:
            total += price * max(1, qty)
            found = True
        cur = str(item.get("currency") or "").strip()
        if cur:
            currency = cur
    if not found:
        return None, currency
    return total, currency


def has_active_current_order(
    *,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when an in-session checkout/catalog cart exists (not necessarily in DB)."""
    if state is not None:
        prep = _prep_dict(getattr(state, "order_prep", None))
        if _line_items_from_state(state):
            return True
        if prep.get("order_flow_v2_active") or prep.get("order_flow_v2_pending"):
            return True
        if prep.get("product_id") and str(getattr(state, "stage", "") or "") == "ordering":
            return True
        if str(prep.get("order_status") or "") in _ACTIVE_ORDER_STATUSES:
            return True
        focus = getattr(state, "current_product_focus", None) or {}
        if isinstance(focus, dict) and (
            focus.get("from_catalog_order") or focus.get("from_native_catalog_order")
        ):
            return True
    meta = dict(inbound_metadata or {})
    if meta.get("source_type") == "catalog_order" and meta.get("product_items"):
        return True
    return False


def is_current_order_amount_question(message: str) -> bool:
    """True when the customer asks about the current cart/order total (not shipment tracking)."""
    raw = str(message or "").strip()
    if not raw:
        return False
    norm = _norm_ar(raw)
    if not norm:
        return False
    has_amount = bool(_CURRENT_AMOUNT_RE.search(norm))
    if not has_amount and "طلبي" in norm:
        has_amount = bool(re.search(r"كم|قيمة|مبلغ|اجمال|إجمال|مجموع|سعر|طلع|صار", norm))
    if not has_amount:
        return False
    if _TRACKING_ONLY_RE.search(norm) and not re.search(
        r"قيمة|مبلغ|اجمال|إجمال|مجموع|سعر|طلع|صار",
        norm,
    ):
        return False
    return True


@dataclass(frozen=True)
class CurrentOrderAmountSnapshot:
    has_active_current_order: bool
    line_items_count: int
    total_amount: Optional[float]
    currency: str
    source: str
    confidence: str
    reason: str


def resolve_current_order_amount(
    *,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> CurrentOrderAmountSnapshot:
    """Resolve current-session order amount facts without DB lookup."""
    active = has_active_current_order(state=state, inbound_metadata=inbound_metadata)
    prep = _prep_dict(getattr(state, "order_prep", None) if state is not None else None)
    line_items = _line_items_from_state(state) if state is not None else []
    meta = dict(inbound_metadata or {})

    total: Optional[float] = None
    currency = ""
    source = "none"
    confidence = "none"
    reason = "no_active_order"

    if not active:
        return CurrentOrderAmountSnapshot(
            has_active_current_order=False,
            line_items_count=len(line_items),
            total_amount=None,
            currency="",
            source=source,
            confidence=confidence,
            reason=reason,
        )

    reason = "active_checkout"
    for key, src in (
        ("order_flow_v2_catalog_total", "order_flow_v2_total"),
        ("order_total", "order_prep_total"),
        ("catalog_checkout_total", "catalog_checkout_total"),
    ):
        val = _as_float(prep.get(key))
        if val is not None and val > 0:
            total = val
            source = src
            confidence = "stored"
            currency = str(prep.get("order_flow_v2_currency") or prep.get("catalog_checkout_currency") or "")
            break

    if total is None:
        meta_total = _as_float(meta.get("total_price"))
        if meta_total is not None and meta_total > 0:
            total = meta_total
            source = "catalog_order_metadata"
            confidence = "stored"
            currency = str(meta.get("currency") or "")

    if total is None and line_items:
        computed, cur = _sum_line_items(line_items)
        if computed is not None:
            total = computed
            source = "order_prep_line_items"
            confidence = "computed"
            currency = cur
            reason = "computed_from_line_items"
        else:
            source = "order_prep_line_items"
            confidence = "partial"
            reason = "line_items_without_prices"

    if not currency:
        currency = str(meta.get("currency") or prep.get("order_flow_v2_currency") or "SAR")

    return CurrentOrderAmountSnapshot(
        has_active_current_order=True,
        line_items_count=len(line_items) or int(meta.get("item_count") or 0),
        total_amount=total,
        currency=currency,
        source=source,
        confidence=confidence,
        reason=reason,
    )


def current_order_amount_facts_dict(snapshot: CurrentOrderAmountSnapshot) -> Dict[str, Any]:
    data = asdict(snapshot)
    data["is_multi_item"] = int(data.get("line_items_count") or 0) > 1
    return data


def should_route_current_order_amount_over_tracking(
    message: str,
    *,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Prefer current-session amount path over DB track_order when both could match."""
    if not is_current_order_amount_question(message):
        return False
    snap = resolve_current_order_amount(state=state, inbound_metadata=inbound_metadata)
    return snap.has_active_current_order


_CURRENT_ORDER_INQUIRY_RE = re.compile(
    r"(?:"
    r"وش\s*(?:هو\s*)?(?:ال)?(?:طلب|طلبي|طلبيتي)"
    r"|(?:ا|أ)?(?:يش|يه)\s*(?:هو\s*)?(?:ال)?(?:طلب|طلبي|طلبيتي)"
    r"|(?:ماذا|ما)\s*(?:هو\s*)?(?:ال)?(?:طلب|طلبيتي)"
    r"|(?:و|ف)?ش\s*(?:ال)?(?:طلب|طلبي|طلبيتي)\s*(?:هو|؟|\?)?"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def is_current_order_inquiry(message: str) -> bool:
    """Customer asks what their current order contains — not shipment tracking."""
    raw = str(message or "").strip()
    if not raw:
        return False
    norm = _norm_ar(raw)
    if not norm:
        return False
    if _TRACKING_ONLY_RE.search(norm) and not _CURRENT_ORDER_INQUIRY_RE.search(norm):
        return False
    if not _CURRENT_ORDER_INQUIRY_RE.search(norm):
        return False
    if re.search(r"(?:وين|فين|متى|حالة|تتبع|tracking|where|when)", norm):
        if not re.search(r"وش\s*(?:هو\s*)?(?:ال)?(?:طلب|طلبي)|(?:ا|أ)?(?:يش|يه)\s*(?:ال)?(?:طلب|طلبي)", norm):
            return False
    return True


def build_current_order_inquiry_facts(
    *,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Facts for summarizing the in-session draft order — no customer-facing text."""
    snap = resolve_current_order_amount(state=state, inbound_metadata=inbound_metadata)
    line_items = _line_items_from_state(state) if state is not None else []
    titles: List[str] = []
    for item in line_items:
        if not isinstance(item, dict):
            continue
        title = str(
            item.get("product_name") or item.get("title") or item.get("name") or ""
        ).strip()
        if title:
            titles.append(title)
    prep = _prep_dict(getattr(state, "order_prep", None) if state is not None else None)
    missing = list(prep.get("missing_fields") or [])
    return {
        **current_order_amount_facts_dict(snap),
        "product_titles": titles,
        "missing_fields": missing,
        "order_status": str(prep.get("order_status") or ""),
        "is_draft_in_progress": True,
    }


def should_route_current_order_inquiry_over_tracking(
    message: str,
    *,
    state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """Route «وش هو طلبي؟» to draft summary, not DB track_order."""
    if not is_current_order_inquiry(message):
        return False
    return has_active_current_order(state=state, inbound_metadata=inbound_metadata)


__all__ = [
    "CurrentOrderAmountSnapshot",
    "build_current_order_inquiry_facts",
    "current_order_amount_facts_dict",
    "has_active_current_order",
    "is_current_order_amount_question",
    "is_current_order_inquiry",
    "resolve_current_order_amount",
    "should_route_current_order_amount_over_tracking",
    "should_route_current_order_inquiry_over_tracking",
]
