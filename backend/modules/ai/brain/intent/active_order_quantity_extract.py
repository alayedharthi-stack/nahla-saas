"""
active_order_quantity_extract.py
────────────────────────────────
Platform-wide bare quantity / variant consumption during active orders.

When fulfillment context is active, messages like «نص كيلo» or
«فيه نص كيلo بالشمع والعكبر» must not fall through with empty cart intents.
No tenant or catalog hardcoding.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_VARIANT_PATTERNS = [
    (re.compile(r"(?:نصف|نص)\s*(?:كilo|كيلo|كيلو|ك)?", re.I), "500g"),
    (re.compile(r"(?:ربع)\s*(?:كilo|كيلo|كيلo|ك)?", re.I), "250g"),
    (re.compile(r"^(?:كilo|كيلo|كيلo|1\s*kg|1\s*كilo)\s*$", re.I), "1kg"),
    (re.compile(r"(?:كilo|كيلo|كيلo|1\s*kg|1\s*كilo)\b", re.I), "1kg"),
    (re.compile(r"\b500\s*g\b", re.I), "500g"),
    (re.compile(r"\b250\s*g\b", re.I), "250g"),
]

_BARE_QTY_SIGNAL_RE = re.compile(
    r"(?:"
    r"(?:نصف|نص|ربع)\s*(?:كilo|كيلo|كيلo|ك)?|"
    r"(?:كilo|كيلo|كيلo|1\s*kg|1\s*كilo)\b|"
    r"\b500\s*g\b|\b250\s*g\b|"
    r"حبتين|حبة\s*واحدة?|(?:٤|4|اربع|أربع)\s*(?:حبة|حبات)?|"
    r"(?:٤|4|اربع|أربع|حبتين|اثنين|2)\s*(?:حبة|حبات|قطعة|قطع)?"
    r")",
    re.I | re.UNICODE,
)

_VARIANT_DETAIL_RE = re.compile(
    r"(?:بال|مع|in)\s*(.+?)(?:\s*$|(?:\s+و\s+))",
    re.I | re.UNICODE,
)
_FEYHA_PREFIX_RE = re.compile(r"^فيه\s+", re.I | re.UNICODE)

_QTY_TWO_RE = re.compile(r"^(?:حبتين|اثنين|2\s*(?:حبة|حبات)?)\s*$", re.I)
_QTY_FOUR_RE = re.compile(
    r"^(?:٤|4|اربع|أربع|اربعة|أربعة)\s*(?:حبة|حبات|قطعة|قطع)?\s*$",
    re.I,
)
_KILO_ONLY_RE = re.compile(
    r"^(?:كilo|كيلo|كيلo|1\s*kg|1\s*كilo|ك\s*ilo)\s*$",
    re.I,
)

_VARIANT_STRIP_RE = re.compile(
    r"(?:^|\s)(?:فيه|نصف|نص|ربع|كilo|كيلo|كيلo|ك|kg|g|حبة|حبات|حبتين|"
    r"واحد|واحدة|قطعة|قطع|\d+)(?:\s|$)",
    re.I,
)

_SOCIAL_SEGMENT_RE = re.compile(
    r"(?:الله\s+يسلم|يسلمك|من\s+كل\s+شر|جزاك\s+الله|بارك\s+الله|"
    r"شكرا|شكراً|السلام|سلام\s+عليكم|حياك\s+الله|ماشاء\s+الله)",
    re.I | re.UNICODE,
)

_CLARIFY_SPLIT_HALF_AR = (
    "فهمت إنك تبغى نصف كيلo ونصف كيلo. وضّح لي: كل نصف كيلo من أي نوع؟"
)
_CLARIFY_SPLIT_GENERIC_AR = (
    "فهمت إنك تبغى أكثر من حجم. وضّح لي: كل حجم لأي منتج؟"
)
_CLARIFY_VARIANT_DETAIL_AR = "تقصد النصف كيلo يكون {detail}؟"
_OUTSIDE_ORDER_QTY_AR = "وش المنتج والحجم اللي تبغاه بالضبط؟"
_ACTIVE_ORDER_QTY_CONTINUE_AR = "تمام، سجلت الحجم — في شي ثاني للطلب؟"


@dataclass(frozen=True)
class ActiveOrderQuantityResult:
    intents: List[Dict[str, Any]]
    clarification_reply: Optional[str] = None


def _norm(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text).lower())
    t = _NORM_RE.sub("", t)
    t = (
        t.replace("\u0623", "\u0627")
        .replace("\u0625", "\u0627")
        .replace("\u0622", "\u0627")
        .replace("\u0649", "\u064a")
        .replace("\u0629", "\u0647")
    )
    return _WS_RE.sub(" ", t).strip()


def _parse_bare_variant(text: str) -> str:
    for pattern, canonical in _VARIANT_PATTERNS:
        if pattern.search(text or ""):
            return canonical
    return ""


def _parse_count_quantity(text: str) -> Optional[int]:
    norm = _norm(text)
    if _QTY_TWO_RE.match(norm):
        return 2
    if _QTY_FOUR_RE.match(norm):
        return 4
    return None


def _segment_is_bare_variant_only(segment: str) -> bool:
    if not _parse_bare_variant(segment):
        return False
    cleaned = str(segment or "")
    for pattern, _ in _VARIANT_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _FEYHA_PREFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"[\W_]+", " ", cleaned, flags=re.UNICODE)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return len(cleaned) <= 1


def _extract_variant_detail(text: str) -> str:
    raw = (text or "").strip()
    raw = _FEYHA_PREFIX_RE.sub("", raw).strip()
    attached = re.search(r"(?:\s|^)(بال[\u0600-\u06FF\s]+)$", raw, re.UNICODE)
    if attached:
        detail = str(attached.group(1) or "").strip(" .،,")
        if detail and len(detail) >= 2:
            return detail
    if " بال" in raw:
        left, _, right = raw.partition(" بال")
        if _parse_bare_variant(left) and right.strip():
            return f"بال{right.strip(' .،,')}"
    m = _VARIANT_DETAIL_RE.search(raw)
    if m:
        detail = str(m.group(1) or "").strip(" .،,")
        if detail and len(detail) >= 2 and not _parse_bare_variant(detail):
            return detail
    return ""


def _split_segments(message: str) -> List[str]:
    lines = [p.strip() for p in re.split(r"[\n\r]+", (message or "").strip()) if p.strip()]
    if len(lines) > 1:
        return lines
    parts = re.split(r"\s*(?:و|،|,)\s*", (message or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _bare_variant_mention_count(text: str) -> int:
    return len(re.findall(
        r"(?:نصف|نص)\s*(?:كilo|كيلo|كيلo|ك)?|"
        r"(?:ربع)\s*(?:كilo|كيلo|كيلo|ك)?",
        text or "",
        re.I,
    ))


def _is_commerce_segment(segment: str) -> bool:
    seg = (segment or "").strip()
    if not seg:
        return False
    if _BARE_QTY_SIGNAL_RE.search(seg):
        return True
    if _extract_variant_detail(seg):
        return True
    if _parse_count_quantity(seg):
        return True
    if _KILO_ONLY_RE.match(_norm(seg)):
        return True
    return False


def _segment_has_product_hint(segment: str) -> bool:
    """True when segment carries free-text beyond bare size/qty tokens."""
    if _segment_is_bare_variant_only(segment):
        return False
    cleaned = _VARIANT_STRIP_RE.sub(" ", segment or "")
    cleaned = _FEYHA_PREFIX_RE.sub("", cleaned)
    cleaned = _VARIANT_DETAIL_RE.sub(" ", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) < 3:
        return False
    if _SOCIAL_SEGMENT_RE.search(cleaned):
        return False
    return True


def message_has_bare_quantity_or_variant_signal(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    for seg in _split_segments(text):
        if _is_commerce_segment(seg):
            return True
    return bool(_BARE_QTY_SIGNAL_RE.search(text))


def _match_for_item(item: Dict[str, Any]) -> Dict[str, str]:
    name = str(item.get("product_name") or item.get("title") or "")
    needle = name.strip() or name
    match: Dict[str, str] = {"product_name_contains": needle.replace("عسل ", "").strip() or needle}
    variant = str(item.get("variant") or "").strip()
    if variant:
        match["variant"] = variant
    return match


def _resolve_active_target(
    *,
    cart_items: Optional[List[Dict[str, Any]]] = None,
    product_focus: Optional[Dict[str, Any]] = None,
    order_prep: Any = None,
) -> Optional[Dict[str, Any]]:
    cart = list(cart_items or [])
    if cart:
        active = cart[-1]
        pname = str(active.get("product_name") or active.get("title") or "").strip()
        if pname:
            return {"product_name": pname, "match": _match_for_item(active)}

    focus = dict(product_focus or {})
    title = str(focus.get("title") or focus.get("product_name") or "").strip()
    if title:
        needle = title.replace("عسل ", "").strip() or title
        return {"product_name": title, "match": {"product_name_contains": needle}}

    prep = order_prep
    if prep is not None:
        if isinstance(prep, dict):
            line_items = list(prep.get("line_items") or prep.get("cart_items") or [])
            pname = str(prep.get("product_name") or "").strip()
        else:
            line_items = list(getattr(prep, "line_items", None) or getattr(prep, "cart_items", None) or [])
            pname = str(getattr(prep, "product_name", "") or "").strip()
        if line_items:
            active = line_items[-1]
            pname = str(active.get("product_name") or active.get("title") or pname).strip()
            if pname:
                return {"product_name": pname, "match": _match_for_item(active)}
        if pname:
            return {
                "product_name": pname,
                "match": {"product_name_contains": pname.replace("عسل ", "").strip() or pname},
            }
    return None


def _build_split_clarification(segments: List[str]) -> str:
    variants = [_parse_bare_variant(s) for s in segments]
    if len(segments) == 2 and all(v == "500g" for v in variants):
        return _CLARIFY_SPLIT_HALF_AR
    return _CLARIFY_SPLIT_GENERIC_AR


def extract_active_order_quantity_fallback(
    message: str,
    *,
    cart_items: Optional[List[Dict[str, Any]]] = None,
    product_focus: Optional[Dict[str, Any]] = None,
    order_prep: Any = None,
    active_commerce: bool = False,
) -> ActiveOrderQuantityResult:
    """Consume bare qty/variant relative to active order context, or ask to clarify."""
    text = (message or "").strip()
    if not text or not active_commerce:
        return ActiveOrderQuantityResult([], None)
    if not message_has_bare_quantity_or_variant_signal(text):
        return ActiveOrderQuantityResult([], None)

    commerce_segments = [s for s in _split_segments(text) if _is_commerce_segment(s)]
    if not commerce_segments:
        return ActiveOrderQuantityResult([], None)

    target = _resolve_active_target(
        cart_items=cart_items,
        product_focus=product_focus,
        order_prep=order_prep,
    )

    commerce_blob = " ".join(commerce_segments)
    bare_mentions = _bare_variant_mention_count(commerce_blob)
    has_product_named = any(_segment_has_product_hint(s) for s in commerce_segments)
    if bare_mentions >= 2 and not has_product_named:
        return ActiveOrderQuantityResult([], _build_split_clarification(commerce_segments))

    bare_variant_segments = [
        s for s in commerce_segments
        if _segment_is_bare_variant_only(s)
    ]
    if len(bare_variant_segments) >= 2:
        return ActiveOrderQuantityResult([], _build_split_clarification(bare_variant_segments))

    detail = _extract_variant_detail(text)
    variant = _parse_bare_variant(text)
    for seg in commerce_segments:
        if not variant:
            variant = _parse_bare_variant(seg)
        if not detail:
            detail = _extract_variant_detail(seg)

    if detail and variant:
        if target:
            return ActiveOrderQuantityResult([
                {
                    "action": "update_variant",
                    "product_name": target["product_name"],
                    "match": dict(target["match"]),
                    "new_variant": variant,
                },
                {
                    "action": "update_edition",
                    "product_name": target["product_name"],
                    "match": dict(target["match"]),
                    "edition": detail,
                },
            ], None)
        return ActiveOrderQuantityResult(
            [],
            _CLARIFY_VARIANT_DETAIL_AR.format(detail=detail),
        )

    if variant and target and len(bare_variant_segments) <= 1:
        return ActiveOrderQuantityResult([{
            "action": "update_variant",
            "product_name": target["product_name"],
            "match": dict(target["match"]),
            "new_variant": variant,
        }], None)

    qty = _parse_count_quantity(text)
    for seg in commerce_segments:
        if qty is None:
            qty = _parse_count_quantity(seg)
    if qty and target:
        return ActiveOrderQuantityResult([{
            "action": "update_quantity",
            "product_name": target["product_name"],
            "match": dict(target["match"]),
            "quantity": qty,
        }], None)

    if _KILO_ONLY_RE.match(_norm(text)) and target:
        return ActiveOrderQuantityResult([{
            "action": "update_variant",
            "product_name": target["product_name"],
            "match": dict(target["match"]),
            "new_variant": "1kg",
        }], None)

    if target and variant:
        return ActiveOrderQuantityResult([{
            "action": "update_variant",
            "product_name": target["product_name"],
            "match": dict(target["match"]),
            "new_variant": variant,
        }], None)

    return ActiveOrderQuantityResult([], None)


def resolve_active_order_quantity_reply(
    message: str,
    *,
    state: Any = None,
    cart_items: Optional[List[Dict[str, Any]]] = None,
    product_focus: Optional[Dict[str, Any]] = None,
    order_prep: Any = None,
    active_commerce: bool = False,
) -> Optional[str]:
    """
    Deterministic reply when bare qty/variant must not receive generic ACK.
    """
    prep = order_prep
    if prep is None and state is not None:
        if isinstance(state, dict):
            prep = state.get("order_prep")
        else:
            prep = getattr(state, "order_prep", None)

    if prep is not None:
        stored = ""
        if isinstance(prep, dict):
            stored = str(prep.get("active_order_quantity_clarification") or "").strip()
        else:
            stored = str(getattr(prep, "active_order_quantity_clarification", "") or "").strip()
        if stored:
            return stored

    if not message_has_bare_quantity_or_variant_signal(message):
        return None

    if active_commerce:
        result = extract_active_order_quantity_fallback(
            message,
            cart_items=cart_items,
            product_focus=product_focus,
            order_prep=prep,
            active_commerce=True,
        )
        if result.clarification_reply:
            return result.clarification_reply
        if result.intents:
            return _ACTIVE_ORDER_QTY_CONTINUE_AR
        return "تمام، أكمل معك الطلب — وش الحجم أو التفاصيل اللي تبغاها؟"

    return _OUTSIDE_ORDER_QTY_AR


__all__ = [
    "ActiveOrderQuantityResult",
    "extract_active_order_quantity_fallback",
    "message_has_bare_quantity_or_variant_signal",
    "resolve_active_order_quantity_reply",
]
