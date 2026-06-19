"""
clarification/resolved_product_guard.py
───────────────────────────────────────
Platform-wide evidence guard for P1-A clarification leakage.

When a product subject is already resolved from the inbound turn (price
subject, slot query, or active focus title), emitters must not re-open
product identification — no honey-type lists, no «أي نوع» prompts.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.clarification.resolved_product_guard")

# Clarification copy that re-opens SKU / category identification.
_TYPE_REOPEN_MARKERS = (
    "مثلاً سدر",
    "سدر، طلح",
    "سدر / طلح",
    "أي نوع",
    "أي نوع أو صفة",
    "أي نوع أو وصفة",
    "حسب النوع (سدر",
    "الأنواع (سدر",
    "تقصد حاجة أو مواصفة",
    "وضّح الاستخدام أو الصفة",
)

_BARE_SUBJECT_TOKENS = frozenset({
    "كilo", "كيلo", "كيلو", "كيلوغرام", "kg", "gram", "جرام", "كجم", "g",
    "لتر", "ml", "piece", "pack", "حبه", "حبة", "سعر", "بكم", "كم", "ال",
    # Order-start verbs — never catalog product names (P0 start-order guard).
    "اطلب", "اشتري", "طلب", "order", "buy", "purchase", "اخذ", "استلم", "شراء",
})


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


def _has_product_substance(subject: str) -> bool:
    try:
        from ..commerce.start_order_verb_guard import is_order_verb_only_query  # noqa: PLC0415

        if is_order_verb_only_query(subject):
            return False
    except Exception:
        logger.exception(
            "[RESOLVED_PRODUCT_GUARD] start_order_verb_guard probe failed",
        )
    norm = _normalize(subject)
    norm = re.sub(r"^ال", "", norm)
    tokens = [t for t in norm.split() if t and t not in _BARE_SUBJECT_TOKENS]
    return any(len(t) >= 2 for t in tokens)


def extract_resolved_product_subject(
    ctx: Any,
    *,
    query: str = "",
    inquiry_query: str = "",
) -> str:
    """Unified resolved product subject for guard + telemetry."""
    q = str(query or "").strip()
    if q and _has_product_substance(q):
        return q

    iq = str(inquiry_query or "").strip()
    if iq and _has_product_substance(iq):
        return iq

    try:
        from ..product_discovery_gate import _resolved_product_query  # noqa: PLC0415

        resolved = str(_resolved_product_query(ctx) or "").strip()
        if resolved and _has_product_substance(resolved):
            return resolved
    except Exception:
        logger.exception(
            "[RESOLVED_PRODUCT_GUARD] _resolved_product_query failed",
        )

    focus = getattr(getattr(ctx, "state", None), "current_product_focus", None) or {}
    if isinstance(focus, dict):
        title = str(focus.get("title") or "").strip()
        if title and _has_product_substance(title):
            return title

    return ""


def has_resolved_product_subject(
    ctx: Any,
    *,
    query: str = "",
    inquiry_query: str = "",
) -> bool:
    return bool(extract_resolved_product_subject(
        ctx, query=query, inquiry_query=inquiry_query,
    ))


def is_product_identification_clarification(text: str) -> bool:
    """True when *text* asks the customer to identify product type/SKU."""
    raw = (text or "").strip()
    if not raw:
        return False
    norm = _normalize(raw)
    return any(_normalize(marker) in norm for marker in _TYPE_REOPEN_MARKERS)


def compose_resolved_product_price_ack(subject: str) -> str:
    """Short price-turn ack when subject is known — no re-identification."""
    name = (subject or "المنتج").strip()
    return f"حاضر، بخصوص *{name}* — راجع معي السعر من الكتالوج."


def compose_resolved_product_search_miss(
    subject: str,
    *,
    variant: int = 0,
) -> str:
    """Concise honest failure — never re-ask product type."""
    if not _has_product_substance(subject):
        from ..commerce.catalog_search_evidence import (  # noqa: PLC0415
            compose_catalog_miss_deterministic_reply,
        )

        return compose_catalog_miss_deterministic_reply(variant=variant)
    name = (subject or "المنتج").strip()
    variants = (
        (
            f"حاضر، بخصوص *{name}* — ما لقيت تطابقاً واضحاً في الكتالوج "
            "حالياً.\n"
            "جرّب اسم المنتج كما يظهر في المتجر، أو اكتب «أكثر مبيعاً»."
        ),
        (
            f"بخصوص *{name}* — ما ظهر عندي في الكتالوج الآن.\n"
            "إذا عندك اسم أدق للمنتج أرسله، أو قول «أكثر مبيعاً»."
        ),
        (
            f"حاضر، ما لقيت *{name}* في الكتالوج حالياً.\n"
            "أرسل الاسم كما في المتجر أو اطلب «أكثر مبيعاً»."
        ),
    )
    return variants[variant % len(variants)]


def log_clarification_leak(
    *,
    tenant_id: Any = None,
    source: str = "",
    normalized_subject: str = "",
    resolved_query: str = "",
    preview: str = "",
    blocked_text: str = "",
) -> None:
    try:
        logger.warning(
            "[CLARIFICATION_LEAK] tenant=%s source=%s normalized_subject=%r "
            "resolved_query=%r preview=%r blocked=%r",
            tenant_id if tenant_id is not None else "-",
            source or "-",
            (normalized_subject or "")[:80],
            (resolved_query or "")[:80],
            (preview or "")[:80],
            (blocked_text or "")[:120],
        )
    except Exception:  # noqa: silent-ok — telemetry emit must never raise to caller
        pass


def apply_resolved_product_clarify_guard(
    ctx: Any,
    question: str,
    *,
    source: str = "",
    query: str = "",
) -> str:
    """
    Replace product-identification clarify when subject is already resolved.

    Logs ``[CLARIFICATION_LEAK]`` when a blocked clarification would have
    reached the customer.
    """
    q = str(question or "").strip()
    if not q:
        return q

    subject = extract_resolved_product_subject(ctx, query=query)
    if not subject:
        return q

    if not is_product_identification_clarification(q):
        return q

    log_clarification_leak(
        tenant_id=getattr(ctx, "tenant_id", None),
        source=source or "clarify_guard",
        normalized_subject=subject,
        resolved_query=subject,
        preview=str(getattr(ctx, "message", "") or "")[:80],
        blocked_text=q,
    )
    return compose_resolved_product_search_miss(subject)


def search_retry_queries(query: str) -> list[str]:
    """
    Deterministic catalog retry queries when exact search misses.

    Platform-wide token shaping — not merchant-specific synonyms.
    """
    raw = (query or "").strip()
    if not raw or not _has_product_substance(raw):
        return []

    seen: set[str] = set()
    out: list[str] = []

    def _add(candidate: str) -> None:
        c = (candidate or "").strip()
        if not c or not _has_product_substance(c):
            return
        key = _normalize(c)
        if key in seen or key == _normalize(raw):
            return
        seen.add(key)
        out.append(c)

    norm = _normalize(raw)
    tokens = [t for t in raw.split() if _has_product_substance(t)]

    if norm.startswith("ال") and len(norm) > 3:
        _add(raw[2:].strip())

    if len(tokens) >= 2:
        _add(tokens[-1])
        _add(" ".join(tokens[-2:]))

    for token in tokens:
        bare = re.sub(r"^ال", "", token, flags=re.UNICODE)
        if bare and bare != token:
            _add(bare)

    try:
        from ..commerce.catalog_query_normalization import (  # noqa: PLC0415
            expand_catalog_search_queries,
        )

        for variant in expand_catalog_search_queries(raw):
            _add(variant)
    except Exception:
        logger.exception(
            "[RESOLVED_PRODUCT_GUARD] catalog_query_expansion_failed query=%r",
            raw[:80],
        )

    return out[:6]


def extract_resolved_product_subject_from_message(message: str) -> str:
    """Message-only subject extraction for webhook layers without BrainContext."""
    raw = (message or "").strip()
    if not raw:
        return ""
    try:
        from ..product_discovery_gate import (  # noqa: PLC0415
            _extract_price_subject,
            extract_inquiry_product_query,
        )
        for candidate in (
            _extract_price_subject(raw),
            extract_inquiry_product_query(raw),
        ):
            c = str(candidate or "").strip()
            if c and _has_product_substance(c):
                return c
        from ..commerce.catalog_query_normalization import (  # noqa: PLC0415
            extract_english_order_product_query,
        )

        en = extract_english_order_product_query(raw)
        if en and _has_product_substance(en):
            return en
    except Exception:
        logger.exception(
            "[RESOLVED_PRODUCT_GUARD] message subject extraction failed",
        )
    return ""


__all__ = [
    "apply_resolved_product_clarify_guard",
    "compose_resolved_product_price_ack",
    "compose_resolved_product_search_miss",
    "extract_resolved_product_subject",
    "extract_resolved_product_subject_from_message",
    "has_resolved_product_subject",
    "is_product_identification_clarification",
    "log_clarification_leak",
    "search_retry_queries",
]
