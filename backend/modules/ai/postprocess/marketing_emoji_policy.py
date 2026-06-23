"""
modules/ai/postprocess/marketing_emoji_policy.py
──────────────────────────────────────────────────
Platform-wide marketing emoji polish — metadata-driven, not template-driven.

Adds a small number of context-appropriate emojis to outbound Arabic replies
after truth guards and scrubs, without changing operational meaning, URLs,
prices, or button payloads.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

logger = logging.getLogger("nahla.marketing_emoji_policy")

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PRICE_RE = re.compile(
    r"(?:\d+(?:[.,]\d{1,2})?\s*(?:ر\.?\s*س|ريال|sar)\b|"
    r"\b(?:sar|ريال)\s*\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)
_IBAN_RE = re.compile(r"\bsa\s?\d{2}\s?(?:\d\s?){20}\b", re.IGNORECASE)
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")

_STYLE_MODES = frozenset({"none", "light", "marketing", "formal"})

# Purposes — semantic buckets, not product categories.
PURPOSE_GREETING = "greeting"
PURPOSE_CATALOG_BROWSE = "catalog_browse"
PURPOSE_ORDER_OR_CART = "order_or_cart"
PURPOSE_ADDRESS_REQUEST = "address_request"
PURPOSE_PAYMENT_INSTRUCTION = "payment_instruction"
PURPOSE_RECEIPT_REVIEW = "receipt_review_or_pending"
PURPOSE_CONFIRMED_SUCCESS = "confirmed_success"
PURPOSE_SHIPMENT_TRACKING = "shipment_created_or_tracking"
PURPOSE_SUPPORT = "support"
PURPOSE_WARNING = "warning_or_review"
PURPOSE_SOCIAL_THANKS = "social_thanks"
PURPOSE_NONE = "none"

_PURPOSE_EMOJI_POOLS: Dict[str, Tuple[str, ...]] = {
    PURPOSE_GREETING: ("😊", "🤝"),
    PURPOSE_CATALOG_BROWSE: ("🛍️", "✨"),
    PURPOSE_ORDER_OR_CART: ("🛒", "📦"),
    PURPOSE_ADDRESS_REQUEST: ("📍"),
    PURPOSE_PAYMENT_INSTRUCTION: ("💳", "🧾"),
    PURPOSE_RECEIPT_REVIEW: ("🧾", "⏳"),
    PURPOSE_CONFIRMED_SUCCESS: ("✅"),
    PURPOSE_SHIPMENT_TRACKING: ("🚚"),
    PURPOSE_SUPPORT: ("🤝"),
    PURPOSE_WARNING: ("⚠️"),
    PURPOSE_SOCIAL_THANKS: ("🤍", "😊"),
}

_CLAIM_SENSITIVE_EMOJI = frozenset({"✅", "🚚", "🔥", "⚠️"})
_HEAVY_MARKETING_EMOJI = frozenset({"🔥", "🏷️"})
_EMOTIONAL_EMOJI = frozenset({"❤️", "😍", "💖", "🌷", "🌹"})
_ROSE_ALLOWED_INBOUND_RE = re.compile(
    r"(?:شكر|شكرا|شكراً|امين|آمين|دعاء|الله|مبارك|تقبل)",
    re.UNICODE | re.IGNORECASE,
)
_OFFER_REPLY_RE = re.compile(
    r"(?:عرض|خصم|تخفيض|promo|discount|sale|🏷️)",
    re.UNICODE | re.IGNORECASE,
)

_PAYMENT_CONFIRMED_STATUSES = frozenset({
    "confirmed",
    "payment_confirmed",
    "payment_received",
    "paid",
})
_PAYMENT_PENDING_STATUSES = frozenset({
    "needs_confirmation",
    "pre_transfer_review",
    "amount_only_insufficient",
    "payment_pending_evidence",
    "awaiting_payment",
    "awaiting_receipt",
    "awaiting_payment_receipt",
})


@dataclass(frozen=True)
class MarketingEmojiContext:
    tenant_id: Optional[int] = None
    conversation_id: Optional[int] = None
    turn_id: Optional[int] = None
    inbound_text: str = ""
    intent_name: str = ""
    decision_action: str = ""
    decision_args: Dict[str, Any] = field(default_factory=dict)
    chosen_path: str = ""
    reply_instruction_path: str = ""
    stage: str = ""
    owner: str = ""
    navigator_step: str = ""
    catalog_navigation_source: str = ""
    order_status: str = ""
    awaiting_payment_receipt: bool = False
    payment_receipt_received: bool = False
    payment_evidence_status: str = ""
    shipment_evidence_ok: bool = False
    social_category: str = ""
    human_priority: bool = False
    locale: str = "ar"
    style_mode: str = "light"
    has_offer_evidence: bool = False
    policy_enabled: bool = True
    audit_only: bool = False


@dataclass(frozen=True)
class MarketingEmojiPolicyResult:
    reply: str
    changed: bool = False
    purpose: str = PURPOSE_NONE
    style_mode: str = "light"
    emoji_count_before: int = 0
    emoji_count_after: int = 0
    blocked_reason: str = ""
    selected_emojis: Tuple[str, ...] = ()


def is_marketing_emoji_policy_enabled() -> bool:
    try:
        from core.config import MARKETING_EMOJI_POLICY_ENABLED  # noqa: PLC0415

        return bool(MARKETING_EMOJI_POLICY_ENABLED)
    except Exception:  # noqa: BLE001
        return False


def is_marketing_emoji_policy_audit_only() -> bool:
    try:
        from core.config import MARKETING_EMOJI_POLICY_AUDIT_ONLY  # noqa: PLC0415

        return bool(MARKETING_EMOJI_POLICY_AUDIT_ONLY)
    except Exception:  # noqa: BLE001
        return False


def resolve_marketing_emoji_style_mode(ai_settings: Optional[Dict[str, Any]]) -> str:
    """Map tenant ai_settings to policy style mode."""
    settings = dict(ai_settings or {})
    explicit = str(settings.get("marketing_emoji_mode") or "").strip().lower()
    if explicit in _STYLE_MODES:
        return explicit
    tone = str(settings.get("reply_tone") or "friendly").strip().lower()
    tone_map = {
        "friendly": "light",
        "neutral": "light",
        "formal": "formal",
        "marketing": "marketing",
        "ودي": "light",
        "رسمي": "formal",
        "تسويقي": "marketing",
    }
    return tone_map.get(tone, "light")


def build_marketing_emoji_context(
    *,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    turn_id: Optional[int] = None,
    inbound_text: str = "",
    intent_name: str = "",
    decision_action: str = "",
    decision_args: Optional[Dict[str, Any]] = None,
    chosen_path: str = "",
    reply_instruction_path: str = "",
    stage: str = "",
    owner: str = "",
    navigator_step: str = "",
    catalog_navigation_source: str = "",
    order_status: str = "",
    awaiting_payment_receipt: bool = False,
    payment_receipt_received: bool = False,
    payment_evidence_status: str = "",
    shipment_evidence_ok: bool = False,
    social_category: str = "",
    human_priority: bool = False,
    locale: str = "ar",
    ai_settings: Optional[Dict[str, Any]] = None,
    reply_text: str = "",
    policy_enabled: Optional[bool] = None,
    audit_only: Optional[bool] = None,
) -> MarketingEmojiContext:
    args = dict(decision_args or {})
    offer_evidence = bool(_OFFER_REPLY_RE.search(reply_text or ""))
    if args.get("discovery_output_kind") in {"offer", "discount"}:
        offer_evidence = True
    return MarketingEmojiContext(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        turn_id=turn_id,
        inbound_text=inbound_text or "",
        intent_name=str(intent_name or "").strip().lower(),
        decision_action=str(decision_action or "").strip().lower(),
        decision_args=args,
        chosen_path=str(chosen_path or args.get("chosen_path") or "").strip().lower(),
        reply_instruction_path=str(reply_instruction_path or "").strip().lower(),
        stage=str(stage or "").strip().lower(),
        owner=str(owner or args.get("turn_owner") or "").strip().lower(),
        navigator_step=str(navigator_step or args.get("navigator_step") or "").strip().lower(),
        catalog_navigation_source=str(catalog_navigation_source or "").strip().lower(),
        order_status=str(order_status or "").strip().lower(),
        awaiting_payment_receipt=bool(awaiting_payment_receipt),
        payment_receipt_received=bool(payment_receipt_received),
        payment_evidence_status=str(payment_evidence_status or "").strip().lower(),
        shipment_evidence_ok=bool(shipment_evidence_ok),
        social_category=str(social_category or args.get("social_category") or "").strip().lower(),
        human_priority=bool(human_priority),
        locale=str(locale or "ar").strip().lower(),
        style_mode=resolve_marketing_emoji_style_mode(ai_settings),
        has_offer_evidence=offer_evidence,
        policy_enabled=bool(policy_enabled if policy_enabled is not None else is_marketing_emoji_policy_enabled()),
        audit_only=bool(audit_only if audit_only is not None else is_marketing_emoji_policy_audit_only()),
    )


def _count_emojis(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def _is_arabic_context(ctx: MarketingEmojiContext, reply: str) -> bool:
    locale = (ctx.locale or "").lower()
    if locale.startswith("ar"):
        return True
    combined = f"{ctx.inbound_text} {reply}"
    return bool(_ARABIC_RE.search(combined))


def _payment_confirmed(ctx: MarketingEmojiContext) -> bool:
    pe = (ctx.payment_evidence_status or "").strip().lower()
    if pe in _PAYMENT_CONFIRMED_STATUSES:
        return True
    if ctx.payment_receipt_received and pe not in _PAYMENT_PENDING_STATUSES:
        return True
    status = (ctx.order_status or "").strip().lower()
    return status in {"complete", "payment_submitted", "paid"}


def _payment_pending(ctx: MarketingEmojiContext) -> bool:
    pe = (ctx.payment_evidence_status or "").strip().lower()
    if pe in _PAYMENT_PENDING_STATUSES:
        return True
    if ctx.awaiting_payment_receipt:
        return True
    status = (ctx.order_status or "").strip().lower()
    return status in {"awaiting_payment", "awaiting_receipt"}


def resolve_message_purpose(ctx: MarketingEmojiContext, reply: str) -> str:
    """Resolve semantic purpose from metadata — reply text is secondary only."""
    path = ctx.reply_instruction_path
    action = ctx.decision_action
    chosen = ctx.chosen_path
    nav = ctx.navigator_step
    catalog_src = ctx.catalog_navigation_source
    intent = ctx.intent_name
    social = ctx.social_category

    if path == "payment_evidence_soft_ack" or path == "payment_claim_ack":
        return PURPOSE_RECEIPT_REVIEW
    if path == "payment_receipt_ack":
        return PURPOSE_CONFIRMED_SUCCESS if _payment_confirmed(ctx) else PURPOSE_RECEIPT_REVIEW
    if path in {"map_image_ack", "address_ingest_ack"}:
        return PURPOSE_ADDRESS_REQUEST
    if path in {"payment_method_ack", "payment_transfer_promise"}:
        return PURPOSE_PAYMENT_INSTRUCTION
    if path == "order_slot_prompt":
        return PURPOSE_ORDER_OR_CART

    if action in {"handoff_to_human", "talk_to_human"} or intent in {"talk_to_human", "employee_not_responding"}:
        return PURPOSE_SUPPORT
    if action == "social_reply" or social in {"thanks", "dua", "greeting", "blessing"}:
        if social in {"thanks", "dua"} or _ROSE_ALLOWED_INBOUND_RE.search(ctx.inbound_text or ""):
            return PURPOSE_SOCIAL_THANKS
        return PURPOSE_GREETING
    if action in {"greet", "action_greet"} or intent == "greet":
        return PURPOSE_GREETING

    if ctx.shipment_evidence_ok and action in {"track_order", "action_track_order"}:
        return PURPOSE_SHIPMENT_TRACKING

    if (
        nav in {"native_catalog_entry", "show_groups", "show_group_products", "top_products_fallback"}
        or "catalog_navigation" in chosen
        or catalog_src in {"native_catalog", "groups", "group_products", "top_fallback", "top_products"}
        or action == "catalog_navigate"
    ):
        return PURPOSE_CATALOG_BROWSE

    if action in {
        "propose_draft_order",
        "start_order",
        "stash_address_pre_product",
        "order_context_update",
    } or ctx.stage in {"ordering", "checkout"}:
        if action == "stash_address_pre_product" or "address" in chosen:
            return PURPOSE_ADDRESS_REQUEST
        return PURPOSE_ORDER_OR_CART

    if action in {"send_payment_link", "payment_transfer_promise", "pay_now", "ask_payment_info"}:
        if _payment_confirmed(ctx):
            return PURPOSE_CONFIRMED_SUCCESS
        return PURPOSE_PAYMENT_INSTRUCTION

    if action in {"ask_shipping", "ask_location"} or intent in {"ask_shipping", "ask_location"}:
        if ctx.shipment_evidence_ok:
            return PURPOSE_SHIPMENT_TRACKING
        return PURPOSE_ADDRESS_REQUEST if intent == "ask_location" else PURPOSE_ORDER_OR_CART

    if _payment_pending(ctx):
        return PURPOSE_RECEIPT_REVIEW
    if _payment_confirmed(ctx):
        return PURPOSE_CONFIRMED_SUCCESS

    # Secondary text hints — only when metadata is thin.
    text = (reply or "").lower()
    if any(k in text for k in ("موقع", "العنوان", "الرمز المختصر", "خرائط")):
        return PURPOSE_ADDRESS_REQUEST
    if any(k in text for k in ("إيصال", "ايصال", "تحويل", "دفع")):
        return PURPOSE_RECEIPT_REVIEW if _payment_pending(ctx) else PURPOSE_PAYMENT_INSTRUCTION
    if any(k in text for k in ("كتالوج", "منتجات", "اختر")):
        return PURPOSE_CATALOG_BROWSE

    return PURPOSE_NONE


def _style_max_new_emojis(ctx: MarketingEmojiContext, reply: str, purpose: str) -> int:
    existing = _count_emojis(reply)
    lines = [ln for ln in (reply or "").splitlines() if ln.strip()]
    length = len((reply or "").strip())
    if length < 80 or len(lines) <= 1:
        base_cap = 1
    elif length < 200 or len(lines) <= 3:
        base_cap = 2
    else:
        base_cap = 3

    mode = (ctx.style_mode or "light").lower()
    if mode == "none":
        return 0
    if mode == "formal":
        if purpose in {PURPOSE_ADDRESS_REQUEST, PURPOSE_RECEIPT_REVIEW, PURPOSE_PAYMENT_INSTRUCTION, PURPOSE_SUPPORT}:
            return max(0, min(1, base_cap) - existing)
        return 0
    if mode == "light":
        cap = 1 if base_cap <= 1 else min(2, base_cap)
        return max(0, cap - existing)
    # marketing
    return max(0, base_cap - existing)


def _pick_emojis(ctx: MarketingEmojiContext, purpose: str, slots: int) -> List[str]:
    if slots <= 0 or purpose == PURPOSE_NONE:
        return []
    pool = list(_PURPOSE_EMOJI_POOLS.get(purpose) or ())
    if ctx.has_offer_evidence and purpose in {PURPOSE_CATALOG_BROWSE, PURPOSE_ORDER_OR_CART}:
        pool = ["✨", "🏷️"] + pool
    seed_raw = "|".join(
        str(p)
        for p in (
            ctx.tenant_id or 0,
            ctx.conversation_id or 0,
            ctx.turn_id or 0,
            purpose,
            ctx.intent_name,
            ctx.decision_action,
        )
    )
    seed = int(hashlib.sha256(seed_raw.encode("utf-8")).hexdigest(), 16)
    picked: List[str] = []
    seen: set[str] = set()
    for i in range(len(pool) * 2):
        if len(picked) >= slots:
            break
        emoji = pool[(seed + i) % len(pool)] if pool else ""
        if not emoji or emoji in seen:
            continue
        if not _emoji_allowed(ctx, purpose, emoji):
            continue
        seen.add(emoji)
        picked.append(emoji)
    return picked


def _emoji_allowed(ctx: MarketingEmojiContext, purpose: str, emoji: str) -> bool:
    if emoji == "✅" and purpose != PURPOSE_CONFIRMED_SUCCESS:
        return False
    if emoji == "🚚" and purpose != PURPOSE_SHIPMENT_TRACKING:
        return False
    if emoji in _HEAVY_MARKETING_EMOJI and not ctx.has_offer_evidence:
        return False
    if emoji in _EMOTIONAL_EMOJI:
        if emoji in {"🌷", "🌹"}:
            return purpose == PURPOSE_SOCIAL_THANKS and _ROSE_ALLOWED_INBOUND_RE.search(
                ctx.inbound_text or ""
            )
        return purpose in {PURPOSE_SOCIAL_THANKS, PURPOSE_GREETING}
    return True


def _protected_spans_preserved(original: str, candidate: str) -> bool:
    if original == candidate:
        return True
    for pattern in (_URL_RE, _PRICE_RE, _IBAN_RE):
        orig = set(pattern.findall(original or ""))
        cand = set(pattern.findall(candidate or ""))
        if cand != orig:
            return False
    # Product/order tokens: strip emoji and compare alphanumeric chunks.
    def _core(t: str) -> str:
        return _EMOJI_RE.sub("", t or "").strip()

    if _core(original) != _core(candidate):
        # Only whitespace/emoji delta is allowed at end of first line.
        orig_lines = (_core(original) or "").splitlines()
        cand_lines = (_core(candidate) or "").splitlines()
        if orig_lines != cand_lines:
            return False
    return True


def _inject_emojis(reply: str, emojis: Sequence[str]) -> str:
    if not emojis:
        return reply
    lines = (reply or "").strip().split("\n", 1)
    first = lines[0].rstrip()
    if _EMOJI_RE.search(first):
        return reply
    emoji_str = "".join(emojis)
    first = f"{first} {emoji_str}".strip()
    if len(lines) > 1:
        return f"{first}\n{lines[1]}"
    return first


def apply_marketing_emoji_policy(
    reply: str,
    ctx: MarketingEmojiContext,
) -> MarketingEmojiPolicyResult:
    """Apply metadata-driven emoji polish. Never raises."""
    original = (reply or "").strip()
    if not original:
        return MarketingEmojiPolicyResult(reply=original)

    before = _count_emojis(original)
    if not ctx.policy_enabled:
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            blocked_reason="policy_disabled",
        )
    if not _is_arabic_context(ctx, original):
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            blocked_reason="non_arabic",
        )
    if ctx.human_priority or ctx.intent_name in {"talk_to_human", "employee_not_responding"}:
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            blocked_reason="sensitive_turn",
        )
    if (ctx.style_mode or "").lower() == "none":
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            blocked_reason="style_none",
        )

    purpose = resolve_message_purpose(ctx, original)
    if purpose == PURPOSE_NONE:
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            purpose=purpose,
            style_mode=ctx.style_mode,
            blocked_reason="no_purpose",
        )

    slots = _style_max_new_emojis(ctx, original, purpose)
    if slots <= 0:
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            purpose=purpose,
            style_mode=ctx.style_mode,
            blocked_reason="emoji_cap_reached",
        )

    selected = tuple(_pick_emojis(ctx, purpose, slots))
    if not selected:
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            purpose=purpose,
            style_mode=ctx.style_mode,
            blocked_reason="no_allowed_emoji",
        )

    candidate = _inject_emojis(original, selected)
    after = _count_emojis(candidate)
    if after > before + slots:
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            purpose=purpose,
            style_mode=ctx.style_mode,
            blocked_reason="would_exceed_cap",
        )
    if not _protected_spans_preserved(original, candidate):
        return MarketingEmojiPolicyResult(
            reply=original,
            emoji_count_before=before,
            emoji_count_after=before,
            purpose=purpose,
            style_mode=ctx.style_mode,
            blocked_reason="protected_span_changed",
            selected_emojis=selected,
        )

    changed = candidate != original
    if changed and ctx.audit_only:
        logger.info(
            "[MARKETING_EMOJI_POLICY] audit_only tenant=%s purpose=%s mode=%s "
            "before=%d after=%d emojis=%s preview=%r",
            ctx.tenant_id,
            purpose,
            ctx.style_mode,
            before,
            after,
            "".join(selected),
            candidate[:80],
        )
        return MarketingEmojiPolicyResult(
            reply=original,
            changed=False,
            purpose=purpose,
            style_mode=ctx.style_mode,
            emoji_count_before=before,
            emoji_count_after=before,
            blocked_reason="audit_only",
            selected_emojis=selected,
        )

    if changed:
        logger.info(
            "[MARKETING_EMOJI_POLICY] tenant=%s purpose=%s mode=%s "
            "before=%d after=%d emojis=%s",
            ctx.tenant_id,
            purpose,
            ctx.style_mode,
            before,
            after,
            "".join(selected),
        )

    return MarketingEmojiPolicyResult(
        reply=candidate,
        changed=changed,
        purpose=purpose,
        style_mode=ctx.style_mode,
        emoji_count_before=before,
        emoji_count_after=after,
        selected_emojis=selected,
    )


__all__ = [
    "MarketingEmojiContext",
    "MarketingEmojiPolicyResult",
    "apply_marketing_emoji_policy",
    "build_marketing_emoji_context",
    "is_marketing_emoji_policy_audit_only",
    "is_marketing_emoji_policy_enabled",
    "resolve_marketing_emoji_style_mode",
    "resolve_message_purpose",
]
