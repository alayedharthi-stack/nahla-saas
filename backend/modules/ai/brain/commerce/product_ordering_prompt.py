"""
commerce/product_ordering_prompt.py
───────────────────────────────────
Context-aware WhatsApp ordering prompts — replaces robotic catalog-menu copy.

Platform-wide: uses catalog evidence and conversation state, not merchant-specific
hardcoding. Honey-store examples in product docs are satisfied when the synced
catalog and customer message indicate a honey/category browse or order intent.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..types import BrainContext, INTENT_START_ORDER

# Legacy phrase — must never reach customers (enforced by tests).
LEGACY_ROBOTIC_PRODUCT_PROMPT = (
    "ما المنتج الذي تودّ طلبه؟ يمكنك ذكر الاسم أو قول «أكثر مبيعاً»."
)

_BEST_SELLER_RE = re.compile(
    r"(?:"
    r"الاكثر\s*مبيعا|اكثر\s*مبيعا|الاكثر\s*طلبا|اكثر\s*طلبا|"
    r"الأكثر\s*مبيعاً|أكثر\s*مبيعاً|best\s*sellers?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_HONEY_RE = re.compile(r"عسل", re.UNICODE)
_ORDER_INTENT_RE = re.compile(
    r"(?:"
    r"(?:ابي|ابغى|أبي|أبغى|اريد|أريد|بدي)\s*(?:اطلب|أطلب|اشتري|أشتري|order)"
    r"|(?:ابي|ابغى|أبي|أبغى)\s+عسل"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_BROWSE_INVENTORY_RE = re.compile(
    r"(?:"
    r"وش\s*عندكم|ايش\s*عندكم|ايه\s*عندكم|ما\s*المتوفر|وش\s*المتوفر|"
    r"ايش\s*المتوفر|ما\s*المنتجات|وش\s*الانواع|ايش\s*الانواع"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def _join_names(names: List[str], *, limit: int = 3) -> str:
    items = [n for n in names if n][:limit]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} و{items[1]}"
    return f"{items[0]} و{items[1]} و{items[2]}"


def _catalog_titles(ctx: BrainContext) -> List[str]:
    state = getattr(ctx, "state", None)
    facts = getattr(ctx, "facts", None)
    seen: List[str] = []
    for source in (
        list(getattr(state, "last_search_candidates", None) or []),
        list(getattr(state, "last_recommended_products", None) or []),
        list(getattr(facts, "top_products", None) or []),
    ):
        for prod in source:
            title = str((prod or {}).get("title") or "").strip()
            if title and title not in seen:
                seen.append(title)
    return seen[:6]


def _variant_labels(ctx: BrainContext) -> List[str]:
    op = getattr(getattr(ctx, "state", None), "order_prep", None)
    if op is None:
        return []
    labels: List[str] = []
    for row in list(getattr(op, "product_variants_raw", None) or []):
        name = str((row or {}).get("name") or (row or {}).get("title") or "").strip()
        if name and name not in labels:
            labels.append(name)
    for group in list(getattr(op, "product_options_meta", None) or []):
        for val in list((group or {}).get("values") or []):
            name = str((val or {}).get("name") or "").strip()
            if name and name not in labels:
                labels.append(name)
    return labels[:4]


def _resolved_product_title(ctx: BrainContext) -> str:
    state = getattr(ctx, "state", None)
    focus = dict(getattr(state, "current_product_focus", None) or {})
    if focus.get("title"):
        return str(focus["title"]).strip()
    op = getattr(state, "order_prep", None)
    pid = str(getattr(op, "product_id", "") or "").strip()
    if pid:
        for title in _catalog_titles(ctx):
            ext = str(
                next(
                    (
                        str((p or {}).get("external_id") or "")
                        for p in (
                            list(getattr(state, "last_search_candidates", None) or [])
                            + list(getattr(state, "last_recommended_products", None) or [])
                        )
                        if str((p or {}).get("title") or "").strip() == title
                    ),
                    "",
                )
            ).strip()
            if ext == pid:
                return title
        return pid
    return ""


def _next_missing_order_field(ctx: BrainContext) -> Optional[str]:
    """When product is known, return the next slot to ask — not product name."""
    state = getattr(ctx, "state", None)
    op = getattr(state, "order_prep", None)
    title = _resolved_product_title(ctx)

    if not title and not getattr(state, "current_product_focus", None):
        return None

    if op and getattr(op, "awaiting_variant_choice", False):
        return "variant"
    if op and list(getattr(op, "product_variants_raw", None) or []):
        selected = dict(getattr(op, "product_options", None) or {})
        if not selected:
            return "variant"
    if op and int(getattr(op, "quantity", 0) or 0) <= 1:
        slots = dict(getattr(getattr(ctx, "intent", None), "slots", None) or {})
        if not slots.get("quantity"):
            return "quantity"
    missing = list(getattr(op, "missing_fields", None) or []) if op else []
    if missing:
        field = str(missing[0]).strip().lower()
        if field in {"address", "address_location", "address_line", "city",
                     "short_address_code", "google_maps_url"}:
            return "address"
        if field in {"payment", "payment_method"}:
            return "payment"
    return None


def _is_honey_context(ctx: BrainContext, message: str) -> bool:
    msg = message or ""
    if _HONEY_RE.search(msg):
        return True
    slots = dict(getattr(getattr(ctx, "intent", None), "slots", None) or {})
    slot_q = str(slots.get("product_query") or slots.get("product_name") or "")
    if _HONEY_RE.search(slot_q):
        return True
    for title in _catalog_titles(ctx):
        if _HONEY_RE.search(title):
            return True
    return False


def _is_browse_inventory(message: str) -> bool:
    return bool(_BROWSE_INVENTORY_RE.search(message or ""))


def _is_best_seller_request(message: str) -> bool:
    return bool(_BEST_SELLER_RE.search(message or ""))


def _is_order_intent_without_product(ctx: BrainContext, message: str) -> bool:
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name == INTENT_START_ORDER:
        return not _resolved_product_title(ctx)
    return bool(_ORDER_INTENT_RE.search(message or "")) and not _resolved_product_title(ctx)


def next_missing_order_field(ctx: BrainContext) -> Optional[str]:
    """Public wrapper — next checkout slot when product context is active."""
    return _next_missing_order_field(ctx)


def build_product_ordering_prompt(ctx: BrainContext) -> str:
    """
    Choose a natural Saudi WhatsApp prompt from conversation context.

    Never returns :data:`LEGACY_ROBOTIC_PRODUCT_PROMPT`.
    """
    message = ctx.message or ""

    if _is_best_seller_request(message):
        names = _catalog_titles(ctx)
        joined = _join_names(names, limit=2)
        if joined:
            return (
                f"الأكثر طلبًا عندنا غالبًا {joined}. "
                "تبي أعطيك التفاصيل والأسعار؟"
            )
        return "أبشر، أعطيك الأكثر طلبًا عندنا — تبي التفاصيل والأسعار؟"

    missing = _next_missing_order_field(ctx)
    if missing == "variant":
        variants = _variant_labels(ctx)
        title = _resolved_product_title(ctx)
        if variants:
            vjoin = " أو ".join(variants[:3])
            if title:
                return f"تمام، تبي {vjoin} من «{title}»؟"
            return f"تمام، تبي {vjoin}؟"
        if title:
            return f"تمام، أي حجم/خيار تفضل لـ «{title}»؟"
        return "تمام، أي حجم تفضل؟"
    if missing == "quantity":
        title = _resolved_product_title(ctx)
        if title:
            return f"تمام، كم الكمية اللي تبيها من «{title}»؟"
        return "تمام، كم الكمية اللي تبيها؟"
    if missing == "address":
        return "تمام، وين التوصيل؟ أرسل المدينة أو رابط الموقع."
    if missing == "payment":
        return "تمام، تبي الدفع تحويل بنكي ولا عند الاستلام؟"

    if _is_browse_inventory(message):
        names = _catalog_titles(ctx)
        joined = _join_names(names, limit=2)
        if _is_honey_context(ctx, message) and joined:
            return (
                f"المتوفر عندنا من العسل: {joined}. "
                "تبغى أعطيك الأسعار والأحجام؟"
            )
        if joined:
            return f"المتوفر عندنا: {joined}. تبغى أعطيك الأسعار والأحجام؟"
        return "أبشر، أعطيك المتوفر عندنا — تبغى الأسعار والأحجام؟"

    if _is_order_intent_without_product(ctx, message) or _is_honey_context(ctx, message):
        names = _catalog_titles(ctx)
        joined = _join_names(names, limit=2)
        if _is_honey_context(ctx, message):
            if joined:
                return (
                    f"أبشر، المتوفر عندنا {joined}. "
                    "تفضل أي نوع؟"
                )
            return (
                "أبشر، تبي أجهز لك طلح نجد أو سمر الحجاز؟ "
                "وإذا عندك نوع معين اكتبه لي."
            )
        if joined:
            return f"أبشر، المتوفر عندنا {joined}. تفضل أي نوع؟"
        return "أبشر، وش المنتج اللي تبي أجهزه لك؟"

    return "أبشر، وش المنتج اللي تبي أجهزه لك؟"


def resolve_product_clarify_question(
    ctx: BrainContext,
    question: str = "",
) -> str:
    """
    Replace legacy robotic copy with a context-aware prompt.

    If *question* is already contextual, keep it unless it matches the banned phrase.
    """
    raw = str(question or "").strip()
    if raw == LEGACY_ROBOTIC_PRODUCT_PROMPT or not raw:
        return build_product_ordering_prompt(ctx)
    if "ما المنتج الذي تود" in raw and "أكثر مبيعاً" in raw:
        return build_product_ordering_prompt(ctx)
    return raw


def build_ordering_clarify_args(ctx: BrainContext) -> Dict[str, Any]:
    return {"question": resolve_product_clarify_question(ctx)}


__all__ = [
    "LEGACY_ROBOTIC_PRODUCT_PROMPT",
    "build_ordering_clarify_args",
    "build_product_ordering_prompt",
    "next_missing_order_field",
    "resolve_product_clarify_question",
]
