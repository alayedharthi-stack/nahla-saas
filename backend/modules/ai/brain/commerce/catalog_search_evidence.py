"""
commerce/catalog_search_evidence.py
────────────────────────────────────
Evidence gates for catalog search routing (platform-wide).

Prevents weak/spurious tokens from reaching ``search_products`` and the
deterministic ``compose_resolved_product_search_miss`` template path.
Operational truth stays deterministic; reply wording stays LLM-owned.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional

from ..decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS
from ..types import BrainContext, Decision

logger = logging.getLogger("nahla.brain.commerce.catalog_search_evidence")

# Universal discourse markers — not merchant/product vocabulary.
_DISCOURSE_ONLY_TOKENS = frozenset({
    "تمام", "ماشي", "ماش", "اوكي", "okay", "ok", "يلا", "حسنا", "حسن",
    "موافق", "خلاص", "نعم", "ايه", "اي", "ايوه", "yes", "yeah",
})

_COMMERCE_THREAD_ACTIONS = frozenset({
    "search_products",
    "narrow_choices",
    "propose_draft_order",
    "llm_reply",
    "variant_pricing",
    "ACTION_SEARCH_PRODUCTS",
    "ACTION_PROPOSE_DRAFT_ORDER",
    "ACTION_LLM_REPLY",
})

_COMMERCE_THREAD_STAGES = frozenset({
    "exploring",
    "deciding",
    "ordering",
    "checkout",
    "collecting_address",
})

_COLLECTIONS_FIRST_SOURCES = frozenset({
    "browse_catalog_groups",
    "collections_first",
    "collections_first_group",
    "global_browse",
    "top_products",
    "top_products_start_order",
})

_COLLECTIONS_FIRST_ENTRIES = frozenset({
    "global_browse",
    "start_order_bare",
})


def _norm_token(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[\u064B-\u065F\u0640]", "", t)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", t).strip()


_SHIPPING_GEO_AMBIGUITY_TOKENS = frozenset(
    _norm_token(token)
    for token in (
        "شحن", "الشحن", "توصيل", "التوصيل", "delivery", "shipping",
        "رياض", "الرياض", "جدة", "جده", "مكة", "مكه", "المدينة", "المدينه",
        "الدمام", "دمام", "القصيم", "قصيم", "الطائف", "طائف",
    )
)

_SHIPPING_GEO_AMBIGUITY_RE = re.compile(
    r"(?:شحن|توصيل|delivery|shipping|توصلون|يوصلون)",
    re.UNICODE | re.IGNORECASE,
)

_INBOUND_SHIPPING_PRICE_RE = re.compile(
    r"(?:"
    r"(?:سعر|بكم|كم\s+سعر|ب\s*كم).*(?:شحن|توصيل)|"
    r"(?:شحن|توصيل).*(?:سعر|بكم|كم)"
    r")",
    re.UNICODE | re.IGNORECASE,
)


def _query_tokens_normalized(query: str) -> list[str]:
    return [_norm_token(part) for part in (query or "").split() if part.strip()]


def _is_shipping_or_geo_ambiguous_query(query: str) -> bool:
    """True when a blocked catalog token is shipping/delivery/geo, not a product name."""
    q = (query or "").strip()
    if not q:
        return False
    norm = _norm_token(q)
    if _SHIPPING_GEO_AMBIGUITY_RE.search(norm):
        return True
    tokens = _query_tokens_normalized(q)
    if not tokens:
        return False
    if len(tokens) == 1:
        return tokens[0] in _SHIPPING_GEO_AMBIGUITY_TOKENS
    return all(token in _SHIPPING_GEO_AMBIGUITY_TOKENS for token in tokens)


def _inbound_is_shipping_price_question(text: str) -> bool:
    """True for delivery-cost asks (e.g. كم سعر الشحن؟), not product price asks."""
    norm = _norm_token(text or "")
    if not norm:
        return False
    return bool(_INBOUND_SHIPPING_PRICE_RE.search(norm))


def _is_ask_price_catalog_credible(ctx: BrainContext, query: str) -> bool:
    """ask_price with a product-like query — platform-wide, not honey-domain only."""
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name != "ask_price":
        return False
    q = (query or "").strip()
    if not q or is_discourse_only_query(q) or _is_vision_template_query(q):
        return False
    if _is_shipping_or_geo_ambiguous_query(q):
        return False
    if _inbound_is_shipping_price_question(ctx.message or ""):
        return False
    return True


def _inbound_metadata(ctx: BrainContext) -> dict:
    profile = getattr(ctx, "profile", None) or {}
    if not isinstance(profile, dict):
        return {}
    meta = profile.get("inbound_metadata")
    return meta if isinstance(meta, dict) else {}


def _recent_history(ctx: BrainContext) -> list:
    history = list(getattr(ctx, "history", None) or [])
    if history:
        return history
    state = getattr(ctx, "state", None)
    if state is None:
        return []
    return list(getattr(state, "recent_messages", None) or [])


def has_conversation_fulfillment_context(ctx: BrainContext) -> bool:
    """Structured or recent-thread evidence of order/shipping dialogue."""
    state = getattr(ctx, "state", None)
    prep = getattr(state, "order_prep", None) or {}
    if isinstance(prep, dict):
        if str(prep.get("city") or "").strip():
            return True
        if str(prep.get("short_address_code") or "").strip():
            return True
        missing = prep.get("missing_fields") or []
        if any(
            str(f or "").strip().lower() in {"city", "address", "short_address_code"}
            for f in missing
        ):
            return True
        if str(prep.get("order_status") or "").strip():
            return True

    stage = str(getattr(state, "stage", "") or "").strip().lower()
    if stage in _COMMERCE_THREAD_STAGES and stage not in {"exploring"}:
        return True

    try:
        from .product_media import has_active_order_evidence  # noqa: PLC0415

        bundle = getattr(ctx, "commerce_bundle", None) or {}
        if has_active_order_evidence(bundle if isinstance(bundle, dict) else {}):
            return True
    except Exception:  # noqa: BLE001
        logger.exception("[CATALOG_SEARCH_EVIDENCE] active_order check failed")

    last_action = str(getattr(state, "last_action", "") or "")
    if last_action in _COMMERCE_THREAD_ACTIONS and _recent_history(ctx):
        try:
            from ..intent.non_commerce_classifier import (  # noqa: PLC0415
                has_product_commerce_signal,
            )

            for turn in _recent_history(ctx)[-6:]:
                body = str((turn or {}).get("body") or (turn or {}).get("message") or "")
                role = str((turn or {}).get("role") or (turn or {}).get("direction") or "").lower()
                if role in {"user", "customer", "in", "inbound", ""} and has_product_commerce_signal(body):
                    return True
        except Exception:  # noqa: BLE001
            logger.exception("[CATALOG_SEARCH_EVIDENCE] history commerce scan failed")

    return False


def _focus_matches_query(ctx: BrainContext, query: str) -> bool:
    focus = getattr(getattr(ctx, "state", None), "current_product_focus", None) or {}
    if not isinstance(focus, dict):
        return False
    title = str(focus.get("title") or "").strip()
    if not title or not query:
        return False
    try:
        from .product_visual import _fuzzy_title_match  # noqa: PLC0415

        return _fuzzy_title_match(title, query)
    except Exception:  # noqa: BLE001
        return _norm_token(title) in _norm_token(query) or _norm_token(query) in _norm_token(title)


def _intent_product_slot(ctx: BrainContext, query: str) -> bool:
    intent = getattr(ctx, "intent", None)
    slots = getattr(intent, "slots", None) or {}
    for key in ("product_query", "product_name"):
        slot_val = str(slots.get(key) or "").strip()
        if slot_val and _norm_token(slot_val) == _norm_token(query):
            return True
    return False


def _query_has_product_domain_signal(query: str) -> bool:
    """Delegate to P1-D-3 ``has_product_commerce_signal`` — not a new keyword list.

    Uses the existing platform honey/commerce domain markers in
    ``non_commerce_classifier`` (``عسل``, ``طلح``, ``سدر``, …). This is one
    supporting signal inside ``has_catalog_search_evidence``; it never blocks
    when focus, slots, or explicit visual evidence already justify search.
    """
    if not (query or "").strip():
        return False
    try:
        from ..intent.non_commerce_classifier import (  # noqa: PLC0415
            has_product_commerce_signal,
        )

        return has_product_commerce_signal(query)
    except Exception:  # noqa: BLE001
        return False


def is_discourse_only_query(query: str) -> bool:
    tokens = [_norm_token(t) for t in (query or "").split() if t.strip()]
    if not tokens:
        return True
    if all(t in _DISCOURSE_ONLY_TOKENS for t in tokens):
        return True
    try:
        from .start_order_verb_guard import is_order_verb_only_query  # noqa: PLC0415

        return is_order_verb_only_query(query)
    except Exception:  # noqa: BLE001
        return False


def is_explicit_customer_visual_product_ask(message: str) -> bool:
    from .product_visual import (  # noqa: PLC0415
        _GENERIC_VISUAL_RE,
        _customer_visual_ask_present,
        customer_authored_caption,
        extract_visual_product_query,
        is_deictic_visual_request,
        is_product_visual_request,
        normalize_for_visual_detection,
    )

    cap = customer_authored_caption(message) or (message or "").strip()
    if not cap:
        return False
    if not is_product_visual_request(cap):
        return False
    if extract_visual_product_query(cap):
        return True
    if is_deictic_visual_request(cap):
        return True
    norm = normalize_for_visual_detection(cap)
    if _GENERIC_VISUAL_RE.search(norm):
        return True
    return _customer_visual_ask_present(norm)


def _is_collections_first_browse(decision: Optional[Decision]) -> bool:
    """Structured discovery plan must reach the presenter — not LLM fallback."""
    args = dict(getattr(decision, "args", None) or {})
    mode = str(args.get("discovery_mode") or "").strip().lower()
    if mode == "collections_first":
        return True
    source = str(args.get("source") or "").strip().lower()
    if source in _COLLECTIONS_FIRST_SOURCES:
        return True
    entry = str(args.get("discovery_entry_type") or "").strip().lower()
    if entry in _COLLECTIONS_FIRST_ENTRIES and mode == "collections_first":
        return True
    return False


def has_catalog_search_evidence(
    ctx: BrainContext,
    query: str,
    decision: Optional[Decision] = None,
) -> bool:
    """True when catalog search is justified by operational evidence."""
    try:
        from ..truth_surface.product_sale_offer_loader import (  # noqa: PLC0415
            is_store_wide_product_sale_inquiry,
        )

        if is_store_wide_product_sale_inquiry(
            getattr(ctx, "message", "") or "",
            brain_state=getattr(ctx, "state", None),
        ):
            return False
    except Exception:  # noqa: BLE001  # noqa: silent-ok — offer gate must not block evidence
        pass

    args = dict(getattr(decision, "args", None) or {})
    q = (query or "").strip()

    if _is_collections_first_browse(decision):
        return True

    if args.get("rejected_product") or args.get("selected_product"):
        return True
    if args.get("alternatives"):
        return True

    source = str(args.get("source") or "").strip().lower()
    if source in {"top_products", "show_more", "global_browse", "browse_catalog_groups", "collections_first"}:
        return True

    if not q:
        reason = str(getattr(decision, "reason", "") or "").lower()
        if any(k in reason for k in ("browse", "top", "show more", "more products")):
            return True
        return False

    if is_discourse_only_query(q):
        return False

    if _is_vision_template_query(q):
        return False

    if _is_shipping_or_geo_ambiguous_query(q):
        return False

    if _query_has_product_domain_signal(q):
        return True

    if _focus_matches_query(ctx, q):
        return True

    if _intent_product_slot(ctx, q):
        return True

    if args.get("after_search") == "product_visual" and is_explicit_customer_visual_product_ask(
        ctx.message or "",
    ):
        return True

    if args.get("after_search") == "propose_order":
        if is_discourse_only_query(q):
            return False
        return True

    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name in {"ask_product", "start_order", "pick_list_item"}:
        if len(q.split()) >= 2 or _query_has_product_domain_signal(q):
            return True

    if _is_ask_price_catalog_credible(ctx, q):
        return True

    return False


def should_use_search_miss_template(
    ctx: BrainContext,
    query: str,
    subject: str,
) -> bool:
    """Deterministic miss copy only when the query was catalog-credible."""
    subj = (subject or query or "").strip()
    if not subj:
        return False
    if is_discourse_only_query(subj):
        return False
    if _is_vision_template_query(subj):
        return False
    return has_catalog_search_evidence(ctx, subj, Decision(action=ACTION_SEARCH_PRODUCTS, args={"query": query}))


def _is_vision_template_query(query: str) -> bool:
    try:
        from .product_visual import _is_vision_stoplist_query  # noqa: PLC0415

        return _is_vision_stoplist_query(query)
    except Exception:  # noqa: BLE001
        return False


def _should_route_customer_media_to_llm(
    ctx: BrainContext,
    query: str,
    decision: Decision,
) -> bool:
    from .product_media import _is_customer_media_origin  # noqa: PLC0415

    msg = ctx.message or ""
    meta = _inbound_metadata(ctx)
    if not _is_customer_media_origin(msg, meta):
        return False
    if is_explicit_customer_visual_product_ask(msg):
        return False
    if has_catalog_search_evidence(ctx, query, decision):
        return False
    return True


def _product_media_llm_decision(ctx: BrainContext) -> Decision:
    from .product_media import (  # noqa: PLC0415
        build_product_media_decision_args,
        detect_product_media_turn,
    )

    meta = _inbound_metadata(ctx)
    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    verdict = detect_product_media_turn(
        ctx.message or "",
        inbound_metadata=meta,
        intent_name=intent_name,
        commerce_blocked=False,
    )
    bundle = getattr(ctx, "commerce_bundle", None) or {}
    return Decision(
        action=ACTION_LLM_REPLY,
        args=build_product_media_decision_args(
            verdict,
            commerce_bundle=bundle if isinstance(bundle, dict) else {},
        ),
        reason="catalog_search_gate: customer media context — product_media LLM path",
        confidence=0.86,
    )


def _weak_query_llm_decision(ctx: BrainContext, decision: Decision, query: str) -> Decision:
    # Product-evidence queries never reach here — ``has_catalog_search_evidence``
    # runs first, so ``shipping_price_ambiguous`` applies only when the blocked
    # token lacked catalog credibility (e.g. ``بكم الرياض`` in an order thread).
    args = {"topic": "commerce_ambiguous"}
    blocked = (query or "").strip()
    if blocked:
        args["blocked_catalog_query"] = blocked
    shipping_ambiguous = (
        _is_shipping_or_geo_ambiguous_query(blocked)
        or _inbound_is_shipping_price_question(ctx.message or "")
    )
    if shipping_ambiguous and (
        has_conversation_fulfillment_context(ctx)
        or _inbound_is_shipping_price_question(ctx.message or "")
    ):
        args["topic"] = "shipping_price_ambiguous"
    return Decision(
        action=ACTION_LLM_REPLY,
        args=args,
        reason=(
            "catalog_search_gate: weak catalog query — LLM compose with "
            f"context (was: {decision.reason})"
        ),
        confidence=max(0.72, float(getattr(decision, "confidence", 0) or 0) - 0.05),
    )


def apply_catalog_search_evidence_gate(
    ctx: BrainContext,
    decision: Decision,
) -> Decision:
    """Redirect weak ``search_products`` decisions before execution."""
    if getattr(decision, "action", "") != ACTION_SEARCH_PRODUCTS:
        return decision

    query = str((decision.args or {}).get("query") or "").strip()

    if _is_collections_first_browse(decision):
        logger.info(
            "[CATALOG_SEARCH_GATE] collections_first tenant=%s source=%r → presenter",
            getattr(ctx, "tenant_id", None),
            str((decision.args or {}).get("source") or "")[:40],
        )
        return decision

    if _should_route_customer_media_to_llm(ctx, query, decision):
        logger.info(
            "[CATALOG_SEARCH_GATE] media_context tenant=%s query=%r → product_media",
            getattr(ctx, "tenant_id", None),
            query[:60],
        )
        return _product_media_llm_decision(ctx)

    if has_catalog_search_evidence(ctx, query, decision):
        return decision

    logger.info(
        "[CATALOG_SEARCH_GATE] blocked tenant=%s query=%r reason=%r preview=%r",
        getattr(ctx, "tenant_id", None),
        query[:60],
        decision.reason,
        (ctx.message or "")[:80],
    )
    return _weak_query_llm_decision(ctx, decision, query)


CATALOG_MISS_CHOSEN_PATH = "catalog_miss_deterministic"

_CATALOG_MISS_NO_MATCH_VARIANTS = (
    "ما ظهر عندي تطابق واضح في الكتالوج حالياً. "
    "اكتب اسم المنتج كما يظهر في المتجر أو حدّد النوع/الحجم المطلوب.",
    "ما لقيت تطابقاً واضحاً في الكتالوج حالياً. "
    "أرسل اسم المنتج كما في المتجر وساعدك.",
)

_CATALOG_MISS_NO_SYNCED_VARIANTS = (
    "ما ظهرت منتجات متزامنة حالياً، "
    "أقدر أساعدك إذا كتبت اسم المنتج كما يظهر في المتجر.",
    "لا توجد منتجات متزامنة الآن. "
    "اكتب اسم المنتج كما يظهر في المتجر.",
)


def compose_catalog_miss_deterministic_reply(
    *,
    no_synced_products: bool = False,
    variant: int = 0,
) -> str:
    """Honest deterministic reply after catalog lookup miss — never LLM."""
    pool = (
        _CATALOG_MISS_NO_SYNCED_VARIANTS
        if no_synced_products
        else _CATALOG_MISS_NO_MATCH_VARIANTS
    )
    return pool[variant % len(pool)]


__all__ = [
    "CATALOG_MISS_CHOSEN_PATH",
    "apply_catalog_search_evidence_gate",
    "compose_catalog_miss_deterministic_reply",
    "has_catalog_search_evidence",
    "has_conversation_fulfillment_context",
    "is_discourse_only_query",
    "is_explicit_customer_visual_product_ask",
    "should_use_search_miss_template",
]
