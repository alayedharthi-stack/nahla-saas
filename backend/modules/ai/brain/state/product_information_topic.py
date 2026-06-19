"""
brain/state/product_information_topic.py
────────────────────────────────────────
Detect product usage / information questions that must block checkout
continuation until answered.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, List, Sequence

_PRODUCT_INFO_RE = re.compile(
    r"(?:"
    r"طريق(?:ه|ة)\s*(?:ال)?(?:استخدام|استعمال|الاستخدام|الاستعمال)"
    r"|(?:كيف|وش|متى|هل)\s+(?:استخدم|استعمل|اخذ|آخذ|أخذ|اشرب|آكل|استعمال|الاستخدام)"
    r"|(?:ي|ت)?(?:اليت|اليت)\s+(?:ت)?(?:خبر|وضح|قول)"
    r"|(?:طريقة|طريقه)\s*(?:ال)?(?:استخدام|استعمال)"
    r"|(?:كم\s*(?:مر(?:ه|ة)|جر(?:عه|عة)|حبه|حبة|ملع(?:ق|قة)))\s*(?:بال)?(?:اليوم|الاسبوع|الأسبوع|الاسبوع)?"
    r"|(?:متى\s*(?:ي|ت)?(?:ؤخذ|اخذ|آخذ|أخذ|يؤخذ|تؤخذ))"
    r"|(?:هل\s*(?:يناسب|يصلح|ينفع|يفيد|مسموح))"
    r"|(?:وش\s*(?:فائد(?:ت(?:ه|ها))?|فايد(?:ت(?:ه|ها))?|فائدة|فايدة))"
    r"|(?:مكون(?:ات)?(?:ه|ها)?|محتو(?:ى|يات)(?:ه|ها)?)"
    r"|how\s+(?:to\s+)?use|usage|dosage|ingredients?"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_FULFILLMENT_ONLY_RE = re.compile(
    r"(?:"
    r"^(?:ال)?(?:موقع|عنوان|العنوان|المدين(?:ه|ة)|city)\s*[:：]?"
    r"|maps\.google|goo\.gl/maps"
    r"|العنوان\s*الوطني"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _normalize(text: str) -> str:
    try:
        from ..interpret.semantic_turn_interpreter import normalize_ar  # noqa: PLC0415

        return normalize_ar(text or "")
    except Exception:  # noqa: BLE001
        return (text or "").strip().lower()


def _join(parts: Iterable[str]) -> str:
    return "\n".join(str(p or "").strip() for p in parts if str(p or "").strip())


def detect_product_information_topic_shift(message: str) -> bool:
    norm = _normalize(message or "")
    if not norm:
        return False
    if _FULFILLMENT_ONLY_RE.search(norm) and not _PRODUCT_INFO_RE.search(norm):
        return False
    return bool(_PRODUCT_INFO_RE.search(norm))


def recent_unresolved_product_information(
    history: Sequence[dict],
    *,
    current_message: str = "",
) -> bool:
    """True when a recent inbound usage question is still open."""
    if detect_product_information_topic_shift(current_message):
        return True

    rows = list(history or [])[-6:]
    saw_info = False
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        direction = str(row.get("direction") or row.get("role") or "").lower()
        body = str(row.get("body") or row.get("message") or "")
        if direction in {"outbound", "assistant", "bot", "ai"}:
            if saw_info:
                return False
            continue
        if direction not in {"inbound", "in", "user", "customer", ""}:
            continue
        if detect_product_information_topic_shift(body):
            saw_info = True
        elif saw_info:
            try:
                from .product_correction import detect_product_correction  # noqa: PLC0415

                if detect_product_correction(body):
                    return False
            except Exception:  # noqa: BLE001  # noqa: silent-ok — optional correction import in history scan
                pass
            if _normalize(body) and len(_normalize(body).split()) > 2:
                return False
    return saw_info


def product_information_blocks_checkout(ctx: Any) -> bool:
    msg = str(getattr(ctx, "message", "") or "")
    if detect_product_information_topic_shift(msg):
        return True
    history = getattr(ctx, "history", None) or []
    if not recent_unresolved_product_information(history, current_message=msg):
        return False
    state = getattr(ctx, "state", None)
    stage = str(getattr(state, "stage", "") or "")
    op = getattr(state, "order_prep", None)
    if stage not in ("ordering", "deciding", "checkout"):
        return False
    if getattr(state, "current_product_focus", None):
        return True
    if op is not None and str(getattr(op, "product_id", "") or "").strip():
        return True
    return False


def collect_product_information_context(ctx: Any) -> str:
    chunks: List[str] = []
    for row in (getattr(ctx, "history", None) or [])[-6:]:
        if isinstance(row, dict) and str(row.get("direction") or "") == "inbound":
            body = str(row.get("body") or row.get("message") or "").strip()
            if body:
                chunks.append(body)
    return _join(chunks)


__all__ = [
    "collect_product_information_context",
    "detect_product_information_topic_shift",
    "product_information_blocks_checkout",
    "recent_unresolved_product_information",
]
