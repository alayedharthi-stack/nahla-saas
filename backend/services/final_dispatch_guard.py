"""
services/final_dispatch_guard.py
────────────────────────────────
Last-chance guard for product-card / catalog-card dispatch.

Earlier gates (decision engine, product discovery, non-commerce classifier)
can block search but stale ``[PRODUCT:]`` markers, visual enforcement,
safety nets, and the attachment loop may still send cards. This module
owns the **final dispatch boundary** invariant:

  If the turn is not an explicit commerce / browse request, hard-suppress
  all product attachments before they reach the wire.

Telemetry (grep-stable):
  * ``[FINAL_DISPATCH_GUARD]`` — allow/deny verdict for the turn
  * ``[PRODUCT_ATTACHMENT_SUPPRESSED]`` — each cleared attachment batch
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional

from core.outbound_sanitizer import contains_handoff_promise
from modules.ai.brain.decision.actions import (
    ACTION_CLARIFY,
    ACTION_HANDOFF,
    ACTION_NARROW,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_OUT_OF_SCOPE,
    ACTION_PLATFORM_REPLY,
    ACTION_RECOMMEND_ADDON,
    ACTION_SEARCH_PRODUCTS,
    ACTION_SOCIAL_REPLY,
)
from modules.ai.brain.product_discovery_gate import (
    has_explicit_broad_browse_request,
    product_discovery_block_reason,
)
from modules.ai.brain.intent.non_commerce_classifier import (
    has_positive_commerce_intent,
)
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from modules.observability.delivery_mode import customer_wants_product_or_image

logger = logging.getLogger("nahla.final_dispatch_guard")

_TEAM_NOTIFICATION_RE = re.compile(
    r"(?:"
    r"وصلت\s*رسالتك"
    r"|س[اأ]?\s*خبر\s*(?:فريق|الفريق|فريق\s*المتجر)"
    r"|(?:س[اأ]?|سوف|راح)\s*(?:يتواصل|يرد|يتابع)\s*معك\s*(?:فريق|الفريق)?"
    r"|(?:فريق|الفريق)\s*(?:المتجر|الدعم)?\s*(?:س[اأ]?|سوف|راح)\s*(?:يتواصل|يرد|يتابع)"
    r")",
    re.UNICODE | re.IGNORECASE,
)

# Brain actions that may attach/send product cards when commerce is allowed.
_COMMERCE_ATTACHMENT_ACTIONS = frozenset({
    ACTION_SEARCH_PRODUCTS,
    ACTION_NARROW,
    ACTION_RECOMMEND_ADDON,
})

# Brain actions that always suppress product attachments.
_HARD_BLOCK_ACTIONS = frozenset({
    ACTION_HANDOFF,
    ACTION_SOCIAL_REPLY,
    ACTION_PLATFORM_REPLY,
    ACTION_ORDER_CONTEXT_UPDATE,
    ACTION_CLARIFY,
    ACTION_OUT_OF_SCOPE,
})


@dataclass(frozen=True)
class ProductAttachmentDispatchDecision:
    """Outcome of :func:`should_allow_product_attachment_dispatch`."""

    allow: bool
    reason: str
    product_discovery_blocked: bool = False

    @property
    def suppress_reason(self) -> str:
        """Alias for log ``[PRODUCT_ATTACHMENT_SUPPRESSED] reason=``."""
        return self.reason


def _contains_team_notification(text: str) -> bool:
    """Detect handoff / staff-escalation / team-notification copy."""
    if not text:
        return False
    if contains_handoff_promise(text):
        return True
    return bool(_TEAM_NOTIFICATION_RE.search(text))


def _payment_media_neutral_turn(
    *,
    inbound_message: str,
    brain_action: str,
    intent_name: str,
) -> bool:
    """True when inbound is payment/media ack without product ask."""
    action = (brain_action or "").strip()
    intent = (intent_name or "").strip().lower()
    if action in _COMMERCE_ATTACHMENT_ACTIONS:
        return False
    if has_positive_commerce_intent(intent):
        return False
    if customer_wants_product_or_image(
        inbound_text=inbound_message,
        brain_action=action,
    ):
        return False
    if has_explicit_broad_browse_request(inbound_message):
        return False

    norm = (inbound_message or "").strip().lower()
    if not norm:
        return False

    _PAYMENT_ACK = (
        "تم التحويل", "حولت", "دفعت", "تم الدفع", "حولتلك",
        "attached payment", "payment sent",
    )
    if any(p in norm for p in _PAYMENT_ACK):
        return True

    if intent in {"payment_confirm", "payment_receipt", "media_ack"}:
        return True

    return False


def should_allow_product_attachment_dispatch(
    *,
    brain_action: str = "",
    intent_name: str = "",
    intent_confidence: Optional[float] = None,
    inbound_message: str = "",
    reply_text: str = "",
    brain_handoff: bool = False,
    commerce_blocked: bool = False,
    fulfillment_discovery_blocked: bool = False,
    brain_state: Optional[dict] = None,
    active_order_state: Optional[dict] = None,
) -> ProductAttachmentDispatchDecision:
    """Require POSITIVE current-turn commerce permission for product cards.

    Returns ``allow=True`` only when the turn explicitly asks for products
    or browse — never from stale brain-state candidates alone.
    """
    action = (brain_action or "").strip()
    intent = (intent_name or "").strip()

    product_discovery_blocked = False
    try:
        state = MerchantConversationState.from_dict(dict(brain_state or {}))
        ctx = BrainContext(
            tenant_id=0,
            customer_phone="",
            message=inbound_message or "",
            intent=Intent(
                name=intent or "general",
                confidence=float(intent_confidence or 0.5),
                raw_message=inbound_message or "",
            ),
            state=state,
            facts=CommerceFacts(),
        )
        _pdr = product_discovery_block_reason(ctx, message=inbound_message)
        product_discovery_blocked = bool(_pdr)
    except Exception:  # noqa: BLE001
        product_discovery_blocked = False

    # ── Hard suppress (never attach) ───────────────────────────────
    if brain_handoff or action == ACTION_HANDOFF:
        return ProductAttachmentDispatchDecision(
            allow=False, reason="handoff", product_discovery_blocked=product_discovery_blocked,
        )

    if contains_handoff_promise(reply_text or "") or _contains_team_notification(
        reply_text or "",
    ):
        return ProductAttachmentDispatchDecision(
            allow=False, reason="handoff", product_discovery_blocked=product_discovery_blocked,
        )

    if action in _HARD_BLOCK_ACTIONS:
        _reason_map = {
            ACTION_SOCIAL_REPLY: "social",
            ACTION_PLATFORM_REPLY: "social",
            ACTION_ORDER_CONTEXT_UPDATE: "fulfillment_lock",
            ACTION_CLARIFY: "clarify",
            ACTION_OUT_OF_SCOPE: "non_commerce_block",
        }
        return ProductAttachmentDispatchDecision(
            allow=False,
            reason=_reason_map.get(action, "non_commerce_block"),
            product_discovery_blocked=product_discovery_blocked,
        )

    if commerce_blocked:
        return ProductAttachmentDispatchDecision(
            allow=False, reason="non_commerce_block",
            product_discovery_blocked=product_discovery_blocked,
        )

    if fulfillment_discovery_blocked:
        return ProductAttachmentDispatchDecision(
            allow=False, reason="fulfillment_lock",
            product_discovery_blocked=product_discovery_blocked,
        )

    if active_order_state and not has_positive_commerce_intent(
        intent, intent_confidence,
    ) and action not in _COMMERCE_ATTACHMENT_ACTIONS:
        if not customer_wants_product_or_image(
            inbound_text=inbound_message or "",
            brain_action=action,
        ) and not has_explicit_broad_browse_request(inbound_message or ""):
            return ProductAttachmentDispatchDecision(
                allow=False, reason="fulfillment_lock",
                product_discovery_blocked=product_discovery_blocked,
            )

    if product_discovery_blocked:
        return ProductAttachmentDispatchDecision(
            allow=False, reason="product_discovery_blocked",
            product_discovery_blocked=True,
        )

    if _payment_media_neutral_turn(
        inbound_message=inbound_message,
        brain_action=action,
        intent_name=intent,
    ):
        return ProductAttachmentDispatchDecision(
            allow=False, reason="payment_media_neutral",
            product_discovery_blocked=product_discovery_blocked,
        )

    # ── Positive allow paths ─────────────────────────────────────────
    if action in _COMMERCE_ATTACHMENT_ACTIONS:
        return ProductAttachmentDispatchDecision(
            allow=True, reason="commerce_action",
            product_discovery_blocked=False,
        )

    if has_positive_commerce_intent(intent, intent_confidence):
        return ProductAttachmentDispatchDecision(
            allow=True, reason="positive_commerce_intent",
            product_discovery_blocked=False,
        )

    if has_explicit_broad_browse_request(inbound_message or ""):
        return ProductAttachmentDispatchDecision(
            allow=True, reason="explicit_browse",
            product_discovery_blocked=False,
        )

    if customer_wants_product_or_image(
        inbound_text=inbound_message or "",
        brain_action=action,
    ):
        return ProductAttachmentDispatchDecision(
            allow=True, reason="visual_product_intent",
            product_discovery_blocked=False,
        )

    return ProductAttachmentDispatchDecision(
        allow=False, reason="no_positive_commerce_intent",
        product_discovery_blocked=product_discovery_blocked,
    )


_PRODUCT_MARKER_STRIP_RE = re.compile(
    r"\[PRODUCT:\s*[^\]\|\n]{1,120}(?:\s*\|[^\]]*)?\]",
    re.IGNORECASE,
)


def strip_product_markers_from_reply(reply_text: str) -> str:
    """Remove ``[PRODUCT:...]`` tokens from outbound reply text."""
    text = reply_text or ""
    if not text or "[PRODUCT:" not in text.upper():
        return text
    cleaned = _PRODUCT_MARKER_STRIP_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def suppress_product_attachments(
    *,
    product_attachments: Optional[List[dict]],
    reply_text: str,
    decision: ProductAttachmentDispatchDecision,
    tenant_id: Optional[int] = None,
    had_stale_candidates: bool = False,
) -> tuple[List[dict], str]:
    """Clear queued product cards and strip markers when dispatch is blocked."""
    attachments = list(product_attachments or [])
    reply_out = reply_text or ""

    if decision.allow:
        return attachments, reply_out

    reason = decision.reason
    if (
        attachments
        and not decision.allow
        and reason not in {"handoff", "social", "non_commerce_block"}
        and (had_stale_candidates or reason == "no_positive_commerce_intent")
    ):
        reason = "stale_candidate"

    if attachments:
        try:
            logger.info(
                "[PRODUCT_ATTACHMENT_SUPPRESSED] tenant=%s reason=%s "
                "count=%d ids=%s",
                tenant_id,
                reason,
                len(attachments),
                [a.get("id") for a in attachments],
            )
        except Exception:  # noqa: BLE001
            pass
        attachments = []

    if "[PRODUCT:" in (reply_out or "").upper():
        reply_out = strip_product_markers_from_reply(reply_out)

    return attachments, reply_out


def log_final_dispatch_guard(
    *,
    decision: ProductAttachmentDispatchDecision,
    tenant_id: Optional[int] = None,
    brain_action: str = "",
    intent_name: str = "",
) -> None:
    """Emit ``[FINAL_DISPATCH_GUARD]`` — one line per evaluation."""
    try:
        logger.info(
            "[FINAL_DISPATCH_GUARD] tenant=%s allow_product_cards=%s "
            "reason=%s brain_action=%s intent=%s "
            "product_discovery_blocked=%s",
            tenant_id,
            str(decision.allow).lower(),
            decision.reason,
            (brain_action or "?"),
            (intent_name or "?"),
            str(decision.product_discovery_blocked).lower(),
        )
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "ProductAttachmentDispatchDecision",
    "log_final_dispatch_guard",
    "should_allow_product_attachment_dispatch",
    "strip_product_markers_from_reply",
    "suppress_product_attachments",
]
