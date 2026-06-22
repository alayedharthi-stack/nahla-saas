"""
brain/intent/cart_intent_extractor.py
──────────────────────────────────────
PR-4 — Deterministic WhatsApp cart intent extraction (no LLM).

Maps Arabic customer phrases to structured cart actions consumed by
``core.wa_cart_line_items.apply_cart_delta``.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.brain.cart_intent_extractor")

_DIA = "\u064b-\u065f\u0670\u06d6-\u06ed"
_NORM_RE = re.compile(f"[{_DIA}]+")
_WS_RE = re.compile(r"\s+")

_PRODUCT_KEYWORDS = {
    "طلح": "عسل طلح",
    "سمر": "عسل سمر",
    "سدر": "عسل سدر",
    "شوك": "عسل شوك",
    "صفي": "عسل صفي",
    "صيف": "عسل صيفي",
    "صيفي": "عسل صيفي",
    "الصيفي": "عسل صيفي",
    "حبة": "عسل حبة البركة",
    "بركة": "عسل حبة البركة",
    "نجد": "عسل طلح",
    "حجاز": "عسل سمر",
}

_VARIANT_PATTERNS = [
    (re.compile(r"(?:نصف|نص)\s*(?:كilo|كيلو|ك)", re.I), "500g"),
    (re.compile(r"(?:ربع)\s*(?:كilo|كيلو|ك)?", re.I), "250g"),
    (re.compile(r"^(?:كilo|كيلo|كيلو|1\s*kg)$", re.I), "1kg"),
    (re.compile(r"(?:كilo|كيلo|كيلو|1\s*kg|1\s*كilo)\b", re.I), "1kg"),
    (re.compile(r"\b500\s*g\b", re.I), "500g"),
    (re.compile(r"\b250\s*g\b", re.I), "250g"),
]

_ADD_RE = re.compile(
    r"(?:^|\s)(?:أ?ضف|اضف|حط|حطي|ابغ[ىي]|أبغ[ىي]|اريد|أريد|ابي|ابى|"
    r"عطني|عطيني|زود|زيد)(?:\s|$)",
    re.I,
)
_REMOVE_RE = re.compile(
    r"(?:^|\s)(?:احذف|شيل|شيلي|امسح|امسحي|بدون|ما\s+ابغ[ىي]|ما\s+أبغ[ىي]|"
    r"لا\s+خلاص\s+بدون|لا\s+ابغ[ىي]|لا\s+أبغ[ىي])(?:\s|$)",
    re.I,
)
_QTY_SET_RE = re.compile(
    r"(?:خلي|خل|خليها|خليها|اجعل|اجعلها|صير|صير)\s+(.+?)\s+"
    r"(?:حبتين|حبة\s*واحدة?|(\d+)\s*(?:حبة|حبات|قطعة|قطع)?)",
    re.I,
)
_QTY_INC_RE = re.compile(
    r"(?:زود|زيد)\s+(?:حبة|واحد|1)\s*(?:من\s+)?(.+)?$|"
    r"(?:زود|زيد)\s+(.+?)\s+(?:واحد|1|حبة)$",
    re.I,
)
_QTY_DEC_RE = re.compile(
    r"(?:نقص|قلل)\s+(.+?)\s+(?:الى|إلى|ل)\s+(?:حبة|(\d+))",
    re.I,
)
_VARIANT_CHANGE_RE = re.compile(
    r"(?:بدل|غير|بدلها)\s+(.+?)\s+(?:كilo|كيلو)\s+"
    r"(?:خله|خليه|خليها|اجعل|اجعلها)\s+(?:نصف|نص)\s*(?:كilo|كيلو)?",
    re.I,
)
_VARIANT_CHANGE_HALF_RE = re.compile(
    r"(?:بدل|غير)\s+(.+?)\s+(?:نصف|نص)\s*(?:كilo|كيلو)?\s+"
    r"(?:خله|خليه|خليها|اجعل|اجعلها)\s+(?:ربع)\s*(?:كilo|كيلo)?",
    re.I,
)
_CLEAR_RE = re.compile(r"(?:^|\s)(?:امسح\s+السلة|فض(?:ي)?\s+السلة|الغ(?:ي)?\s+الطلب)(?:\s|$)", re.I)

_AR_NUM_WORDS = {
    "واحد": 1, "واحدة": 1, "حبة": 1,
    "اثنين": 2, "إثنين": 2, "ثنتين": 2, "حبتين": 2,
    "ثلاث": 3, "ثلاثة": 3, "تلات": 3, "تلاته": 3,
    "اربع": 4, "أربع": 4, "اربعة": 4,
}

_KILO_ONLY_RE = re.compile(
    r"^(?:كilo|كيلo|كيلو|1\s*kg|1\s*كilo|ك\s*ilo)\s*$",
    re.I,
)
_LARGE_ONLY_RE = re.compile(r"^(?:كبير|كبيرة)\s*$", re.I)
_BUCKET_KG_RE = re.compile(
    r"(?:10\s*(?:كilo|كيلo|كيلو|kg|ك)?|(?:كilo|كيلo|كيلو)\s*10|سطل\s*(?:10)?|10\s*سطل)",
    re.I,
)
_QTY_FOUR_RE = re.compile(
    r"^(?:٤|4|اربع|أربع|اربعة|أربعة)\s*(?:حبة|حبات|قطعة|قطع)?\s*$",
    re.I,
)
_EDITION_ONLY_RE = re.compile(r"^(?:ال)?جديد(?:\s|$)?$", re.I)
_QTY_TWO_RE = re.compile(r"^(?:حبتين|اثنين|2\s*(?:حبة|حبات)?)\s*$", re.I)
_LEADING_QTY_RE = re.compile(
    r"^\s*(?P<qty>[0-9٠-٩]+|واحد|واحدة|اثنين|إثنين|ثنتين|"
    r"ثلاث|ثلاثة|تلات|تلاته|اربع|أربع|اربعة|أربعة)\s+",
    re.I,
)

_ARABIC_NON_CART_TOKENS = {
    "مرحبا", "اهلا", "أهلا", "السلام", "سلام", "شكرا", "شكراً",
    "نعم", "لا", "تمام", "اوكي",
}


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


def _parse_variant(text: str) -> str:
    for pattern, canonical in _VARIANT_PATTERNS:
        if pattern.search(text):
            return canonical
    return ""


def _parse_leading_quantity(text: str) -> int:
    m = _LEADING_QTY_RE.match(text or "")
    if not m:
        return 1
    raw = _norm(m.group("qty") or "")
    if raw in _AR_NUM_WORDS:
        return max(int(_AR_NUM_WORDS[raw]), 1)
    digits = raw.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    try:
        return max(int(digits), 1)
    except (TypeError, ValueError):
        return 1


def _resolve_product_name(text: str) -> str:
    norm = _norm(text)
    if "عسل" in norm:
        if "سمر" in norm and "حجاز" in norm:
            return "عسل سمر الحجاز"
        if "صفي" in norm:
            return "عسل صفي"
        if "صيف" in norm:
            return "عسل صيفي"
        if "سمر" in norm:
            return "عسل سمر"
        if "طلح" in norm or "نجد" in norm:
            return "عسل طلح"
        residual = re.sub(
            r"(?:^|\s)(?:أ?ضف|اضف|حط|حطي|ابغ[ىي]|أبغ[ىي]|اريد|أريد|ابي|ابى|"
            r"عطني|عطيني|عسل|كilo|كيلو|كيلo|نصف|ربع|حبة|حبتين|واحد|واحدة|"
            r"\d+\s*(?:kg|g|كilo|كيلo|كيلو)?)(?:\s|$)",
            " ",
            norm,
            flags=re.I,
        )
        residual = _WS_RE.sub(" ", residual).strip()
        if residual:
            return ""
        return "عسل"
    for key, name in _PRODUCT_KEYWORDS.items():
        if key in norm:
            return name
    cleaned = re.sub(
        r"(?:^|\s)(?:أ?ضف|اضف|حط|ابغ[ىي]|أبغ[ىي]|اريد|عسل|كilo|كيلو|نصف|ربع|"
        r"حبة|حبتين|واحد|واحدة|\d+\s*(?:kg|g|كilo|كيلo)?)(?:\s|$)",
        " ",
        norm,
        flags=re.I,
    )
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if cleaned in _ARABIC_NON_CART_TOKENS:
        return ""
    # Do not invent free-text product names — only known honey keywords above.
    return ""


def _split_multi_segments(message: str) -> List[str]:
    lines = [p.strip() for p in re.split(r"[\n\r]+", message.strip()) if p.strip()]
    if len(lines) > 1:
        return lines
    parts = re.split(r"\s+(?:و|،|,)\s*", message.strip())
    return [p.strip() for p in parts if p.strip()]


def _skip_cart_segment(segment: str) -> bool:
    """Skip gift headers and recipient name lines — not cart lines."""
    seg = (segment or "").strip()
    if not seg:
        return True
    norm = _norm(seg)
    if re.search(r"طلب\s+توصيل", norm):
        if not _parse_variant(seg) and not any(
            k in norm for k in ("طلح", "سمر", "صيف", "صفي", "سدر", "شوك")
        ):
            return True
    if re.search(r"لهذا\s+الشخص|هدية", norm) and not _parse_variant(seg):
        if not any(k in norm for k in _PRODUCT_KEYWORDS) and "عسل" not in norm:
            return True
    tokens = [t for t in seg.split() if t.strip()]
    if len(tokens) >= 2 and len(tokens) <= 4:
        if not _parse_variant(seg) and not any(k in norm for k in _PRODUCT_KEYWORDS):
            if "عسل" not in norm and not _ADD_RE.search(seg):
                return True
    return False


def _is_probable_line_item_segment(segment: str) -> bool:
    seg = (segment or "").strip()
    if not seg or _skip_cart_segment(seg):
        return False
    norm = _norm(seg)
    if any(k in norm for k in _PRODUCT_KEYWORDS) or "عسل" in norm:
        return bool(_parse_variant(seg) or _LEADING_QTY_RE.match(seg) or _ADD_RE.search(seg))
    return False


def _extract_add_segment(segment: str) -> Optional[Dict[str, Any]]:
    seg = segment.strip()
    if not seg:
        return None
    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            has_explicit_order_select_signal,
            is_commerce_inquiry_turn,
        )

        if is_commerce_inquiry_turn(seg) and not has_explicit_order_select_signal(seg):
            return None
    except Exception:  # noqa: silent-ok - inquiry guard is best-effort
        pass
    is_add = bool(_ADD_RE.search(seg)) or not (
        _REMOVE_RE.search(seg) or _QTY_SET_RE.search(seg) or _VARIANT_CHANGE_RE.search(seg)
    )
    if _REMOVE_RE.search(seg):
        return None
    if not is_add and not _parse_variant(seg) and not any(k in _norm(seg) for k in _PRODUCT_KEYWORDS):
        return None
    name = _resolve_product_name(seg)
    if not name:
        return None
    variant = _parse_variant(seg)
    qty = _parse_leading_quantity(seg)
    if "ثاني" in _norm(seg) or "second" in _norm(seg):
        qty = 1
    return {
        "action": "add_item",
        "product_name": name,
        "variant": variant,
        "quantity": qty,
    }


def extract_cart_intents(message: str) -> List[Dict[str, Any]]:
    """
    Extract zero or more cart intents from a customer message.

    Returns actions: ``add_item``, ``update_quantity``, ``update_variant``,
    ``remove_item``, ``clear_cart``.
    """
    if not message or not str(message).strip():
        return []

    text = str(message).strip()
    norm = _norm(text)
    if norm in _ARABIC_NON_CART_TOKENS:
        return []
    try:
        from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
            has_explicit_order_select_signal,
            is_commerce_inquiry_turn,
        )

        if is_commerce_inquiry_turn(text) and not has_explicit_order_select_signal(text):
            return []
    except Exception:  # noqa: silent-ok - inquiry guard is best-effort
        pass
    has_product_hint = any(k in norm for k in _PRODUCT_KEYWORDS) or "عسل" in norm
    has_cart_verb = bool(
        _ADD_RE.search(text)
        or _REMOVE_RE.search(text)
        or _QTY_SET_RE.search(text)
        or _QTY_INC_RE.search(text)
        or _QTY_DEC_RE.search(text)
        or _VARIANT_CHANGE_RE.search(text)
        or _VARIANT_CHANGE_HALF_RE.search(text)
        or _CLEAR_RE.search(norm)
    )
    if not has_product_hint and not has_cart_verb:
        return []
    # Short product tokens like "سمر" alone are valid ordering hints.
    if norm in _PRODUCT_KEYWORDS or norm in {"سمر", "طلح", "سدر", "شوك"}:
        has_product_hint = True
    intents: List[Dict[str, Any]] = []

    if _CLEAR_RE.search(norm):
        return [{"action": "clear_cart"}]

    for pattern, new_variant in (
        (_VARIANT_CHANGE_HALF_RE, "250g"),
        (_VARIANT_CHANGE_RE, "500g"),
    ):
        m = pattern.search(text)
        if m:
            product = _resolve_product_name(m.group(1))
            if product:
                old_variant = "1kg"
                intents.append({
                    "action": "update_variant",
                    "product_name": product,
                    "match": {
                        "product_name_contains": product.replace("عسل ", ""),
                        "variant": old_variant,
                    },
                    "old_variant": old_variant,
                    "new_variant": new_variant,
                })
                return intents

    m_qty = _QTY_SET_RE.search(text)
    if m_qty:
        product = _resolve_product_name(m_qty.group(1))
        if product:
            qty_raw = m_qty.group(2)
            if qty_raw and qty_raw.isdigit():
                qty = int(qty_raw)
            elif "حبتين" in _norm(m_qty.group(0)):
                qty = 2
            else:
                qty = 1
            intents.append({
                "action": "update_quantity",
                "product_name": product,
                "match": {"product_name_contains": product.replace("عسل ", "")},
                "quantity": qty,
            })
            return intents

    m_inc = _QTY_INC_RE.search(text)
    if m_inc:
        ref = (m_inc.group(1) or m_inc.group(2) or "").strip()
        product = _resolve_product_name(ref or text)
        if product:
            intents.append({
                "action": "increment_quantity",
                "product_name": product,
                "match": {"product_name_contains": product.replace("عسل ", "")},
                "delta": 1,
            })
            return intents

    m_dec = _QTY_DEC_RE.search(text)
    if m_dec:
        product = _resolve_product_name(m_dec.group(1))
        qty_raw = m_dec.group(2)
        if product:
            qty = int(qty_raw) if qty_raw and qty_raw.isdigit() else 1
            intents.append({
                "action": "update_quantity",
                "product_name": product,
                "match": {"product_name_contains": product.replace("عسل ", "")},
                "quantity": qty,
            })
            return intents

    if _REMOVE_RE.search(text):
        product = _resolve_product_name(text)
        if product:
            intents.append({
                "action": "remove_item",
                "product_name": product,
                "match": {"product_name_contains": product.replace("عسل ", "")},
            })
            return intents

    segments = _split_multi_segments(text)
    if len(segments) > 1:
        expected = 0
        unresolved: List[str] = []
        for seg in segments:
            if _skip_cart_segment(seg):
                continue
            probable = _is_probable_line_item_segment(seg)
            if probable:
                expected += 1
            item = _extract_add_segment(seg)
            if item:
                intents.append(item)
            elif probable:
                unresolved.append(seg)
        if expected:
            logger.info(
                "[COMPOUND_ORDER_PARSE] compound_order_detected=true "
                "expected_line_items_count=%s extracted_line_items_count=%s "
                "unresolved_product_mentions=%s",
                expected,
                len(intents),
                unresolved,
            )
        if intents:
            return intents

    item = _extract_add_segment(text)
    if item:
        intents.append(item)
    elif norm in {"سمر", "طلح", "صفي", "سدر", "شوك"} or norm in _PRODUCT_KEYWORDS:
        try:
            from modules.ai.brain.commerce.commerce_inquiry_boundary import (  # noqa: PLC0415
                has_explicit_order_select_signal,
                is_commerce_inquiry_turn,
            )

            if is_commerce_inquiry_turn(text) and not has_explicit_order_select_signal(text):
                return intents
        except Exception:  # noqa: silent-ok - inquiry guard is best-effort
            pass
        name = _resolve_product_name(text)
        if name:
            intents.append({
                "action": "add_item",
                "product_name": name,
                "variant": _parse_variant(text),
                "quantity": 1,
            })
    return intents


def _active_cart_item(cart_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cart_items:
        return None
    return cart_items[-1]


def _match_for_item(item: Dict[str, Any]) -> Dict[str, str]:
    name = str(item.get("product_name") or item.get("title") or "")
    needle = name.replace("عسل ", "").strip() or name
    match: Dict[str, str] = {"product_name_contains": needle}
    variant = str(item.get("variant") or "").strip()
    if variant:
        match["variant"] = variant
    return match


def extract_cart_intents_with_context(
    message: str,
    *,
    cart_items: Optional[List[Dict[str, Any]]] = None,
    product_focus: Optional[Dict[str, Any]] = None,
    order_prep: Any = None,
    active_commerce: bool = False,
) -> List[Dict[str, Any]]:
    """
    Context-aware cart intents — stabilizes repeated ``كيلo`` / edition picks.

    When the cart already has a product+variant, a lone ``كيلo`` confirms
    ``quantity=1`` instead of re-opening variant selection.
    """
    if not message or not str(message).strip():
        return []

    text = str(message).strip()
    norm = _norm(text)
    cart = list(cart_items or [])
    active = _active_cart_item(cart)

    if _EDITION_ONLY_RE.match(norm) and active:
        return [{
            "action": "update_edition",
            "product_name": active.get("product_name") or "",
            "match": _match_for_item(active),
            "edition": "إنتاج 1447 / الجديد",
        }]

    if _KILO_ONLY_RE.match(norm) and active:
        variant = str(active.get("variant") or "").strip()
        if variant:
            return [{
                "action": "update_quantity",
                "product_name": active.get("product_name") or "",
                "match": _match_for_item(active),
                "quantity": 1,
            }]
        return [{
            "action": "update_variant",
            "product_name": active.get("product_name") or "",
            "match": _match_for_item(active),
            "new_variant": "1kg",
        }]

    if _LARGE_ONLY_RE.match(norm) and active:
        variant = str(active.get("variant") or "").strip()
        if variant:
            return [{
                "action": "update_quantity",
                "product_name": active.get("product_name") or "",
                "match": _match_for_item(active),
                "quantity": 1,
            }]
        return [{
            "action": "update_variant",
            "product_name": active.get("product_name") or "",
            "match": _match_for_item(active),
            "new_variant": "1kg",
        }]

    if _BUCKET_KG_RE.search(text) and active:
        return [{
            "action": "update_variant",
            "product_name": active.get("product_name") or "",
            "match": _match_for_item(active),
            "new_variant": "10kg",
        }]

    if _QTY_FOUR_RE.match(norm) and active:
        return [{
            "action": "update_quantity",
            "product_name": active.get("product_name") or "",
            "match": _match_for_item(active),
            "quantity": 4,
        }]

    if _QTY_TWO_RE.match(norm) and active:
        return [{
            "action": "update_quantity",
            "product_name": active.get("product_name") or "",
            "match": _match_for_item(active),
            "quantity": 2,
        }]

    base = extract_cart_intents(message)
    if base:
        return base

    if active_commerce:
        from modules.ai.brain.intent.active_order_quantity_extract import (  # noqa: PLC0415
            extract_active_order_quantity_fallback,
        )

        fallback = extract_active_order_quantity_fallback(
            message,
            cart_items=cart,
            product_focus=product_focus,
            order_prep=order_prep,
            active_commerce=True,
        )
        if fallback.clarification_reply:
            return [{
                "action": "active_order_clarify",
                "reply": fallback.clarification_reply,
            }]
        if fallback.intents:
            return fallback.intents

    return []


__all__ = ["extract_cart_intents", "extract_cart_intents_with_context"]
