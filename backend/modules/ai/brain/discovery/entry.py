"""
discovery/entry.py
──────────────────
Phase 1 — unified product discovery entry point.

Consolidates scattered text-pattern / start-order / browse routing from
``DefaultDecisionEngine`` into one classifier + router. Reuses existing
guards and policies; does not invent new catalog behavior.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..decision.actions import ACTION_SEARCH_PRODUCTS
from ..types import (
    BrainContext,
    Decision,
    INTENT_ASK_PRICE,
    INTENT_ASK_PRODUCT,
    INTENT_NEED_BASED_PRODUCT_ADVICE,
    INTENT_WHO_ARE_YOU,
)

logger = logging.getLogger("nahla.brain.discovery.entry")

START_ORDER_BARE = "start_order_bare"
GLOBAL_BROWSE = "global_browse"
CATEGORY_BROWSE = "category_browse"
TOP_PRODUCTS = "top_products"
SHOW_MORE = "show_more"
PRODUCT_SPECIFIC = "product_specific"
NO_DISCOVERY = "no_discovery"

_SHOW_MORE_PATTERNS = (
    "باقي الخيارات",
    "وريني باقي",
    "خيارات اكثر",
    "خيارات أكثر",
    "more options",
    "show more",
)

_TOP_SELLER_PATTERNS = (
    "الاكثر مبيعا",
    "اكثر مبيعا",
    "الاكثر مبيعًا",
    "اكثر مبيعًا",
    "الاكثر طلبا",
    "اكثر طلبا",
    "الاكثر طلبًا",
    "best sellers",
    "top products",
)

_DIA = r"[\u064B-\u065F\u0640]"

_EMBEDDED_ORDER_PRODUCT_RE = re.compile(
    r"(?:"
    r"(?:ابغ[ىي]|أبغ[ىي]|اب[ىي]|أب[ىي]|اريد|أريد|بغيت|ودي|حاب|حابب|بدي)"
    r"\s+(?:اشتري|أشتري|اطلب|أطلب|order|buy)"
    r"\s+"
    r")(.{2,80})",
    re.UNICODE | re.IGNORECASE,
)


def _normalize_ar(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(_DIA, "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    t = re.sub(r"[؟?!.,؛:]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass(frozen=True)
class DiscoveryEntryDecision:
    matched: bool
    entry_type: str
    source: str
    query: Optional[str]
    category_scope: Optional[str]
    reason: str

    @classmethod
    def no_match(cls, reason: str = "not_discovery") -> "DiscoveryEntryDecision":
        return cls(
            matched=False,
            entry_type=NO_DISCOVERY,
            source="",
            query=None,
            category_scope=None,
            reason=reason,
        )


def _extract_embedded_order_product_query(message: str) -> str:
    """Mid-message commerce recovery («… أبغى أشتري عسل»)."""
    from ..commerce.start_order_verb_guard import _has_product_substance  # noqa: PLC0415

    raw = (message or "").strip()
    m = _EMBEDDED_ORDER_PRODUCT_RE.search(raw)
    if not m:
        return ""
    candidate = (m.group(1) or "").strip(" ؟?!.,،")
    if candidate and _has_product_substance(candidate):
        return candidate
    return ""


def extract_order_product_query(ctx: BrainContext) -> str:
    """Product name from order-start phrasing — platform-wide extraction."""
    from ..commerce.catalog_query_normalization import (  # noqa: PLC0415
        extract_english_order_product_query,
    )
    from ..commerce.contact_escalation import (  # noqa: PLC0415
        is_branch_list_request,
        is_branch_location_order_tail,
    )
    from ..commerce.start_order_verb_guard import (  # noqa: PLC0415
        extract_start_order_product_query,
        is_bare_start_order_phrase,
    )
    from ..order_context_gate import is_order_fulfillment_product_query  # noqa: PLC0415
    from ..product_discovery_gate import has_inquiry_phrasing  # noqa: PLC0415

    msg = ctx.message or ""
    if is_bare_start_order_phrase(msg):
        return ""

    extracted = extract_start_order_product_query(msg)
    if extracted:
        if (
            not is_order_fulfillment_product_query(extracted)
            and not has_inquiry_phrasing(msg)
            and not is_branch_location_order_tail(extracted)
            and not is_branch_list_request(msg)
        ):
            return extracted

    en_extracted = extract_english_order_product_query(msg)
    if en_extracted and (
        not is_order_fulfillment_product_query(en_extracted)
        and not has_inquiry_phrasing(msg)
        and not is_branch_location_order_tail(en_extracted)
        and not is_branch_list_request(msg)
    ):
        return en_extracted

    return _extract_embedded_order_product_query(msg)


def _is_price_turn(ctx: BrainContext) -> bool:
    """Price asks are not discovery entry turns."""
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    msg = ctx.message or ""
    if intent_name in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT):
        try:
            from ..commerce.price_turn_classifier import (  # noqa: PLC0415
                PriceTurnKind,
                classify_price_turn,
            )

            if classify_price_turn(ctx) == PriceTurnKind.PRODUCT_PRICE_ASK:
                return True
        except Exception:
            logger.exception("[DISCOVERY_ENTRY] price_turn_classifier_failed")
    try:
        from ..product_discovery_gate import _extract_price_subject  # noqa: PLC0415

        if _extract_price_subject(msg):
            return True
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] price_subject_extract_failed")
    return False


def _discovery_suppressed(ctx: BrainContext) -> Optional[str]:
    """Return suppression reason or ``None`` when discovery may classify."""
    intent_name = str(getattr(ctx.intent, "name", "") or "")
    msg = ctx.message or ""

    if intent_name == INTENT_WHO_ARE_YOU:
        return "persona_identity"

    try:
        from ..intent import rules  # noqa: PLC0415

        matched = rules.match(msg)
        if matched is not None and matched.name == INTENT_WHO_ARE_YOU:
            return "persona_identity"
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] identity_rules_match_failed")

    norm = _normalize_ar(msg)
    if norm in {"من انت", "من انت؟"} or re.match(r"^من\s+انت", norm):
        return "persona_identity"

    order_prep = getattr(ctx.state, "order_prep", None)
    try:
        from ..commerce.checkout_slot_contact_guard import (  # noqa: PLC0415
            message_fulfills_checkout_slot,
        )

        if message_fulfills_checkout_slot(msg, order_prep=order_prep):
            return "checkout_slot"
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] checkout_slot_probe_failed")

    try:
        from ..order_context_gate import should_block_product_discovery  # noqa: PLC0415

        if should_block_product_discovery(ctx, msg):
            return "active_fulfillment"
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] fulfillment_lock_probe_failed")

    return None


def _is_show_more_request(message: str) -> bool:
    norm = _normalize_ar(message or "")
    if any(p in norm for p in _SHOW_MORE_PATTERNS):
        return True
    try:
        from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: PLC0415
            browse_alternatives_requested,
        )

        return bool(browse_alternatives_requested(message or ""))
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] show_more_probe_failed")
        return False


def _is_top_seller_request(message: str) -> bool:
    norm = _normalize_ar(message or "")
    return any(p in norm for p in _TOP_SELLER_PATTERNS)


def _resolve_category_scope(
    ctx: BrainContext,
    *,
    source: str,
) -> Optional[str]:
    try:
        from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            active_category_from_state,
            resolve_browse_category_scope,
        )

        return resolve_browse_category_scope(
            ctx.message or "",
            "",
            active_category=active_category_from_state(ctx.state),
            source=source,
        )
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] resolve_category_scope_failed")
        return None


def _category_browse_entry(ctx: BrainContext) -> Optional[DiscoveryEntryDecision]:
    from ..product_discovery_gate import (  # noqa: PLC0415
        extract_types_overview_query,
        has_types_overview_ask,
        is_generic_category_noun,
    )

    msg = ctx.message or ""
    try:
        from ..commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )

        if global_availability_browse_requested(msg):
            return None
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] global_browse_probe_failed")

    if has_types_overview_ask(msg):
        subject = extract_types_overview_query(msg)
        if subject and is_generic_category_noun(subject):
            scope = _resolve_category_scope(ctx, source="category_browse") or subject
            return DiscoveryEntryDecision(
                matched=True,
                entry_type=CATEGORY_BROWSE,
                source="category_browse",
                query=scope,
                category_scope=scope,
                reason="types/options overview for category noun",
            )

    try:
        from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
            active_category_from_state,
            extract_browse_category_scope,
            is_generic_category_browse,
        )

        scope = extract_browse_category_scope(msg, "")
        locked = active_category_from_state(ctx.state)
        if scope and is_generic_category_browse(msg, scope):
            return DiscoveryEntryDecision(
                matched=True,
                entry_type=CATEGORY_BROWSE,
                source="category_browse",
                query=scope,
                category_scope=scope,
                reason="generic category options browse",
            )
        if locked and is_generic_category_browse(msg, ""):
            return DiscoveryEntryDecision(
                matched=True,
                entry_type=CATEGORY_BROWSE,
                source="category_browse",
                query=locked,
                category_scope=locked,
                reason="session-locked category browse",
            )
    except Exception:
        logger.exception("[DISCOVERY_ENTRY] category_browse_entry_failed")
    return None


def _global_browse_entry(ctx: BrainContext) -> Optional[DiscoveryEntryDecision]:
    msg = ctx.message or ""
    try:
        from ..commerce.product_breadth_policy import (  # noqa: PLC0415
            global_availability_browse_requested,
        )
        from ..product_discovery_gate import has_explicit_broad_browse_request  # noqa: PLC0415

        is_global = global_availability_browse_requested(msg) or has_explicit_broad_browse_request(
            msg,
        )
    except Exception:  # noqa: BLE001
        is_global = False

    if not is_global:
        return None

    scope = _resolve_category_scope(ctx, source="top_products")
    return DiscoveryEntryDecision(
        matched=True,
        entry_type=GLOBAL_BROWSE,
        source="top_products",
        query=scope or "",
        category_scope=scope,
        reason="global inventory / catalog browse",
    )


def resolve_discovery_entry(ctx: BrainContext) -> DiscoveryEntryDecision:
    """
    Unified classifier for discovery entry turns.

    Returns ``matched=False`` when the turn is not a discovery entry or must
    not be hijacked (identity, checkout slot answers, fulfillment lock).
    """
    suppressed = _discovery_suppressed(ctx)
    if suppressed:
        return DiscoveryEntryDecision.no_match(suppressed)

    if _is_price_turn(ctx):
        return DiscoveryEntryDecision.no_match("price_turn")

    msg = ctx.message or ""
    intent_name = str(getattr(ctx.intent, "name", "") or "")

    if _is_show_more_request(msg):
        from ..product_discovery_gate import _has_prior_browse_context  # noqa: PLC0415

        if _has_prior_browse_context(ctx):
            return DiscoveryEntryDecision(
                matched=True,
                entry_type=SHOW_MORE,
                source="show_more",
                query=str(getattr(ctx.state, "last_browse_query", "") or "") or None,
                category_scope=None,
                reason="show more product options",
            )

    from ..commerce.start_order_verb_guard import is_bare_start_order_phrase  # noqa: PLC0415

    if is_bare_start_order_phrase(msg):
        return DiscoveryEntryDecision(
            matched=True,
            entry_type=START_ORDER_BARE,
            source="top_products_start_order",
            query="",
            category_scope=None,
            reason="bare start-order opener — no product query",
        )

    product_query = extract_order_product_query(ctx)
    if (
        product_query
        and intent_name
        not in {
            INTENT_NEED_BASED_PRODUCT_ADVICE,
            "need_based_product_advice",
            "solution_seeking_commerce",
        }
        and getattr(ctx, "goal_regimen_bundle", None) is None
    ):
        return DiscoveryEntryDecision(
            matched=True,
            entry_type=PRODUCT_SPECIFIC,
            source="order_product_query",
            query=product_query,
            category_scope=None,
            reason=f"order phrase product query {product_query!r}",
        )

    category_entry = _category_browse_entry(ctx)
    if category_entry is not None:
        return category_entry

    if _is_top_seller_request(msg):
        scope = _resolve_category_scope(ctx, source="top_products")
        return DiscoveryEntryDecision(
            matched=True,
            entry_type=TOP_PRODUCTS,
            source="top_products",
            query=scope or "",
            category_scope=scope,
            reason="top sellers / best sellers browse",
        )

    global_entry = _global_browse_entry(ctx)
    if global_entry is not None:
        return global_entry

    return DiscoveryEntryDecision.no_match("not_discovery")


def route_discovery_entry(
    ctx: BrainContext,
    entry: DiscoveryEntryDecision,
    *,
    facts: Any,
    product_discovery_blocked: Callable[[str], bool],
    fulfillment_locked_fallback: Callable[[], Optional[Decision]],
    block_stale_resume: Callable[[str], bool],
    is_commerce_blocked: Callable[[BrainContext], bool],
) -> Optional[Decision]:
    """Map a discovery entry decision to an engine ``Decision``."""
    if not entry.matched:
        return None

    if not getattr(facts, "has_products", False):
        return None

    if is_commerce_blocked(ctx):
        return None

    source = str(entry.source or "").strip().lower()

    if product_discovery_blocked(source):
        fb = fulfillment_locked_fallback()
        if fb is not None:
            return fb
        if entry.entry_type in {
            START_ORDER_BARE,
            TOP_PRODUCTS,
            GLOBAL_BROWSE,
            SHOW_MORE,
        }:
            from ..product_discovery_gate import clarify_instead_of_top_products  # noqa: PLC0415

            return clarify_instead_of_top_products(
                ctx,
                reason="weak_or_unknown_intent",
            )
        if entry.entry_type == PRODUCT_SPECIFIC:
            return None
        return None

    if entry.entry_type == SHOW_MORE:
        if block_stale_resume("show_more"):
            return None
        logger.info(
            "[DISCOVERY_ENTRY] route=%s source=%s offset=%d pool=%d tenant=%s",
            entry.entry_type,
            source,
            int(getattr(ctx.state, "catalog_browse_offset", 0) or 0),
            len(getattr(ctx.state, "catalog_browse_pool", None) or []),
            getattr(ctx, "tenant_id", None),
        )
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": str(entry.query or ""),
                "source": "show_more",
            },
            reason=entry.reason,
            confidence=0.90,
        )

    scope = (entry.category_scope or "").strip()
    query = str(entry.query or "").strip()

    if scope and entry.entry_type in {TOP_PRODUCTS, GLOBAL_BROWSE, CATEGORY_BROWSE}:
        logger.info(
            "[DISCOVERY_ENTRY] route=%s source=category_browse query=%r tenant=%s",
            entry.entry_type,
            scope,
            getattr(ctx, "tenant_id", None),
        )
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": scope, "source": "category_browse"},
            reason=(
                f"discovery entry {entry.entry_type} — "
                f"category-scoped browse search {scope!r}"
            ),
            confidence=0.93,
        )

    if entry.entry_type == START_ORDER_BARE:
        logger.info(
            "[DISCOVERY_ENTRY] route=%s source=%s tenant=%s",
            entry.entry_type,
            source,
            getattr(ctx, "tenant_id", None),
        )
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "", "source": "top_products_start_order"},
            reason="start_order with no product query — bare start-order opener",
            confidence=0.85,
        )

    if entry.entry_type == PRODUCT_SPECIFIC:
        logger.info(
            "[DISCOVERY_ENTRY] route=%s query=%r tenant=%s",
            entry.entry_type,
            query,
            getattr(ctx, "tenant_id", None),
        )
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": query, "after_search": "propose_order"},
            reason=entry.reason,
            confidence=0.88,
        )

    if entry.entry_type in {TOP_PRODUCTS, GLOBAL_BROWSE}:
        logger.info(
            "[DISCOVERY_ENTRY] route=%s source=%s tenant=%s msg=%r",
            entry.entry_type,
            source,
            getattr(ctx, "tenant_id", None),
            (ctx.message or "")[:60],
        )
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": "", "source": source or "top_products"},
            reason=entry.reason,
            confidence=0.92,
        )

    if entry.entry_type == CATEGORY_BROWSE:
        logger.info(
            "[DISCOVERY_ENTRY] route=%s query=%r tenant=%s",
            entry.entry_type,
            query or scope,
            getattr(ctx, "tenant_id", None),
        )
        return Decision(
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": query or scope, "source": "category_browse"},
            reason=entry.reason,
            confidence=0.93,
        )

    return None


__all__ = [
    "CATEGORY_BROWSE",
    "GLOBAL_BROWSE",
    "NO_DISCOVERY",
    "PRODUCT_SPECIFIC",
    "SHOW_MORE",
    "START_ORDER_BARE",
    "TOP_PRODUCTS",
    "DiscoveryEntryDecision",
    "extract_order_product_query",
    "resolve_discovery_entry",
    "route_discovery_entry",
]
