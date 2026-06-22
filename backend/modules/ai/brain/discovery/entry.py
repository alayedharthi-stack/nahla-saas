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

from ..decision.actions import ACTION_CLARIFY, ACTION_SEARCH_PRODUCTS
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
    "top sellers",
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
    catalog_group_slug: Optional[str] = None
    catalog_group_id: Optional[int] = None

    @classmethod
    def no_match(cls, reason: str = "not_discovery") -> "DiscoveryEntryDecision":
        return cls(
            matched=False,
            entry_type=NO_DISCOVERY,
            source="",
            query=None,
            category_scope=None,
            reason=reason,
            catalog_group_slug=None,
            catalog_group_id=None,
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
            try:
                from ..catalog.catalog_browse_turn_policy import is_catalog_browse_message  # noqa: PLC0415

                if not is_catalog_browse_message(msg, intent_name=intent_name):
                    return "active_fulfillment"
            except Exception:  # noqa: BLE001
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


def _discovery_category_followup_entry(ctx: BrainContext) -> Optional[DiscoveryEntryDecision]:
    """Short category answer while commerce objective remains discovery."""
    from ..commerce.commerce_objective import (  # noqa: PLC0415
        COMMERCE_OBJECTIVE_DISCOVERY,
        get_commerce_objective,
    )
    from ..commerce.commerce_browse_category_guard import (  # noqa: PLC0415
        _is_valid_scope_token,
        extract_browse_category_scope,
    )

    if get_commerce_objective(ctx.state) != COMMERCE_OBJECTIVE_DISCOVERY:
        return None

    msg = (ctx.message or "").strip()
    if not msg:
        return None

    scope = extract_browse_category_scope(msg, "")
    if not scope or not _is_valid_scope_token(scope):
        return None

    words = [w for w in msg.split() if w.strip()]
    if len(words) > 3:
        return None

    return DiscoveryEntryDecision(
        matched=True,
        entry_type=CATEGORY_BROWSE,
        source="category_browse",
        query=scope,
        category_scope=scope,
        reason="discovery objective category follow-up",
    )


def _apply_catalog_group_scope(
    ctx: BrainContext,
    entry: DiscoveryEntryDecision,
) -> DiscoveryEntryDecision:
    db = getattr(ctx, "_db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    if db is None or tenant_id is None:
        return entry
    try:
        from ..catalog.catalog_browse_scope_resolver import (  # noqa: PLC0415
            active_catalog_group_slug_from_state,
            resolve_browse_scope,
            stamp_catalog_group_session,
        )
        from ..commerce.commerce_browse_category_guard import active_category_from_state  # noqa: PLC0415

        resolution = resolve_browse_scope(
            db,
            int(tenant_id),
            ctx.message or "",
            str(entry.query or entry.category_scope or ""),
            active_group_slug=active_catalog_group_slug_from_state(ctx.state),
            active_category=active_category_from_state(ctx.state),
        )
        if not resolution.matched:
            try:
                from ..catalog.catalog_intelligence_telemetry import (  # noqa: PLC0415
                    emit_catalog_intelligence_event,
                )

                if entry.entry_type == GLOBAL_BROWSE:
                    emit_catalog_intelligence_event(
                        "global_browse",
                        tenant_id=int(tenant_id),
                        group=None,
                        reason="no_scope_match",
                        entry_type=entry.entry_type,
                    )
            except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break routing
                pass
            return entry
        try:
            from ..catalog.catalog_intelligence_telemetry import (  # noqa: PLC0415
                emit_catalog_intelligence_event,
            )

            emit_catalog_intelligence_event(
                "browse_scope",
                tenant_id=int(tenant_id),
                group=resolution.group_slug,
                group_id=resolution.group_id,
                match_source=resolution.match_source,
                entry_type=entry.entry_type,
            )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — telemetry must not break routing
            pass
        stamp_catalog_group_session(ctx.state, resolution)
        return DiscoveryEntryDecision(
            matched=entry.matched,
            entry_type=entry.entry_type,
            source=entry.source,
            query=resolution.scope_query or entry.query,
            category_scope=resolution.group_label or entry.category_scope,
            reason=f"{entry.reason}; catalog_group={resolution.group_slug}",
            catalog_group_slug=resolution.group_slug,
            catalog_group_id=resolution.group_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "[DISCOVERY_ENTRY] catalog_group_scope_failed tenant=%s",
            getattr(ctx, "tenant_id", None),
        )
        return entry


def resolve_discovery_entry(ctx: BrainContext) -> DiscoveryEntryDecision:
    """
    Unified classifier for discovery entry turns.

    Returns ``matched=False`` when the turn is not a discovery entry or must
    not be hijacked (identity, checkout slot answers, fulfillment lock).
    """
    entry = _classify_discovery_entry(ctx)
    if not entry.matched:
        return entry
    return _apply_catalog_group_scope(ctx, entry)


def _classify_discovery_entry(ctx: BrainContext) -> DiscoveryEntryDecision:
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
    _global_browse_msg = False
    try:
        from ..commerce.product_breadth_policy import global_availability_browse_requested  # noqa: PLC0415

        _global_browse_msg = global_availability_browse_requested(msg)
    except Exception:  # noqa: BLE001
        _global_browse_msg = False
    if (
        product_query
        and not _global_browse_msg
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

    followup_entry = _discovery_category_followup_entry(ctx)
    if followup_entry is not None:
        return followup_entry

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


def _load_merchant_discovery_settings(ctx: BrainContext):
    from ..commerce.merchant_discovery_settings import load_merchant_discovery_settings  # noqa: PLC0415

    merchant_context = getattr(ctx, "merchant_context", None)
    if isinstance(merchant_context, dict) and merchant_context.get("discovery_settings"):
        return load_merchant_discovery_settings(merchant_context)

    db = getattr(ctx, "_db", None)
    tenant_id = getattr(ctx, "tenant_id", None)
    if db is not None and tenant_id is not None:
        try:
            from services.merchant_discovery_settings_service import load_settings_for_brain  # noqa: PLC0415

            return load_settings_for_brain(db, int(tenant_id))
        except Exception:
            logger.exception(
                "[DISCOVERY_STRATEGY] discovery_settings_db_load_failed tenant=%s",
                tenant_id,
            )

    return load_merchant_discovery_settings(getattr(ctx, "tenant_context", None))


def _resolve_strategy_layer(
    ctx: BrainContext,
    entry: DiscoveryEntryDecision,
    facts: Any,
) -> tuple[str, Any, dict]:
    from ..commerce.commerce_objective import update_commerce_objective  # noqa: PLC0415
    from ..commerce.discovery_strategy import (  # noqa: PLC0415
        DiscoveryMode,
        build_catalog_context_snapshot,
        resolve_discovery_strategy,
        strategy_to_decision_args,
    )
    from ..catalog.catalog_intelligence import CatalogIntelligence  # noqa: PLC0415
    from ..catalog.catalog_browse_scope_resolver import load_merchant_catalog_groups  # noqa: PLC0415
    from ..catalog.catalog_provider import get_catalog_provider  # noqa: PLC0415

    objective = update_commerce_objective(ctx, entry)
    settings = _load_merchant_discovery_settings(ctx)
    collection_count = 0
    db = getattr(ctx, "_db", None)
    platform = str(getattr(facts, "integration_platform", "") or "")
    if db is not None:
        try:
            provider = get_catalog_provider(
                db,
                ctx.tenant_id,
                integration_platform=platform,
            )
            collection_count = len(
                CatalogIntelligence(provider).list_collections(
                    limit=20,
                    merchant_settings=settings,
                    merchant_catalog_groups=load_merchant_catalog_groups(db, ctx.tenant_id),
                )
            )
        except Exception:
            logger.exception(
                "[DISCOVERY_STRATEGY] collection_count_failed tenant=%s",
                getattr(ctx, "tenant_id", None),
            )
    catalog_ctx = build_catalog_context_snapshot(
        facts=facts,
        collection_count=collection_count,
        has_featured=bool(settings.global_featured_product_ids()),
    )
    strategy = resolve_discovery_strategy(
        commerce_objective=objective,
        entry_type=entry.entry_type,
        catalog_context=catalog_ctx,
        merchant_settings=settings,
    )
    try:
        ctx.state.last_discovery_mode = strategy.mode.value
    except Exception:
        logger.exception("[DISCOVERY_STRATEGY] last_discovery_mode_stamp_failed")
    return objective, strategy, strategy_to_decision_args(strategy, merchant_settings=settings)


def _discovery_decision(
    ctx: BrainContext,
    entry: DiscoveryEntryDecision,
    facts: Any,
    *,
    action: str,
    args: dict,
    reason: str,
    confidence: float,
    allow_guided: bool = True,
) -> Decision:
    _objective, strategy, strategy_args = _resolve_strategy_layer(ctx, entry, facts)
    merged = dict(args or {})
    merged.update(strategy_args)
    merged["commerce_objective"] = _objective
    merged["discovery_entry_type"] = entry.entry_type
    if entry.catalog_group_slug:
        merged["catalog_group_slug"] = entry.catalog_group_slug
    if entry.catalog_group_id is not None:
        merged["catalog_group_id"] = entry.catalog_group_id

    from ..commerce.discovery_strategy import DiscoveryMode  # noqa: PLC0415

    if (
        allow_guided
        and strategy.mode == DiscoveryMode.GUIDED_DISCOVERY
        and entry.entry_type not in {SHOW_MORE, PRODUCT_SPECIFIC, GLOBAL_BROWSE}
        and action == ACTION_SEARCH_PRODUCTS
    ):
        from ..catalog.discovery_presenter import DiscoveryPresentationComposer  # noqa: PLC0415
        from ..catalog.catalog_intelligence import DiscoveryPlan  # noqa: PLC0415

        guided = DiscoveryPresentationComposer().compose(
            plan=DiscoveryPlan(
                output_kind="guided",
                guided_question=strategy.guided_question or "",
            ),
            strategy=strategy,
            entry_source=str(entry.source or ""),
            entry_type=entry.entry_type,
        )
        question = guided.text
        return Decision(
            action=ACTION_CLARIFY,
            args={
                **merged,
                "question": question,
                "topic": "discovery_guided",
            },
            reason=f"guided discovery — {reason}",
            confidence=confidence,
        )
    return Decision(action=action, args=merged, reason=reason, confidence=confidence)


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
        return _discovery_decision(
            ctx,
            entry,
            facts,
            action=ACTION_SEARCH_PRODUCTS,
            args={
                "query": str(entry.query or ""),
                "source": "show_more",
            },
            reason=entry.reason,
            confidence=0.90,
            allow_guided=False,
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
        return _discovery_decision(
            ctx,
            entry,
            facts,
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
        return _discovery_decision(
            ctx,
            entry,
            facts,
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
        return _discovery_decision(
            ctx,
            entry,
            facts,
            action=ACTION_SEARCH_PRODUCTS,
            args={"query": query, "after_search": "propose_order"},
            reason=entry.reason,
            confidence=0.88,
            allow_guided=False,
        )

    if entry.entry_type in {TOP_PRODUCTS, GLOBAL_BROWSE}:
        logger.info(
            "[DISCOVERY_ENTRY] route=%s source=%s tenant=%s msg=%r",
            entry.entry_type,
            source,
            getattr(ctx, "tenant_id", None),
            (ctx.message or "")[:60],
        )
        return _discovery_decision(
            ctx,
            entry,
            facts,
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
        return _discovery_decision(
            ctx,
            entry,
            facts,
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
