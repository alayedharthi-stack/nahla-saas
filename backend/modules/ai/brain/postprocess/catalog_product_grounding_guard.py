"""
catalog_product_grounding_guard.py
──────────────────────────────────
Post-compose guard: block invented product names in customer replies.

Product option lists must be sourced from active catalog evidence only —
not LLM memory, stale KB, or generic category knowledge.

Modes (NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE):
  off     — disabled
  shadow  — log only
  enforce — rewrite ungrounded product lists (default)
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from modules.ai.brain.commerce.catalog_product_grounding import (
    build_uncertain_catalog_reply,
    extract_seasonal_product_subject,
    is_seasonal_availability_ask,
    seasonal_subject_in_catalog,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (
    ProductClaimGroundingEvidence,
    _norm,
)

logger = logging.getLogger("nahla.brain.postprocess.catalog_product_grounding_guard")

_DETERMINISTIC_ALLOW_PATHS = frozenset({
    "variant_pricing",
    "product_search_results",
    "product_card_send",
    "notify_me_back_in_stock_ack",
    "catalog_product_list",
    "catalog_miss_resolved_subject",
    "rule",
    "catalog_navigation_groups",
    "catalog_navigation_group_products",
    "catalog_navigation_top_products_fallback",
})

_HONEY_PRODUCT_MENTION_RE = re.compile(
    r"عسل\s+[\u0600-\u06FFa-zA-Z]+(?:\s+[\u0600-\u06FFa-zA-Z]+){0,3}",
    re.UNICODE,
)

_BULLET_OR_NUMBERED_RE = re.compile(
    r"^(?:[-•*]|\d+[\.\)])\s*(.+)$",
    re.MULTILINE | re.UNICODE,
)

_SEASONAL_DATE_CLAIM_RE = re.compile(
    r"(?:"
    r"(?:يجي|يوصل|ينزل|يتوفر|راح\s+يجي|راح\s+يوصل|بيكون)\s+"
    r"(?:في|بعد|خلال|قريب|الاسبوع|الأسبوع|الشهر|الصيف|الشتاء|رمضان)"
    r"|(?:متوقع|مقرر)\s+(?:في|بعد)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

_KNOWN_INVENTION_MARKERS = (
    "عسل القطف",
    "عسل الشهد",
    "عسل السدر",
    "عسل الطلح البلدي",
)


def catalog_product_grounding_guard_mode() -> str:
    mode = os.environ.get(
        "NAHLA_CATALOG_PRODUCT_GROUNDING_GUARD_MODE", "enforce",
    ).strip().lower()
    if mode in ("off", "shadow", "enforce"):
        return mode
    return "enforce"


@dataclass(frozen=True)
class CatalogProductGroundingGuardResult:
    reply: str
    action: str
    replaced: bool = False
    reason: str = ""
    ungrounded_mentions: tuple[str, ...] = ()
    shadow_mode: bool = False
    would_rewrite: bool = False


def _catalog_titles_from_evidence(
    evidence: ProductClaimGroundingEvidence,
    executor_products: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[str]:
    titles: List[str] = []
    for row in list(evidence.available_products) + list(executor_products or []):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def _extract_product_mentions(reply: str) -> List[str]:
    mentions: List[str] = []
    seen: Set[str] = set()
    for m in _HONEY_PRODUCT_MENTION_RE.finditer(reply or ""):
        phrase = (m.group(0) or "").strip()
        if phrase and phrase not in seen:
            seen.add(phrase)
            mentions.append(phrase)
    for line in (reply or "").splitlines():
        m = _BULLET_OR_NUMBERED_RE.match(line.strip())
        if not m:
            continue
        item = (m.group(1) or "").strip()
        item = re.sub(r"\*([^*]+)\*", r"\1", item)
        item = re.sub(r":\s*\d+.*$", "", item).strip()
        if item and item not in seen and len(item) >= 4:
            seen.add(item)
            mentions.append(item)
    return mentions


def _distinctive_tokens(text: str) -> Set[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    stop = frozenset({
        "منتج", "product", "عسل", "حجم", "وزن", "كيلو", "نصف", "ربع", "جرام",
        "البلدي", "بلدي", "طبيعي", "اصلي", "أصلي",
    })
    return {
        t
        for t in tokenize(normalize_arabic(text or ""))
        if len(t) >= 3 and t not in stop
    }


def _strict_catalog_mention_match(mention: str, title: str) -> bool:
    """Stricter than fuzzy product reference — blocks same-type different-SKU invention."""
    mention_norm = _norm(mention)
    title_norm = _norm(title)
    if not mention_norm or not title_norm:
        return False
    if title_norm in mention_norm or mention_norm in title_norm:
        return True
    mention_toks = _distinctive_tokens(mention)
    title_toks = _distinctive_tokens(title)
    if not title_toks:
        return False
    overlap = mention_toks & title_toks
    required = 2 if len(title_toks) >= 2 else 1
    return len(overlap) >= required


def _mention_grounded_in_catalog(mention: str, catalog_titles: Sequence[str]) -> bool:
    if not mention or not catalog_titles:
        return False
    for title in catalog_titles:
        if _strict_catalog_mention_match(mention, title):
            return True
    return False


def _ungrounded_product_mentions(
    reply: str,
    catalog_titles: Sequence[str],
) -> List[str]:
    if not reply.strip():
        return []
    ungrounded: List[str] = []
    for mention in _extract_product_mentions(reply):
        if not _mention_grounded_in_catalog(mention, catalog_titles):
            ungrounded.append(mention)
    for marker in _KNOWN_INVENTION_MARKERS:
        if _norm(marker) in _norm(reply) and not _mention_grounded_in_catalog(marker, catalog_titles):
            if marker not in ungrounded:
                ungrounded.append(marker)
    return ungrounded


def _seasonal_availability_invented(
    reply: str,
    inbound_text: str,
    catalog_titles: Sequence[str],
) -> bool:
    if not is_seasonal_availability_ask(inbound_text):
        return False
    subject = extract_seasonal_product_subject(inbound_text)
    if not subject or seasonal_subject_in_catalog(subject, catalog_titles):
        return False
    norm_reply = _norm(reply)
    if _SEASONAL_DATE_CLAIM_RE.search(norm_reply):
        return True
    if _norm(subject) in norm_reply and any(
        tok in norm_reply
        for tok in ("متوفر", "متاح", "يجي", "يوصل", "ينزل", "قريب", "بعد")
    ):
        return True
    return False


def _rewrite_grounded_reply(
    *,
    catalog_titles: Sequence[str],
    inbound_text: str,
    category_hint: str = "",
) -> str:
    seasonal_subject = ""
    if is_seasonal_availability_ask(inbound_text):
        seasonal_subject = extract_seasonal_product_subject(inbound_text)
    return build_uncertain_catalog_reply(
        category_hint=category_hint,
        seasonal_subject=seasonal_subject,
        catalog_titles=catalog_titles,
        greeting="وعليكم السلام،" if "السلام" in (inbound_text or "") else "",
    )


def apply_catalog_product_grounding_guard(
    *,
    reply: str,
    inbound_text: str = "",
    category_hint: str = "",
    availability_context: Optional[Dict[str, Any]] = None,
    executor_products: Optional[Sequence[Dict[str, Any]]] = None,
    evidence: Optional[ProductClaimGroundingEvidence] = None,
    chosen_path: str = "",
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    order_state: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    intent: Any = None,
) -> CatalogProductGroundingGuardResult:
    mode = catalog_product_grounding_guard_mode()
    original = str(reply or "")

    if mode == "off":
        return CatalogProductGroundingGuardResult(reply=original, action="disabled")

    if not original.strip():
        return CatalogProductGroundingGuardResult(reply=original, action="allowed")

    path = str(chosen_path or "").strip()
    if path in _DETERMINISTIC_ALLOW_PATHS:
        return CatalogProductGroundingGuardResult(reply=original, action="allowed")

    try:
        from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: PLC0415
            resolve_current_turn_social_non_commerce,
        )

        last_question = str(getattr(order_state, "last_question_asked", "") or "")
        current_turn = resolve_current_turn_social_non_commerce(
            inbound_text or "",
            intent=intent,
            state=order_state,
            inbound_metadata=inbound_metadata,
            last_question=last_question,
        )
        if current_turn.matched:
            logger.info(
                "[CATALOG_PRODUCT_GROUNDING_GUARD] allow_social_noncommerce "
                "tenant=%s conv=%s category=%s reason=%s",
                tenant_id,
                conversation_id,
                current_turn.category or "-",
                current_turn.reason or "-",
            )
            return CatalogProductGroundingGuardResult(
                reply=original,
                action="allowed_social_noncommerce",
                reason=current_turn.reason or "current_turn_social_non_commerce",
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_GROUNDING_GUARD] social_non_commerce_probe_failed")

    if evidence is None:
        from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: PLC0415
            build_product_claim_grounding_evidence,
        )

        evidence = build_product_claim_grounding_evidence(
            None,
            tenant_id,
            availability_context=availability_context,
            executor_products=executor_products,
            chosen_path=path,
        )

    catalog_titles = _catalog_titles_from_evidence(evidence, executor_products)
    ungrounded = _ungrounded_product_mentions(original, catalog_titles)
    seasonal_invented = _seasonal_availability_invented(
        original, inbound_text, catalog_titles,
    )

    if not ungrounded and not seasonal_invented:
        return CatalogProductGroundingGuardResult(reply=original, action="allowed")

    rewritten = _rewrite_grounded_reply(
        catalog_titles=catalog_titles,
        inbound_text=inbound_text,
        category_hint=category_hint,
    )

    if mode == "shadow":
        logger.info(
            "[CATALOG_PRODUCT_GROUNDING_GUARD] shadow tenant=%s conv=%s "
            "ungrounded=%s seasonal_invented=%s",
            tenant_id,
            conversation_id,
            ungrounded,
            seasonal_invented,
        )
        return CatalogProductGroundingGuardResult(
            reply=original,
            action="shadow",
            would_rewrite=True,
            reason="ungrounded_product_names",
            ungrounded_mentions=tuple(ungrounded),
            shadow_mode=True,
        )

    logger.info(
        "[CATALOG_PRODUCT_GROUNDING_GUARD] enforce tenant=%s conv=%s "
        "ungrounded=%s seasonal_invented=%s",
        tenant_id,
        conversation_id,
        ungrounded,
        seasonal_invented,
    )
    return CatalogProductGroundingGuardResult(
        reply=rewritten,
        action="rewritten",
        replaced=True,
        reason="ungrounded_product_names",
        ungrounded_mentions=tuple(ungrounded),
    )


__all__ = [
    "CatalogProductGroundingGuardResult",
    "apply_catalog_product_grounding_guard",
    "catalog_product_grounding_guard_mode",
]
