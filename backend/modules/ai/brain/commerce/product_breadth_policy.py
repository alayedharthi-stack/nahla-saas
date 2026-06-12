"""
brain/commerce/product_breadth_policy.py
────────────────────────────────────────
LIMIT_RECOMMENDATION_BREADTH — policy-level invariant for mobile commerce UX.

Commerce intent being valid is NOT sufficient to dump 4–6 products or
stack multiple catalog cards. Breadth scales with confidence and ONLY
expands when the customer explicitly asks to browse widely.

  * low confidence (discovery, ambiguous, first recommendation)
    → 1 focused option, max 1 catalog card
  * medium confidence (specific product ask, moderate intent)
    → max 2–3 options, max 2 catalog cards
  * soft inventory browse ("وش عندكم؟") → max 2–3 options
  * hard broad browse ("وريني كل المنتجات") → max 3 options (env-tunable)

This is enforced in search, compose, pipeline state, and webhook
attachment dispatch — NOT prompt-only.

Master switch: ``LIMIT_RECOMMENDATION_BREADTH`` (default ``true``).
Legacy alias: ``LIMIT_INITIAL_PRODUCT_OPTIONS``.
"""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.product_breadth")

_HIGH_COMMERCE_INTENTS = frozenset({
    "ask_product",
    "ask_price",
    "start_order",
    "pick_list_item",
})

_AMBIGUOUS_INTENTS = frozenset({
    "general",
    "greeting",
    "hesitation",
})

# Sources that continue prior browsing — medium breadth, not auto-broad.
_CONTINUATION_SOURCES = frozenset({
    "replay",
    "show_more",
    "top_products_replay_fallback",
})

# Soft inventory questions — unlock 2–3 options, NOT a 5-item dump.
_SOFT_INVENTORY_BROWSE_PHRASES = (
    "وش عندكم",
    "ما عندكم",
    "ايش عندكم",
    "ايه عندكم",
    "ما المنتجات",
    "ما المتاح",
    "ما المتوفر",
)

# Global availability / catalog browse — defocus stale product_focus for the turn.
# ``وش``-prefixed inventory questions plus top-seller list triggers.
# ``explicit_broad_browse_requested`` (soft/hard) is checked first in
# ``global_availability_browse_requested``.
_GLOBAL_CATALOG_BROWSE_PHRASES = (
    "وش المتوفر",
    "ايش المتوفر",
    "ايه المتوفر",
    "وش المنتجات",
    "ايش المنتجات",
    "وش الانواع",
    "ايش الانواع",
    "وش المنتجات كلها",
    "الاكثر مبيعا",
    "اكثر مبيعا",
    "الاكثر مبيعًا",
    "اكثر مبيعًا",
    "الاكثر طلبا",
    "اكثر طلبا",
    "الاكثر طلبًا",
    "اعرض المنتجات",
    "وريني المنتجات",
    "show products",
    "show me",
    "top products",
    "best sellers",
)

_HARD_BROAD_BROWSE_PHRASES = (
    "ورني كل الانواع",
    "وريني كل الانواع",
    "ورني كل المنتجات",
    "وريني كل المنتجات",
    "ورني كل الخيارات",
    "وريني كل الخيارات",
    "ابي اشوف الخيارات كلها",
    "أبي أشوف الخيارات كلها",
    "ارسل المنتجات",
    "أرسل المنتجات",
    "اعرض كل المنتجات",
    "اعرض جميع المنتجات",
    "كل المنتجات",
    "كل الخيارات",
    "show all products",
    "list all products",
)

_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
_ZW_RE = re.compile(r"[\u200B-\u200F\u2028-\u202F\u2060-\u206F]")


def _norm_ar(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _ZW_RE.sub("", s)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    return s.lower().strip()


def limit_recommendation_breadth_enabled() -> bool:
    raw = (
        os.getenv("LIMIT_RECOMMENDATION_BREADTH")
        or os.getenv("LIMIT_INITIAL_PRODUCT_OPTIONS", "true")
        or ""
    ).strip().lower()
    return raw not in {"false", "0", "off", "no", ""}


def limit_initial_product_options_enabled() -> bool:
    """Legacy alias — same master switch."""
    return limit_recommendation_breadth_enabled()


def _env_int(name: str, default: int, *, lo: int = 1, hi: int = 16) -> int:
    try:
        val = int((os.getenv(name) or str(default)).strip())
    except (TypeError, ValueError):
        val = default
    return max(lo, min(hi, val))


def explicit_soft_browse_requested(message: str) -> bool:
    """Inventory-style browse ("وش عندكم؟") — 2–3 focused options."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    return any(phrase in norm for phrase in _SOFT_INVENTORY_BROWSE_PHRASES)


def explicit_hard_browse_requested(message: str) -> bool:
    """Customer explicitly asks to see many / all products."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    return any(phrase in norm for phrase in _HARD_BROAD_BROWSE_PHRASES)


def explicit_broad_browse_requested(message: str) -> bool:
    """True when soft OR hard explicit browse phrases match."""
    return explicit_soft_browse_requested(message) or explicit_hard_browse_requested(
        message
    )


def global_availability_browse_requested(message: str) -> bool:
    """Store-wide inventory browse — do not narrow to stale ``product_focus``.

    Deterministic context gate only; the LLM still composes the reply freely.
    """
    norm = _norm_ar(message or "")
    if not norm:
        return False
    if explicit_broad_browse_requested(message):
        return True
    return any(phrase in norm for phrase in _GLOBAL_CATALOG_BROWSE_PHRASES)


def global_catalog_browse_requested(message: str) -> bool:
    """Catalog-wide type/availability asks — not soft-only «وش عندكم»."""
    norm = _norm_ar(message or "")
    if not norm:
        return False
    if explicit_hard_browse_requested(message):
        return True
    return any(phrase in norm for phrase in _GLOBAL_CATALOG_BROWSE_PHRASES)


def resolve_kb_active_product_ids(
    state: Any,
    message: str,
) -> Optional[set]:
    """Catalog product ids for KB section scoping.

    Returns ``None`` (unscoped) on global browse turns or when there is no
    focus signal; otherwise a set of ids from focus + recent recommendations.
    """
    if global_availability_browse_requested(message or ""):
        return None
    pid_candidates: set = set()
    try:
        focus = getattr(state, "current_product_focus", None) or {}
        focus_id = focus.get("id") if isinstance(focus, dict) else None
        if isinstance(focus_id, int):
            pid_candidates.add(focus_id)
        for rec in (getattr(state, "last_recommended_products", None) or [])[:5]:
            rid = (rec or {}).get("id") if isinstance(rec, dict) else None
            if isinstance(rid, int):
                pid_candidates.add(rid)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — KB id extraction fallback returns unscoped
        return None
    return pid_candidates if pid_candidates else None


def _product_key(product: Dict[str, Any]) -> str:
    p = product or {}
    return str(p.get("external_id") or p.get("id") or p.get("title") or "").strip()


def next_catalog_browse_batch(
    pool: Sequence[Dict[str, Any]],
    *,
    offset: int = 0,
    exclude_keys: Optional[Sequence[str]] = None,
    limit: int = 3,
) -> tuple[List[Dict[str, Any]], int]:
    """Return the next unseen slice from *pool* and the advanced offset."""
    excluded = {k for k in (exclude_keys or []) if k}
    batch: List[Dict[str, Any]] = []
    idx = max(0, int(offset or 0))
    while idx < len(pool) and len(batch) < max(1, limit):
        p = pool[idx]
        idx += 1
        key = _product_key(p)
        if key and key in excluded:
            continue
        batch.append(p)
    return batch, idx


@dataclass(frozen=True)
class ProductBreadthDecision:
    display_limit: int
    catalog_card_limit: int
    search_fetch_limit: int
    confidence_tier: str       # low | medium | high
    mode: str                  # focused | standard | broad
    policy_enabled: bool = True
    explicit_broad: bool = False

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "display_limit": self.display_limit,
            "catalog_card_limit": self.catalog_card_limit,
            "search_fetch_limit": self.search_fetch_limit,
            "confidence_tier": self.confidence_tier,
            "mode": self.mode,
            "policy_enabled": self.policy_enabled,
            "explicit_broad": self.explicit_broad,
        }


def _confidence_tier(
    *,
    intent_name: str,
    intent_confidence: float,
    query: str,
    stage: str,
    is_first_recommendation: bool,
) -> str:
    name = (intent_name or "").strip().lower()
    q = (query or "").strip()

    if name in _HIGH_COMMERCE_INTENTS and float(intent_confidence or 0) >= 0.82 and q:
        return "high"
    if name in _AMBIGUOUS_INTENTS or float(intent_confidence or 0) < 0.70:
        return "low"
    if is_first_recommendation and stage in ("discovery", "exploring") and not q:
        return "low"
    if stage in ("discovery", "exploring") and not q:
        return "low"
    return "medium"


def resolve_product_breadth(
    *,
    message: str = "",
    intent_name: str = "",
    intent_confidence: float = 0.0,
    source: str = "",
    query: str = "",
    stage: str = "discovery",
    total_available: int = 0,
    is_first_recommendation: bool = False,
) -> ProductBreadthDecision:
    """Return display / fetch / catalog limits for this commerce turn."""
    enabled = limit_recommendation_breadth_enabled()
    tier = _confidence_tier(
        intent_name=intent_name,
        intent_confidence=intent_confidence,
        query=query,
        stage=stage,
        is_first_recommendation=is_first_recommendation,
    )
    soft_browse = explicit_soft_browse_requested(message)
    hard_browse = explicit_hard_browse_requested(message)
    catalog_global = global_catalog_browse_requested(message)
    global_browse = global_availability_browse_requested(message)
    try:
        from modules.ai.brain.postprocess.availability_guard_policy import (  # noqa: PLC0415
            browse_alternatives_requested,
        )

        alt_browse = browse_alternatives_requested(message)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — optional import for breadth policy
        alt_browse = False
    explicit_broad = soft_browse or hard_browse or global_browse or alt_browse

    limit_low = _env_int("PRODUCT_DISPLAY_LIMIT_LOW", 1)
    limit_medium = _env_int("PRODUCT_DISPLAY_LIMIT_MEDIUM", 3)
    limit_broad = _env_int("PRODUCT_DISPLAY_LIMIT_BROAD", 3)
    catalog_low = _env_int("CATALOG_CARDS_PER_TURN_LOW", 1)
    catalog_medium = _env_int("CATALOG_CARDS_PER_TURN_FOCUSED", 2)
    catalog_broad = _env_int("CATALOG_CARDS_PER_TURN_MAX", 3)

    if not enabled:
        return ProductBreadthDecision(
            display_limit=16,
            catalog_card_limit=catalog_broad,
            search_fetch_limit=8,
            confidence_tier=tier,
            mode="broad",
            policy_enabled=False,
            explicit_broad=explicit_broad,
        )

    if total_available == 1:
        display, mode, catalog = 1, "focused", catalog_low
    elif hard_browse or catalog_global:
        display, mode, catalog = limit_broad, "broad", catalog_medium
    elif soft_browse or alt_browse:
        # "وش عندكم؟" / "وش غيرها؟" → guided browse, not a catalog wall.
        display, mode, catalog = limit_medium, "browse", catalog_medium
    elif (source or "").strip().lower() in _CONTINUATION_SOURCES:
        display, mode, catalog = limit_medium, "standard", catalog_medium
    elif tier == "low":
        display, mode, catalog = limit_low, "focused", catalog_low
    elif tier == "medium":
        display, mode, catalog = limit_medium, "standard", catalog_medium
    elif tier == "high":
        # Specific product ask — 1–2 focused matches, one card max.
        display, mode, catalog = min(2, limit_medium), "focused", catalog_low
    else:
        display, mode, catalog = limit_medium, "standard", catalog_medium

    fetch = min(12, max(display + 4, display + 2))

    return ProductBreadthDecision(
        display_limit=display,
        catalog_card_limit=catalog,
        search_fetch_limit=fetch,
        confidence_tier=tier,
        mode=mode,
        policy_enabled=True,
        explicit_broad=explicit_broad,
    )


def resolve_product_breadth_from_context(ctx: Any, decision: Any) -> ProductBreadthDecision:
    """Convenience wrapper using ``BrainContext`` + ``Decision``."""
    intent = getattr(ctx, "intent", None)
    state = getattr(ctx, "state", None)
    args = getattr(decision, "args", None) or {}
    had_prior_list = bool(getattr(state, "last_search_candidates", None))
    stage = str(getattr(state, "stage", "") or "discovery")
    return resolve_product_breadth(
        message=str(getattr(ctx, "message", "") or ""),
        intent_name=getattr(intent, "name", "") or "",
        intent_confidence=float(getattr(intent, "confidence", 0) or 0),
        source=str(args.get("source") or ""),
        query=str(args.get("query") or ""),
        stage=stage,
        is_first_recommendation=(
            not had_prior_list and stage in ("discovery", "exploring")
        ),
    )


def apply_display_slice(
    products: Sequence[Dict[str, Any]],
    breadth: ProductBreadthDecision,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Slice *products* for customer-facing display; return meta for templates."""
    pool = list(products or [])
    total = len(pool)
    if not breadth.policy_enabled or total <= breadth.display_limit:
        return pool, {
            "total_count": total,
            "hidden_count": 0,
            "display_limit": total,
            "show_more_hint": False,
        }
    shown = pool[: breadth.display_limit]
    hidden = total - len(shown)
    return shown, {
        "total_count": total,
        "hidden_count": hidden,
        "display_limit": breadth.display_limit,
        "show_more_hint": hidden > 0,
    }


def clamp_product_attachments(
    attachments: Sequence[Dict[str, Any]],
    breadth: ProductBreadthDecision,
) -> List[Dict[str, Any]]:
    """Hard cap on product-card attachments for one outbound turn."""
    items = list(attachments or [])
    if not breadth.policy_enabled:
        return items
    cap = max(1, int(breadth.catalog_card_limit or 1))
    if len(items) <= cap:
        return items
    return items[:cap]


def resolve_catalog_card_limit(
    *,
    message: str = "",
    intent_name: str = "",
    intent_confidence: float = 0.0,
    source: str = "",
    query: str = "",
    stage: str = "discovery",
    is_first_recommendation: bool = False,
) -> int:
    """Max product-card attachments to dispatch in one webhook turn."""
    b = resolve_product_breadth(
        message=message,
        intent_name=intent_name,
        intent_confidence=intent_confidence,
        source=source,
        query=query,
        stage=stage,
        is_first_recommendation=is_first_recommendation,
    )
    return b.catalog_card_limit


def resolve_breadth_for_inbound(
    *,
    message: str,
    inbound_metadata: Optional[dict] = None,
    brain_state: Optional[dict] = None,
) -> ProductBreadthDecision:
    """Webhook-side breadth from inbound text + persisted brain state."""
    meta = inbound_metadata or {}
    bs = brain_state or {}
    had_prior = bool(bs.get("last_search_candidates"))
    stage = str(bs.get("stage") or "discovery")
    return resolve_product_breadth(
        message=message or "",
        intent_name=str(bs.get("last_intent") or ""),
        source=str(meta.get("last_search_source") or ""),
        query="",
        stage=stage,
        is_first_recommendation=(not had_prior and stage in ("discovery", "exploring")),
    )


def log_product_breadth(
    *,
    tenant_id: Any,
    breadth: ProductBreadthDecision,
    total: int,
    shown: int,
    action: str = "",
) -> None:
    try:
        logger.info(
            "[RECOMMENDATION_BREADTH] tenant=%s action=%s mode=%s tier=%s "
            "explicit_broad=%s display_limit=%d catalog_limit=%d "
            "total=%d shown=%d",
            tenant_id,
            action or "?",
            breadth.mode,
            breadth.confidence_tier,
            str(breadth.explicit_broad).lower(),
            breadth.display_limit,
            breadth.catalog_card_limit,
            total,
            shown,
        )
    except Exception:
        pass


__all__ = [
    "ProductBreadthDecision",
    "apply_display_slice",
    "clamp_product_attachments",
    "explicit_broad_browse_requested",
    "explicit_hard_browse_requested",
    "explicit_soft_browse_requested",
    "global_availability_browse_requested",
    "limit_initial_product_options_enabled",
    "limit_recommendation_breadth_enabled",
    "log_product_breadth",
    "next_catalog_browse_batch",
    "resolve_breadth_for_inbound",
    "resolve_catalog_card_limit",
    "resolve_kb_active_product_ids",
    "resolve_product_breadth",
    "resolve_product_breadth_from_context",
]
