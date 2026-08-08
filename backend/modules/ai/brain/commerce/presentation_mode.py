"""
commerce/presentation_mode.py
─────────────────────────────
Platform-wide outbound presentation mode — evidence-based, tenant-agnostic.

Phase 0 (Foundation): enum, resolver, attachment vocabulary.
Phase 1 (Shadow): stamp ``Decision.args`` + structured logs only — no routing,
dispatch enforcement, or prompt changes.

Telemetry (grep-stable):
  * ``[PRESENTATION_MODE]``        — brain boundary (resolved mode)
  * ``[PRESENTATION_MODE_SHADOW]`` — webhook (mode vs actual attachments)

Env flags:
  * ``NAHLA_PRESENTATION_MODE_SHADOW``   — default ``true`` (Phase 1)
  * ``NAHLA_PRESENTATION_MODE_ENFORCE``  — default ``false`` (Phase 3+)
  * ``NAHLA_PRICE_WITH_CARD_ENABLED``    — default ``false`` (Phase 5)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

logger = logging.getLogger("nahla.brain.presentation_mode")


class PresentationMode(str, Enum):
    PRICE_ONLY = "price_only"
    PRICE_WITH_CARD = "price_with_card"
    VISUAL = "visual"
    DISCOVERY_LIST = "discovery_list"


# Closed attachment vocabulary referenced by future dispatch enforcement.
ATTACHMENT_NONE = "none"
ATTACHMENT_PRODUCT_CARD = "product_card"
ATTACHMENT_CATALOG = "catalog"
ATTACHMENT_LEGACY_IMAGE = "legacy_image"
ATTACHMENT_CTA_URL = "cta_url"
ATTACHMENT_INTERACTIVE_BUTTONS = "interactive_buttons"

_ALLOWED_BY_MODE: Dict[PresentationMode, FrozenSet[str]] = {
    PresentationMode.PRICE_ONLY: frozenset({ATTACHMENT_NONE}),
    PresentationMode.PRICE_WITH_CARD: frozenset({
        ATTACHMENT_PRODUCT_CARD,
        ATTACHMENT_CATALOG,
        ATTACHMENT_LEGACY_IMAGE,
        ATTACHMENT_CTA_URL,
    }),
    PresentationMode.VISUAL: frozenset({
        ATTACHMENT_PRODUCT_CARD,
        ATTACHMENT_CATALOG,
        ATTACHMENT_LEGACY_IMAGE,
        ATTACHMENT_CTA_URL,
    }),
    PresentationMode.DISCOVERY_LIST: frozenset({
        ATTACHMENT_NONE,
        ATTACHMENT_INTERACTIVE_BUTTONS,
    }),
}

_FORBIDDEN_BY_MODE: Dict[PresentationMode, FrozenSet[str]] = {
    PresentationMode.PRICE_ONLY: frozenset({
        ATTACHMENT_PRODUCT_CARD,
        ATTACHMENT_CATALOG,
        ATTACHMENT_LEGACY_IMAGE,
        ATTACHMENT_CTA_URL,
    }),
    PresentationMode.PRICE_WITH_CARD: frozenset({
        ATTACHMENT_INTERACTIVE_BUTTONS,
    }),
    PresentationMode.VISUAL: frozenset({
        ATTACHMENT_INTERACTIVE_BUTTONS,
    }),
    PresentationMode.DISCOVERY_LIST: frozenset({
        ATTACHMENT_PRODUCT_CARD,
        ATTACHMENT_CATALOG,
        ATTACHMENT_LEGACY_IMAGE,
    }),
}


def _truthy_env(name: str, default: str = "false") -> bool:
    raw = (os.environ.get(name) or default).strip().lower()
    return raw in ("1", "true", "yes", "on")


def is_presentation_mode_shadow_enabled() -> bool:
    """Shadow logging + ``Decision.args`` stamping (Phase 1). Default ON."""
    return _truthy_env("NAHLA_PRESENTATION_MODE_SHADOW", "true")


def is_presentation_mode_enforce_enabled() -> bool:
    """Dispatch/routing enforcement (Phase 3+). Default OFF."""
    return _truthy_env("NAHLA_PRESENTATION_MODE_ENFORCE", "false")


def is_price_with_card_enabled() -> bool:
    """Optional rich price presentation (Phase 5). Default OFF."""
    return _truthy_env("NAHLA_PRICE_WITH_CARD_ENABLED", "false")


@dataclass(frozen=True)
class PresentationModeResult:
    mode: Optional[PresentationMode]
    evidence: Dict[str, Any] = field(default_factory=dict)
    skipped: bool = False

    @property
    def allowed_attachments(self) -> Sequence[str]:
        if self.mode is None:
            return ()
        return sorted(_ALLOWED_BY_MODE.get(self.mode, frozenset()))

    @property
    def forbidden_attachments(self) -> Sequence[str]:
        if self.mode is None:
            return ()
        return sorted(_FORBIDDEN_BY_MODE.get(self.mode, frozenset()))


def _has_product_focus(ctx: Any) -> bool:
    focus = getattr(getattr(ctx, "state", None), "current_product_focus", None) or {}
    return bool(isinstance(focus, dict) and str(focus.get("title") or "").strip())


def _is_visual_mode(ctx: Any, decision: Any) -> bool:
    from ..types import INTENT_PRODUCT_VISUAL_REQUEST  # noqa: PLC0415

    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    if intent_name == INTENT_PRODUCT_VISUAL_REQUEST:
        return True

    args = getattr(decision, "args", None) or {}
    topic = str(args.get("topic") or "").strip().lower()
    if topic == "product_visual":
        return True

    after_search = str(args.get("after_search") or "").strip().lower()
    if after_search == "product_visual":
        return True

    msg = str(getattr(ctx, "message", "") or "")
    try:
        from .product_visual import is_product_visual_request  # noqa: PLC0415

        if is_product_visual_request(msg):
            return True
    except Exception:  # noqa: BLE001  # noqa: silent-ok — product_visual optional import path
        pass
    return False


def _is_commerce_price_intent(ctx: Any) -> bool:
    from ..intent.rules import INTENT_ASK_PRICE, INTENT_ASK_PRODUCT  # noqa: PLC0415

    name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")
    return name in (INTENT_ASK_PRICE, INTENT_ASK_PRODUCT)


def _composite_price_and_visual(message: str) -> bool:
    """Price + explicit visual deictic in one turn (price_with_card candidate)."""
    msg = (message or "").strip()
    if not msg:
        return False
    try:
        from .product_visual import is_product_visual_request  # noqa: PLC0415

        if not is_product_visual_request(msg):
            return False
    except Exception:  # noqa: BLE001
        return False
    norm = msg.lower()
    return any(
        tok in norm
        for tok in ("بكم", "كم سعر", "سعر", "price", "how much")
    )


def _is_discovery_decision(ctx: Any, decision: Any) -> bool:
    from ..decision.actions import (  # noqa: PLC0415
        ACTION_NARROW,
        ACTION_SEARCH_PRODUCTS,
    )

    args = getattr(decision, "args", None) or {}
    topic = str(args.get("topic") or "").strip().lower()
    if topic in {"category_discovery", "show_all_variants_prices"}:
        return True

    action = str(getattr(decision, "action", "") or "")
    if action in {ACTION_NARROW, ACTION_SEARCH_PRODUCTS}:
        source = str(args.get("source") or "").strip().lower()
        if source in {"top_products_start_order", "show_more", "replay"}:
            return True
        if action == ACTION_SEARCH_PRODUCTS and not _is_commerce_price_intent(ctx):
            return True
    return False


def resolve_presentation_mode(
    ctx: Any,
    *,
    decision: Optional[Any] = None,
) -> PresentationModeResult:
    """
    Evidence-based presentation mode for this turn.

    ``price_only`` is the default for resolved product price questions
    (``PRODUCT_PRICE_ASK`` and focus-backed unit/pronoun price turns).
    """
    if decision is None:
        return PresentationModeResult(mode=None, skipped=True, evidence={"reason": "no_decision"})

    intent_name = str(getattr(getattr(ctx, "intent", None), "name", "") or "")

    if _is_visual_mode(ctx, decision):
        return PresentationModeResult(
            mode=PresentationMode.VISUAL,
            evidence={"rule": "visual_intent", "intent": intent_name},
        )

    price_kind_value = ""
    normalized_subject = ""
    if _is_commerce_price_intent(ctx):
        try:
            from .price_turn_classifier import (  # noqa: PLC0415
                PriceTurnKind,
                classify_price_turn,
                normalize_price_subject,
            )

            kind = classify_price_turn(ctx)
            price_kind_value = kind.value
            normalized_subject = normalize_price_subject(ctx)

            if kind == PriceTurnKind.PRODUCT_PRICE_ASK and normalized_subject:
                if is_price_with_card_enabled() and _composite_price_and_visual(
                    str(getattr(ctx, "message", "") or ""),
                ):
                    return PresentationModeResult(
                        mode=PresentationMode.PRICE_WITH_CARD,
                        evidence={
                            "rule": "product_price_ask_composite_visual",
                            "price_kind": price_kind_value,
                            "normalized_subject": normalized_subject[:60],
                        },
                    )
                return PresentationModeResult(
                    mode=PresentationMode.PRICE_ONLY,
                    evidence={
                        "rule": "product_price_ask_resolved",
                        "price_kind": price_kind_value,
                        "normalized_subject": normalized_subject[:60],
                    },
                )

            if kind in {
                PriceTurnKind.UNIT_PRICE_REFERENCE,
                PriceTurnKind.PRONOUN_REFERENCE,
            } and _has_product_focus(ctx):
                return PresentationModeResult(
                    mode=PresentationMode.PRICE_ONLY,
                    evidence={
                        "rule": "focus_backed_price_turn",
                        "price_kind": price_kind_value,
                    },
                )

            if kind == PriceTurnKind.BARE_PRICE_ASK:
                return PresentationModeResult(
                    mode=PresentationMode.DISCOVERY_LIST,
                    evidence={"rule": "bare_price_ask", "price_kind": price_kind_value},
                )

            if kind == PriceTurnKind.PRICE_COMMENT and _has_product_focus(ctx):
                return PresentationModeResult(
                    mode=PresentationMode.PRICE_ONLY,
                    evidence={"rule": "price_comment_on_focus", "price_kind": price_kind_value},
                )
        except Exception as exc:  # noqa: BLE001  # noqa: silent-ok — price classifier optional; unclassified mode
            logger.debug("[PRESENTATION_MODE] price classifier unavailable: %s", exc)

    if _is_discovery_decision(ctx, decision):
        return PresentationModeResult(
            mode=PresentationMode.DISCOVERY_LIST,
            evidence={
                "rule": "discovery_decision",
                "intent": intent_name,
                "action": str(getattr(decision, "action", "") or ""),
            },
        )

    if _is_commerce_price_intent(ctx):
        from ..decision.actions import ACTION_SEARCH_PRODUCTS  # noqa: PLC0415

        action = str(getattr(decision, "action", "") or "")
        if action == ACTION_SEARCH_PRODUCTS and normalized_subject:
            return PresentationModeResult(
                mode=PresentationMode.PRICE_ONLY,
                evidence={
                    "rule": "price_search_shadow_default",
                    "normalized_subject": normalized_subject[:60],
                    "price_kind": price_kind_value,
                },
            )

    return PresentationModeResult(
        mode=None,
        skipped=True,
        evidence={"reason": "non_commerce_or_unclassified", "intent": intent_name},
    )


def log_presentation_mode(
    *,
    tenant_id: Optional[int],
    mode: PresentationMode,
    evidence: Optional[Dict[str, Any]] = None,
    action: str = "",
    intent: str = "",
    preview: str = "",
    source: str = "brain",
) -> None:
    ev = evidence or {}
    logger.info(
        "[PRESENTATION_MODE] tenant=%s source=%s mode=%s action=%s intent=%s "
        "rule=%s price_kind=%s preview=%r shadow=%s enforce=%s",
        tenant_id if tenant_id is not None else "-",
        source or "-",
        mode.value,
        action or "-",
        intent or "-",
        str(ev.get("rule") or "-"),
        str(ev.get("price_kind") or "-"),
        (preview or "")[:80],
        str(is_presentation_mode_shadow_enabled()).lower(),
        str(is_presentation_mode_enforce_enabled()).lower(),
    )


def apply_presentation_mode_shadow(ctx: Any, decision: Any) -> Any:
    """
    Stamp shadow metadata on ``Decision.args`` without changing ``action``.

    No-op when shadow flag is off or mode cannot be classified.
    """
    if not is_presentation_mode_shadow_enabled():
        return decision

    result = resolve_presentation_mode(ctx, decision=decision)
    if result.mode is None or result.skipped:
        return decision

    args = dict(getattr(decision, "args", None) or {})
    args["presentation_mode"] = result.mode.value
    args["presentation_evidence"] = dict(result.evidence)
    args["presentation_shadow"] = True
    args["presentation_allowed_attachments"] = list(result.allowed_attachments)
    args["presentation_forbidden_attachments"] = list(result.forbidden_attachments)

    log_presentation_mode(
        tenant_id=getattr(ctx, "tenant_id", None),
        mode=result.mode,
        evidence=result.evidence,
        action=str(getattr(decision, "action", "") or ""),
        intent=str(getattr(getattr(ctx, "intent", None), "name", "") or ""),
        preview=str(getattr(ctx, "message", "") or ""),
        source="brain",
    )

    return replace(decision, args=args)


def log_presentation_mode_dispatch_shadow(
    *,
    tenant_id: Optional[int],
    presentation_mode: str = "",
    delivery_audit: Optional[Dict[str, Any]] = None,
    brain_action: str = "",
    inbound_preview: str = "",
) -> None:
    """
    Compare shadow mode vs what actually landed on the wire (Phase 1 only).
    """
    if not is_presentation_mode_shadow_enabled():
        return

    audit = delivery_audit or {}
    catalog = int(audit.get("catalog_card_sent_count", 0) or 0)
    legacy = int(audit.get("legacy_media_sent_count", 0) or 0)
    cta = int(audit.get("cta_url_sent_count", 0) or 0)
    unified = int(audit.get("unified_product_card_sent_count", 0) or 0)
    rich = catalog + legacy + cta + unified

    mismatch = ""
    mode = (presentation_mode or "").strip().lower()
    if mode == PresentationMode.PRICE_ONLY.value and rich > 0:
        mismatch = "price_only_got_rich_attachments"
    elif mode == PresentationMode.DISCOVERY_LIST.value and catalog > 0:
        mismatch = "discovery_list_got_catalog_card"
    elif mode == PresentationMode.VISUAL.value and rich == 0 and audit.get("text_sent"):
        mismatch = "visual_got_text_only"

    logger.info(
        "[PRESENTATION_MODE_SHADOW] tenant=%s mode=%s action=%s "
        "catalog_cards=%d legacy_media=%d cta_urls=%d rich_total=%d "
        "mismatch=%s preview=%r audit=%s",
        tenant_id if tenant_id is not None else "-",
        mode or "-",
        brain_action or "-",
        catalog,
        legacy,
        cta,
        rich,
        mismatch or "-",
        (inbound_preview or "")[:80],
        audit,
    )


__all__ = [
    "ATTACHMENT_CATALOG",
    "ATTACHMENT_CTA_URL",
    "ATTACHMENT_INTERACTIVE_BUTTONS",
    "ATTACHMENT_LEGACY_IMAGE",
    "ATTACHMENT_NONE",
    "ATTACHMENT_PRODUCT_CARD",
    "PresentationMode",
    "PresentationModeResult",
    "apply_presentation_mode_shadow",
    "is_presentation_mode_enforce_enabled",
    "is_presentation_mode_shadow_enabled",
    "is_price_with_card_enabled",
    "log_presentation_mode",
    "log_presentation_mode_dispatch_shadow",
    "resolve_presentation_mode",
]
