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
from modules.ai.brain.turn_owner_contract import (
    POSTPROCESS_CATALOG_GROUNDING,
    get_turn_owner_contract,
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
    "kb_availability_facts",
})

_CATALOG_REWRITE_BLOCKED_TOPICS = frozenset({
    "health_advisory_product_safety",
    "cold_shipping_inquiry",
    "shipping_inquiry",
    "shipping_eta",
    "storefront_self_checkout",
    "order_tracking",
    "latest_order_summary",
    "order_history",
    "order_reference_list",
})

_ORDER_OWNER_INTENTS = frozenset({
    "track_order",
    "latest_order_summary",
    "order_history_count",
    "order_reference_list",
})

_HONEY_PRODUCT_MENTION_RE = re.compile(
    r"عسل\s+[\u0600-\u06FFa-zA-Z]+(?:\s+[\u0600-\u06FFa-zA-Z]+){0,3}",
    re.UNICODE,
)

_BULLET_OR_NUMBERED_RE = re.compile(
    r"^(?:[-•*]|\d+[\.\)])\s*(.+)$",
    re.MULTILINE | re.UNICODE,
)

_RECOMMEND_SPLIT_RE = re.compile(
    r"\s*(?:أو|او)\s*",
    re.UNICODE,
)

_RECOMMEND_LEAD_RE = re.compile(
    r"^(?:أنصحك|انصحك|ننصحك|نصيحة)\s*[:،]?\s*",
    re.UNICODE | re.IGNORECASE | re.MULTILINE,
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


def _catalog_rewrite_blocked_by_current_turn(
    inbound_metadata: Optional[Dict[str, Any]],
) -> bool:
    contract = get_turn_owner_contract(inbound_metadata=inbound_metadata)
    if contract is not None and (
        contract.block_catalog_push
        or contract.blocks(POSTPROCESS_CATALOG_GROUNDING)
    ):
        return True
    meta = dict(inbound_metadata or {})
    topic = str(meta.get("decision_topic") or meta.get("topic") or "").strip()
    if topic in _CATALOG_REWRITE_BLOCKED_TOPICS:
        return True
    if meta.get("block_catalog_push"):
        return True
    if meta.get("potential_payment_document") or meta.get("payment_receipt_turn"):
        return True
    try:
        from core.payment_document_signals import metadata_has_potential_payment_document  # noqa: PLC0415

        if metadata_has_potential_payment_document(meta):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    try:
        from modules.ai.brain.commerce.payment_evidence_turn_route import (  # noqa: PLC0415
            inbound_metadata_has_payment_evidence,
        )

        if inbound_metadata_has_payment_evidence(meta):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok
        pass
    return False


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
    for phrase in _extract_recommend_conjuncts(reply or ""):
        if phrase and phrase not in seen and len(phrase) >= 3:
            seen.add(phrase)
            mentions.append(phrase)
    return mentions


def _extract_or_pairs(reply: str) -> List[str]:
    """Short A-or-B noun phrases. Used only when mixed with catalog titles."""
    out: List[str] = []
    for raw_line in str(reply or "").splitlines() or [str(reply or "")]:
        line = raw_line.strip()
        if not line or len(line) > 120 or not _RECOMMEND_SPLIT_RE.search(line):
            continue
        line = _RECOMMEND_LEAD_RE.sub("", line, count=1)
        line = re.sub(r"[🎁✨👗🪷🛒.]+$", "", line).strip()
        parts = [p.strip(" .،,*-•") for p in _RECOMMEND_SPLIT_RE.split(line) if p.strip()]
        if not (2 <= len(parts) <= 3):
            continue
        if any(len(part) > 40 for part in parts):
            continue
        cleaned: List[str] = []
        for part in parts:
            part = re.sub(r"^(?:ب|ل|ك)\s*", "", part).strip()
            part = re.sub(r"\s+كهدية$", "", part).strip()
            if 2 < len(part) <= 40:
                cleaned.append(part)
        if 2 <= len(cleaned) <= 3:
            for part in cleaned:
                if part not in out:
                    out.append(part)
    return out


def _extract_recommend_conjuncts(reply: str) -> List[str]:
    text = str(reply or "").strip()
    if not text or not _RECOMMEND_LEAD_RE.search(text):
        return []
    return _extract_or_pairs(text)


def _strip_ungrounded_mentions(reply: str, ungrounded: Sequence[str]) -> str:
    working = str(reply or "")
    for mention in ungrounded:
        token = str(mention or "").strip()
        if not token:
            continue
        pattern = re.compile(
            rf"(?:\s*(?:أو|او|,|،)\s*)?{re.escape(token)}",
            re.UNICODE,
        )
        working = pattern.sub("", working)
    working = re.sub(r"[ \t]{2,}", " ", working)
    working = re.sub(r"(?:أو|او)\s*(?:أو|او)", "أو", working)
    working = re.sub(r"^(?:أو|او)\s+", "", working)
    working = re.sub(r"\s+(?:أو|او)\s*$", "", working)
    working = re.sub(r"\s+([.،,])", r"\1", working)
    return working.strip()


def _distinctive_tokens(text: str) -> Set[str]:
    from modules.ai.knowledge.product_matcher import normalize_arabic, tokenize  # noqa: PLC0415

    stop = frozenset({
        "منتج", "product", "عسل", "حجم", "وزن", "كيلو", "نصف", "ربع", "جرام",
        "البلدي", "بلدي", "طبيعي", "اصلي", "أصلي",
        "مميز", "جميل", "رائع", "حلو", "مناسب", "جديد", "خاص",
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
    if len(mention_toks) == 1:
        return bool(overlap)
    required = 2 if len(title_toks) >= 2 else 1
    return len(overlap) >= required


def _mention_grounded_in_catalog(mention: str, catalog_titles: Sequence[str]) -> bool:
    if not mention or not catalog_titles:
        return False
    for title in catalog_titles:
        if _strict_catalog_mention_match(mention, title):
            return True
    return False


def _looks_like_product_option_list(reply: str) -> bool:
    text = str(reply or "")
    if not text.strip():
        return False
    if _BULLET_OR_NUMBERED_RE.search(text):
        return True
    return len(_extract_or_pairs(text)) >= 2


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

    if _catalog_rewrite_blocked_by_current_turn(inbound_metadata):
        return CatalogProductGroundingGuardResult(
            reply=original,
            action="allowed_catalog_push_blocked",
        )

    intent_name = str(getattr(intent, "name", "") or "").strip()
    if not intent_name:
        intent_name = str(dict(inbound_metadata or {}).get("intent") or "").strip()
    if intent_name in _ORDER_OWNER_INTENTS:
        return CatalogProductGroundingGuardResult(
            reply=original,
            action="allowed_order_evidence_owner",
        )

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
    meta = dict(inbound_metadata or {})
    for extra in (
        list(meta.get("catalog_reasoning_titles") or []),
        list(meta.get("claimable_values") or []),
    ):
        for title in extra:
            token = str(title or "").strip()
            if token and token not in catalog_titles:
                catalog_titles.append(token)
    ungrounded = _ungrounded_product_mentions(original, catalog_titles)
    or_parts = _extract_or_pairs(original)
    grounded_or = [
        part for part in or_parts
        if _mention_grounded_in_catalog(part, catalog_titles)
    ]
    ungrounded_or = [part for part in or_parts if part not in grounded_or]
    if grounded_or and ungrounded_or:
        for part in ungrounded_or:
            if part not in ungrounded:
                ungrounded.append(part)
    seasonal_invented = _seasonal_availability_invented(
        original, inbound_text, catalog_titles,
    )

    if not ungrounded and not seasonal_invented:
        return CatalogProductGroundingGuardResult(reply=original, action="allowed")

    invention_marker_hit = any(
        _norm(marker) in _norm(original)
        and not _mention_grounded_in_catalog(marker, catalog_titles)
        for marker in _KNOWN_INVENTION_MARKERS
    )
    if (
        ungrounded
        and not seasonal_invented
        and not invention_marker_hit
        and not _looks_like_product_option_list(original)
    ):
        return CatalogProductGroundingGuardResult(
            reply=original,
            action="allowed_non_option_prose",
            reason="ungrounded_tokens_not_product_option_list",
            ungrounded_mentions=tuple(ungrounded),
        )

    contract = get_turn_owner_contract(inbound_metadata=inbound_metadata)
    try:
        from modules.ai.brain.commerce.inbound_fragment_guard import (  # noqa: PLC0415
            should_block_catalog_grounding_fallback,
        )

        meta = dict(inbound_metadata or {})
        _block_catalog, _block_reason = should_block_catalog_grounding_fallback(
            inbound_text=inbound_text,
            inbound_metadata=meta,
            intent=intent,
            decision_topic=str(
                meta.get("decision_topic") or meta.get("topic") or "",
            ),
            protected_final_reply=bool(
                contract is not None and contract.protected_final_reply
            ),
        )
        if _block_catalog:
            logger.info(
                "[CATALOG_PRODUCT_GROUNDING_GUARD] blocked_catalog_containment "
                "tenant=%s conv=%s reason=%s inbound=%r",
                tenant_id,
                conversation_id,
                _block_reason or "-",
                (inbound_text or "")[:80],
            )
            return CatalogProductGroundingGuardResult(
                reply=original,
                action="blocked_catalog_containment",
                reason=_block_reason or "catalog_containment",
                ungrounded_mentions=tuple(ungrounded),
                would_rewrite=True,
            )
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_GROUNDING_GUARD] catalog_containment_probe_failed")

    stripped = _strip_ungrounded_mentions(original, ungrounded)
    extracted = _extract_product_mentions(original)
    mention_pool = list(extracted)
    for part in _extract_or_pairs(original):
        if part not in mention_pool:
            mention_pool.append(part)
    mixed_grounded = any(
        mention not in ungrounded
        and _mention_grounded_in_catalog(mention, catalog_titles)
        for mention in mention_pool
    )
    if mixed_grounded and stripped.strip() and stripped.strip() != original.strip():
        rewritten = stripped
    else:
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
    try:
        from modules.ai.brain.commerce.commerce_entry_catalog_delivery import (  # noqa: PLC0415
            is_catalog_confirmation_bot_reply,
            pin_pending_catalog_send,
        )

        if is_catalog_confirmation_bot_reply(rewritten):
            pin_pending_catalog_send(order_state, source="catalog_confirmation")
    except Exception:  # noqa: BLE001  # noqa: silent-ok — pending pin is best-effort
        logger.debug("[CATALOG_PRODUCT_GROUNDING_GUARD] pending_catalog_pin_failed")
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
