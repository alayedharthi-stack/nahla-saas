"""
brain/relational/state.py
─────────────────────────
Pure relational-layer computation. Commit 1 of the May 2026
relational-architecture rollout: telemetry + state only, ZERO
behaviour change.

Why this module exists
──────────────────────
Tenant 33 production audit (May 2026) found three classes of
failures the existing intent + stance + social-classifier stack
cannot answer:

  * Customer praises packaging after delivery -> bot runs a customer
    lookup and replies "no orders found for your number".
  * Customer complains about a Hajj-period shipping delay -> bot
    replies with a flat "we received your note, will follow up"
    escalation ACK.
  * Customer asks where the apiary is -> bot ships "موقعنا 📍\n{url}"
    with zero conversational warmth.

The system answers "what does the customer want done?" but never
"what relational moment is the customer in right now?". This module
owns that second question.

Architectural rule (pinned in :mod:`contracts`)
───────────────────────────────────────────────
    Relational layer may shape the conversation, but must never
    fabricate business state. It may only influence tone, framing,
    empathy expression, transactional-artifact suppression, and
    action prioritisation.

The verdict carries:
  * ``moment``           — closed-enum :class:`ConversationMoment`
  * ``lifecycle_stage``  — closed-enum :class:`LifecycleStage`
  * ``sentiment``        — closed-enum :class:`Sentiment`
  * ``post_purchase_window`` — closed-enum :class:`PostPurchaseWindow`
  * ``urgency``          — closed-enum :class:`Urgency`
  * ``advisory_for_brain``  — plain-English description (NOT outbound copy)
  * ``framing_directive``   — short tone hint (NOT outbound copy)
  * ``reason``           — short token, log-greppable

It carries NO field whose name matches a business-fact pattern
(see :data:`contracts.BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS`); the
architectural invariant test fails the build if anyone adds one.

Determinism + safety
────────────────────
``compute_relational_state`` is a pure function. It NEVER:
  * touches the DB,
  * raises,
  * mutates any input,
  * calls a network / I/O / LLM.

Inputs may be ``None`` / empty / garbage; outputs are always a
populated :class:`RelationalState` with sensible defaults.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .contracts import ARCHITECTURAL_RULE_TEXT
from .dedup_suppression import (
    text_indicates_religious_ritual,
    text_indicates_seasonal_greeting,
)
from .moments import (
    ConversationMoment,
    LifecycleStage,
    PostPurchaseWindow,
    Sentiment,
    Urgency,
    to_log_tokens,
)

logger = logging.getLogger("nahla.relational")


# ── Lightweight Arabic normaliser (mirrors core.payment_intent /
# core.payment_evidence). Local copy so this module has no
# dependency on the order/payment stack. ────────────────────────────
_AR_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670]")


def _normalise_arabic(text: Optional[str]) -> str:
    if not text:
        return ""
    try:
        t = _AR_DIACRITICS_RE.sub("", str(text))
        t = t.replace("ـ", "")
        t = (
            t.replace("أ", "ا")
             .replace("إ", "ا")
             .replace("آ", "ا")
             .replace("ى", "ي")
             .replace("ة", "ه")
        )
        return t.lower().strip()
    except Exception:
        return ""


# ── Lexicons (kept tight on purpose; broaden only with evidence) ────

# Hesitation / pre-purchase concern markers.
_HESITATION_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "محتار", "مش متاكد", "مش متاكده", "مو متاكد", "مو متاكده",
    "خايف", "خايفه", "خوف", "اخاف", "متردد", "متردده",
    "ابي اتاكد", "ابغى اتاكد", "ابي اطمن", "ودي اطمن",
    "اول مره", "اول طلب", "ما جربت من قبل", "ما جربت قبل",
    "كيف اتاكد", "تنصحوني", "ابي رايكم",
    "not sure", "im hesitant", "first time",
))

# Shipping-delay markers (customer-facing complaints).
_SHIPPING_DELAY_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "تاخر", "تاخرت", "متاخر", "متاخره", "ما وصل", "ما وصلت",
    "لين الحين ما", "للحين ما", "متى يوصل", "وين الشحنه",
    "وش اخبار شحنتي", "شحنتي وين", "بعد ما وصلت", "ما استلمت",
    "delayed", "late", "still waiting", "havent received",
    "hasnt arrived", "where is my order", "where is the order",
))

# Product-quality complaint markers.
_PRODUCT_QUALITY_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "سوء تخزين", "سيء التخزين", "تخزين سيء", "تخزين خاطئ",
    "تالف", "تالفه", "خربان", "خربانه", "فاسد", "فاسده",
    "طعمه سيء", "طعمها سيء", "طعم غريب", "ريحه غريبه",
    "غير ممتاز", "خرب", "ما عجبني", "ما عجبتني", "ما يستاهل",
    "بايخ", "بايخه", "ما هو زي قبل", "تغيرت الجوده",
    "damaged", "spoiled", "bad quality", "stale", "tastes bad",
))

# Generic complaint markers (catch-all).
_GENERIC_COMPLAINT_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "شكوى", "شكوي", "غش", "ابي حقي", "اشتكي",
    "غير راضي", "غير راضيه", "مو راضي", "مو راضيه",
    "مزعجه", "مزعج", "زعلان", "زعلانه", "محبط", "محبطه",
    "complaint", "unacceptable", "disappointed", "frustrated",
))

# Strong-negative tokens that bump sentiment to ``angry``.
_ANGER_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "غش", "نصب", "فضيحه", "فضيحة", "هرج", "كذب",
    "scam", "fraud", "ridiculous", "outrageous",
))

# Recovery cues — phrases a customer types when they're back AFTER
# a complaint cycle. We only fire RECOVERY_AFTER_FAILURE when the
# rolling summary OR recent_turns indicate a prior complaint AND
# the inbound shows continuation rather than fresh anger.
_RECOVERY_CONTINUATION_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "اخر تحديث", "بعدكم ما", "تواصلت معكم", "مفيش رد", "مافي رد",
    "اعتذركم وصل", "كلامكم وصل", "الموضوع لسه", "الموضوع للحين",
    "still no update", "any update", "follow up", "any news",
))

# Praise that's heavy enough to cross the relational threshold even
# in long messages. Tighter than social_classifier's ``compliment``
# — these are the ones a 200-character review would carry.
_HEAVY_PRAISE_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "اكثر من رائع", "اكثر من ممتاز", "ما شاء الله",
    "تسلم الايادي", "تسلم الأيادي", "بيض الله وجهك",
    "ما قصرتم", "ما قصرتو", "كفو", "رفعتو راسنا", "رفعتم راسنا",
    "احسنت", "احسنتم", "احسنتو", "والله رايع", "والله ممتاز",
    "amazing", "excellent", "outstanding", "best honey",
    "highly recommend", "perfect quality", "loved it",
))

# Generic gratitude tokens (subset of social_classifier thanks; we
# keep our own copy so we don't leak a dependency on that module's
# private constants).
_GRATITUDE_MARKERS: Tuple[str, ...] = tuple(_normalise_arabic(s) for s in (
    "شكرا", "شكراً", "مشكور", "مشكوره", "تسلم", "يعطيك العافيه",
    "يعطيكم العافيه", "ربي يعافيك",
    "thanks", "thank you", "appreciate it",
))


# ── Public dataclass ────────────────────────────────────────────────
# IMPORTANT: every field name here is checked against
# ``BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS``. Renaming or adding a
# field requires the architectural test to still pass.


@dataclass(frozen=True)
class RelationalState:
    """Verdict of the relational layer for a single conversation
    turn. Never carries business state; only carries shape /
    framing signals downstream layers may consume.

    ARCHITECTURAL RULE (pinned in :mod:`contracts`):
        ``ARCHITECTURAL_RULE_TEXT``
    """

    moment: ConversationMoment = ConversationMoment.NONE
    lifecycle_stage: LifecycleStage = LifecycleStage.UNKNOWN
    sentiment: Sentiment = Sentiment.UNKNOWN
    post_purchase_window: PostPurchaseWindow = PostPurchaseWindow.NONE
    urgency: Urgency = Urgency.NORMAL
    # Plain-English description of the moment for the brain prompt
    # overlay. NEVER outbound copy. NEVER an imperative ("say X").
    # The brain reads this for understanding only.
    advisory_for_brain: str = ""
    # Short tone hint (e.g. "warmer than baseline", "no upsell").
    # Also for the prompt overlay; also non-imperative.
    framing_directive: str = ""
    # Stable short token explaining which rule fired. For logs.
    reason: str = ""

    def to_log_dict(self) -> Dict[str, Any]:
        """Flat dict for the ``[CX]`` log line. Operators grep
        ``cx_moment=`` / ``cx_lifecycle=`` / ``cx_sentiment=``."""
        toks = to_log_tokens(
            moment=self.moment,
            lifecycle=self.lifecycle_stage,
            sentiment=self.sentiment,
            post_purchase=self.post_purchase_window,
            urgency=self.urgency,
        )
        return {
            "cx_moment":        toks.moment,
            "cx_lifecycle":     toks.lifecycle,
            "cx_sentiment":     toks.sentiment,
            "cx_post_purchase": toks.post_purchase,
            "cx_urgency":       toks.urgency,
            "cx_reason":        self.reason or "",
        }

    def is_inert(self) -> bool:
        """True when this verdict carries no relational signal —
        downstream layers can short-circuit (no prompt overlay, no
        suppression, no logging)."""
        return (
            self.moment == ConversationMoment.NONE
            and self.sentiment in (Sentiment.UNKNOWN, Sentiment.NEUTRAL)
        )


# ── Helpers ─────────────────────────────────────────────────────────


def _detect_lifecycle(profile: Optional[Dict[str, Any]]) -> LifecycleStage:
    """Map a CustomerProfile-shaped dict to a lifecycle stage.

    Reads only the metric-typed fields the relational layer is
    allowed to observe (``total_orders``, ``rfm_segment``,
    ``customer_status``, ``churn_risk_score``, ``is_returning``).
    Never touches order_id / payment / shipping fields.
    """
    if not profile or not isinstance(profile, dict):
        return LifecycleStage.UNKNOWN
    try:
        total_orders = int(profile.get("total_orders") or 0)
    except Exception:
        total_orders = 0
    try:
        churn_score = float(profile.get("churn_risk_score") or 0.0)
    except Exception:
        churn_score = 0.0
    rfm_segment = str(profile.get("rfm_segment") or "").strip().lower()
    customer_status = str(profile.get("customer_status") or "").strip().lower()
    is_returning = bool(profile.get("is_returning"))

    if customer_status == "lead" or total_orders <= 0:
        return LifecycleStage.FIRST_TIME
    if total_orders >= 5 or rfm_segment in ("vip", "champion", "champions"):
        return LifecycleStage.LOYAL
    if churn_score >= 0.7 and total_orders >= 1:
        return LifecycleStage.LAPSED
    if total_orders >= 2:
        return LifecycleStage.REPEAT
    if is_returning or total_orders == 1:
        return LifecycleStage.RETURNING
    return LifecycleStage.UNKNOWN


def _detect_post_purchase_window(
    *,
    last_shipment_event_at: Optional[datetime],
    order_status: str,
    now: Optional[datetime] = None,
) -> PostPurchaseWindow:
    """Bucket the recency of the last shipment event. ``now`` is
    injectable for deterministic tests.

    NOTE: ``order_status`` is read but never echoed in the verdict
    field names (it comes in via state, not via a relational field).
    """
    now = now or datetime.now(timezone.utc)
    status = str(order_status or "").strip().lower()
    if not last_shipment_event_at:
        # No shipment event known. Use the soft signals from
        # status only.
        if status in ("shipped", "out_for_delivery"):
            return PostPurchaseWindow.AWAITING_DELIVERY
        return PostPurchaseWindow.NONE
    try:
        # Naive datetimes — assume UTC for comparison.
        evt = last_shipment_event_at
        if evt.tzinfo is None:
            evt = evt.replace(tzinfo=timezone.utc)
        delta = now - evt
    except Exception:
        return PostPurchaseWindow.NONE
    if status in ("shipped", "out_for_delivery"):
        return PostPurchaseWindow.AWAITING_DELIVERY
    if delta <= timedelta(hours=48):
        return PostPurchaseWindow.DELIVERED_WITHIN_48H
    if delta <= timedelta(days=7):
        return PostPurchaseWindow.DELIVERED_WITHIN_7D
    return PostPurchaseWindow.DELIVERED_OLDER


def _detect_sentiment(
    *,
    norm_inbound: str,
    summary_sentiment: Optional[str],
    has_complaint_marker: bool,
    has_anger_marker: bool,
    has_gratitude: bool,
    has_heavy_praise: bool,
) -> Sentiment:
    """Coarse, rule-driven sentiment. Heavy markers beat soft
    summary; gratitude / praise beat absence. Default neutral
    when nothing fires."""
    if has_anger_marker:
        return Sentiment.ANGRY
    if has_complaint_marker:
        return Sentiment.FRUSTRATED
    if has_heavy_praise or has_gratitude:
        return Sentiment.POSITIVE
    s = str(summary_sentiment or "").strip().lower()
    if s in ("angry",):
        return Sentiment.ANGRY
    if s in ("frustrated", "negative", "upset"):
        return Sentiment.FRUSTRATED
    if s in ("concerned", "worried", "anxious"):
        return Sentiment.CONCERNED
    if s in ("positive", "happy", "satisfied"):
        return Sentiment.POSITIVE
    if s in ("neutral", ""):
        return Sentiment.NEUTRAL
    return Sentiment.UNKNOWN


def _scan_any(blob: str, markers: Tuple[str, ...]) -> bool:
    if not blob or not markers:
        return False
    for m in markers:
        if m and m in blob:
            return True
    return False


def _summary_indicates_prior_complaint(summary: Any) -> bool:
    """Detect whether the rolling summary already records a prior
    complaint moment. Tolerant to dict / str / None shapes."""
    if not summary:
        return False
    if isinstance(summary, dict):
        for key in (
            "last_intent", "intents", "tags", "labels",
            "recent_complaint", "complaint_logged",
        ):
            v = summary.get(key)
            if isinstance(v, str) and "complaint" in v.lower():
                return True
            if isinstance(v, list) and any(
                isinstance(x, str) and "complaint" in x.lower() for x in v
            ):
                return True
            if isinstance(v, bool) and v:
                return True
        return False
    if isinstance(summary, str):
        return "complaint" in summary.lower() or "شكوى" in summary
    return False


# ── Public function ────────────────────────────────────────────────


def compute_relational_state(
    *,
    inbound_text: Optional[str],
    intent_name: Optional[str] = None,
    stance: Optional[str] = None,
    social_category: Optional[str] = None,
    customer_profile: Optional[Dict[str, Any]] = None,
    order_state: Optional[Dict[str, Any]] = None,
    conversation_summary: Any = None,
    recent_customer_messages: Optional[List[str]] = None,
    last_shipment_event_at: Optional[datetime] = None,
    handoff_signals: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> RelationalState:
    """Pure, never-raising classifier returning a populated
    :class:`RelationalState`.

    Parameters
    ----------
    inbound_text:
        The customer's current message body. ``None`` / empty is
        valid and yields an inert verdict.
    intent_name:
        The intent classifier's ``Intent.name``. Used as one
        signal among many — moment is independent of intent.
    stance:
        The stance detector's verdict (``STANCE_*``). Independent
        of moment; we only consult it for tie-breakers.
    social_category:
        Output of the social classifier (``thanks`` / ``blessing``
        / ``strong_praise`` / …) when it fired. May be ``None`` for
        non-social messages (most messages, by design).
    customer_profile:
        ``CustomerProfile`` shaped dict; we read only metric-typed
        fields (see :func:`_detect_lifecycle`).
    order_state:
        ``MerchantConversationState.order_prep`` shaped dict; we
        read only ``order_status`` and ``selected_product`` for
        funnel detection. Never echoed in our output fields.
    conversation_summary:
        Rolling summary (dict / str). Used to detect prior
        complaint markers for ``RECOVERY_AFTER_FAILURE``.
    recent_customer_messages:
        Last ~3 customer messages (oldest first). Used as
        secondary evidence for sentiment / recovery detection.
    last_shipment_event_at:
        Datetime of the last outbound shipment notice (if any).
        Drives the post-purchase window bucket.
    handoff_signals:
        Optional ``{"is_complaint", "tier", "is_explicit"}`` from
        ``handoff_detector``. We never invent these — caller
        passes them when known.
    now:
        Injection point for deterministic tests.

    Returns
    -------
    :class:`RelationalState`
        Always populated. Never raises.

    Note
    ----
    This function does not implement
    ``RELATIONAL_LAYER_ENABLED`` gating. Callers (the pipeline)
    decide whether to call it at all. Once called, it always
    returns a verdict.
    """
    # Guard rails: every input may be ``None``; never raise.
    try:
        return _compute_unsafe(
            inbound_text=inbound_text,
            intent_name=intent_name,
            stance=stance,
            social_category=social_category,
            customer_profile=customer_profile,
            order_state=order_state,
            conversation_summary=conversation_summary,
            recent_customer_messages=recent_customer_messages,
            last_shipment_event_at=last_shipment_event_at,
            handoff_signals=handoff_signals,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[CX] compute_relational_state failed (returning inert): %s",
            exc,
        )
        return RelationalState()


def _compute_unsafe(
    *,
    inbound_text: Optional[str],
    intent_name: Optional[str],
    stance: Optional[str],
    social_category: Optional[str],
    customer_profile: Optional[Dict[str, Any]],
    order_state: Optional[Dict[str, Any]],
    conversation_summary: Any,
    recent_customer_messages: Optional[List[str]],
    last_shipment_event_at: Optional[datetime],
    handoff_signals: Optional[Dict[str, Any]],
    now: Optional[datetime],
) -> RelationalState:
    norm = _normalise_arabic(inbound_text or "")

    # Fast exit: nothing to classify.
    if not norm and not (handoff_signals or {}).get("is_explicit_handoff_request"):
        return RelationalState()

    intent_norm = str(intent_name or "").strip().lower()
    stance_norm = str(stance or "").strip().lower()
    social_norm = str(social_category or "").strip().lower()
    handoff = handoff_signals or {}
    summary_dict = conversation_summary if isinstance(conversation_summary, dict) else None
    summary_sentiment = (
        summary_dict.get("sentiment") if summary_dict else None
    )
    order_status = ""
    has_selected_product = False
    if isinstance(order_state, dict):
        order_status = str(order_state.get("order_status") or "")
        has_selected_product = bool(order_state.get("selected_product"))

    # ── Signal extraction ──
    has_shipping_delay = _scan_any(norm, _SHIPPING_DELAY_MARKERS)
    has_quality_complaint = _scan_any(norm, _PRODUCT_QUALITY_MARKERS)
    has_generic_complaint = _scan_any(norm, _GENERIC_COMPLAINT_MARKERS)
    has_anger = _scan_any(norm, _ANGER_MARKERS)
    has_hesitation = _scan_any(norm, _HESITATION_MARKERS)
    has_recovery_cue = _scan_any(norm, _RECOVERY_CONTINUATION_MARKERS)
    has_heavy_praise = _scan_any(norm, _HEAVY_PRAISE_MARKERS)
    has_gratitude = _scan_any(norm, _GRATITUDE_MARKERS)

    is_explicit_handoff = bool(handoff.get("is_explicit_handoff_request"))
    handoff_is_complaint = bool(handoff.get("is_complaint"))

    has_complaint_marker = (
        has_shipping_delay or has_quality_complaint
        or has_generic_complaint or has_anger or handoff_is_complaint
    )

    # ── Composite dimensions ──
    lifecycle = _detect_lifecycle(customer_profile)
    sentiment = _detect_sentiment(
        norm_inbound=norm,
        summary_sentiment=summary_sentiment,
        has_complaint_marker=has_complaint_marker,
        has_anger_marker=has_anger,
        has_gratitude=has_gratitude or social_norm in ("thanks", "compliment", "strong_praise"),
        has_heavy_praise=has_heavy_praise or social_norm == "strong_praise",
    )
    ppw = _detect_post_purchase_window(
        last_shipment_event_at=last_shipment_event_at,
        order_status=order_status,
        now=now,
    )
    urgency = (
        Urgency.HIGH if (has_anger or (has_shipping_delay and ppw == PostPurchaseWindow.AWAITING_DELIVERY))
        else (
            Urgency.NORMAL
            if (has_complaint_marker or has_recovery_cue)
            else Urgency.LOW
        )
    )

    # ── Moment classification (priority order matters) ──
    # 1) Explicit handoff request beats everything.
    if is_explicit_handoff or intent_norm == "talk_to_human":
        return _make(
            ConversationMoment.ESCALATION_REQUEST, lifecycle, sentiment, ppw, urgency,
            reason="explicit_handoff_request",
            advisory=(
                "Customer is asking to talk to a human. Acknowledge "
                "the request warmly, capture the topic if any, then "
                "let the existing handoff path do its job."
            ),
            framing="warm_handoff_no_brushoff",
        )

    # 2) Recovery after failure: prior complaint in summary AND
    #    the customer is back with a continuation cue or a fresh
    #    complaint marker.
    if _summary_indicates_prior_complaint(conversation_summary) and (
        has_recovery_cue or has_complaint_marker
    ):
        return _make(
            ConversationMoment.RECOVERY_AFTER_FAILURE, lifecycle, sentiment, ppw, urgency,
            reason="prior_complaint_plus_continuation",
            advisory=(
                "Customer is returning AFTER a previous complaint "
                "was acknowledged. They need ownership and a real "
                "next step, not another apology. Reassure, give "
                "concrete status if known, never repeat the prior "
                "apology verbatim."
            ),
            framing="ownership_over_apology",
        )

    # 3) Specific complaints: product quality > shipping delay > generic.
    if has_quality_complaint and ppw in (
        PostPurchaseWindow.DELIVERED_WITHIN_48H,
        PostPurchaseWindow.DELIVERED_WITHIN_7D,
        PostPurchaseWindow.DELIVERED_OLDER,
        PostPurchaseWindow.AWAITING_DELIVERY,
    ):
        return _make(
            ConversationMoment.COMPLAINT_PRODUCT_QUALITY, lifecycle, sentiment, ppw, urgency,
            reason="quality_marker_in_post_delivery",
            advisory=(
                "Customer is dissatisfied with the PRODUCT itself. "
                "Acknowledge the quality concern with empathy, take "
                "ownership without defensive language, ask one "
                "specific question if needed, and offer a real next "
                "step. Do NOT push catalogue or coupons. Do NOT "
                "use 'سيتم التواصل' as a deflection."
            ),
            framing="empathic_ownership_no_deflection",
        )
    if has_shipping_delay:
        return _make(
            ConversationMoment.COMPLAINT_SHIPPING_DELAY, lifecycle, sentiment, ppw, urgency,
            reason="shipping_delay_marker",
            advisory=(
                "Customer is concerned about a delayed shipment. "
                "Start by acknowledging the wait without excuses, "
                "take ownership, share what you know about the "
                "shipment status if available, and explain the "
                "next step. Escalation flag may still be set for "
                "the dashboard, but you must not stop replying."
            ),
            framing="acknowledge_then_help",
        )
    if has_generic_complaint or has_anger:
        return _make(
            ConversationMoment.COMPLAINT_GENERIC, lifecycle, sentiment, ppw, urgency,
            reason="generic_complaint_marker",
            advisory=(
                "Customer is expressing dissatisfaction without a "
                "specific axis. Acknowledge the feeling, ask one "
                "calm clarifying question, and avoid jumping to "
                "promotions or coupon offers."
            ),
            framing="calm_clarifying_no_upsell",
        )

    # 4) Praise post-delivery beats generic gratitude.
    if (has_heavy_praise or social_norm == "strong_praise") and ppw in (
        PostPurchaseWindow.DELIVERED_WITHIN_48H,
        PostPurchaseWindow.DELIVERED_WITHIN_7D,
        PostPurchaseWindow.DELIVERED_OLDER,
    ):
        return _make(
            ConversationMoment.PRAISE_POST_DELIVERY, lifecycle, sentiment, ppw, urgency,
            reason="strong_praise_in_post_delivery",
            advisory=(
                "Customer is praising the product / packaging / "
                "delivery AFTER receiving the order. This is a "
                "loyalty moment. Reciprocate the warmth naturally; "
                "do NOT run a customer-lookup or push a new "
                "transaction. If anything, invite them to share "
                "again or come back when they need more."
            ),
            framing="celebrate_no_lookup_no_upsell",
        )
    if has_gratitude or has_heavy_praise or social_norm in (
        "thanks", "compliment", "strong_praise",
    ):
        return _make(
            ConversationMoment.GRATITUDE_GENERIC, lifecycle, sentiment, ppw, urgency,
            reason="gratitude_no_post_delivery",
            advisory=(
                "Customer expressed thanks. Reciprocate briefly "
                "and naturally; do not invent a transaction the "
                "thanks does not warrant."
            ),
            framing="reciprocate_briefly",
        )

    # 4c) Seasonal greeting (W3.1, May 2026). Customer's turn carries
    #     an explicit seasonal congratulation. Fires only when no
    #     gratitude / praise / complaint / escalation / recovery
    #     moment has matched above (additive-only insertion). Must
    #     NOT fire when the customer is mid-funnel: a transactional
    #     active turn that happens to include "كل عام مبارك" still
    #     belongs to the funnel (the dedup-suppression gate also
    #     guards this independently, but the classifier shouldn't
    #     hand it the wrong moment).
    has_seasonal_marker = text_indicates_seasonal_greeting(inbound_text)
    has_religious_marker = text_indicates_religious_ritual(inbound_text)
    active_funnel_statuses_w3 = {
        "awaiting_receipt", "under_review", "processing", "payment_pending",
    }
    is_mid_funnel_w3 = (
        has_selected_product
        or order_status.lower() in active_funnel_statuses_w3
    )
    if has_seasonal_marker and not is_mid_funnel_w3:
        return _make(
            ConversationMoment.SEASONAL_GREETING, lifecycle, sentiment, ppw, urgency,
            reason="seasonal_greeting_marker",
            advisory=(
                "Customer is sending a seasonal greeting (Eid / "
                "Ramadan / new-year style). This is a relational "
                "turn — match the register naturally, do not pivot "
                "to a sales prompt or a customer-lookup. The Brain "
                "owns the wording; downstream dedup will be told "
                "to keep your reply intact."
            ),
            framing="match_seasonal_register",
        )

    # 4d) Religious ritual exchange (W3.1, May 2026). Pure
    #     supplication / blessing without a gratitude marker (which
    #     would have fired GRATITUDE_GENERIC at step 4b above) and
    #     without a commerce intent in flight. Same mid-funnel guard
    #     as seasonal: a customer mid-payment-flow saying "بارك الله
    #     فيك" stays in transactional_active.
    if has_religious_marker and not is_mid_funnel_w3:
        return _make(
            ConversationMoment.RELIGIOUS_RITUAL_EXCHANGE, lifecycle, sentiment, ppw, urgency,
            reason="religious_ritual_marker",
            advisory=(
                "Customer's turn is a religious supplication / "
                "blessing / ritual phrase with no commerce ask. "
                "Reciprocate naturally; do not start a customer "
                "lookup, do not push the catalogue, do not treat "
                "lexical repetition with a previous reply as a "
                "loop."
            ),
            framing="reciprocate_ritual_warmly",
        )

    # 5) Pre-purchase concern: hesitation markers in first-time / lapsed.
    if has_hesitation and lifecycle in (
        LifecycleStage.FIRST_TIME, LifecycleStage.LAPSED, LifecycleStage.UNKNOWN,
    ) and not has_selected_product:
        return _make(
            ConversationMoment.CONCERN_PRE_PURCHASE, lifecycle, sentiment, ppw, urgency,
            reason="hesitation_pre_purchase",
            advisory=(
                "Customer is hesitating before a first purchase. "
                "Build trust: address the specific concern, offer "
                "factual reassurance from the merchant knowledge "
                "base, do NOT push a coupon as the first move."
            ),
            framing="trust_build_no_coupon_push",
        )

    # 6) Loyal repeat customer: only when no other moment fires.
    if lifecycle in (LifecycleStage.LOYAL, LifecycleStage.REPEAT):
        return _make(
            ConversationMoment.LOYAL_REPEAT_CUSTOMER, lifecycle, sentiment, ppw, urgency,
            reason="loyal_lifecycle_no_other_moment",
            advisory=(
                "Customer is a loyal / repeat buyer asking an "
                "ordinary question. Mirror that familiarity in "
                "your tone — they know the store, you know them. "
                "Do NOT default to first-time framing."
            ),
            framing="familiar_warm_concise",
        )

    # 7) Active funnel.
    active_statuses = {
        "awaiting_receipt", "under_review", "processing", "payment_pending",
    }
    if has_selected_product or order_status.lower() in active_statuses:
        return _make(
            ConversationMoment.TRANSACTIONAL_ACTIVE, lifecycle, sentiment, ppw, urgency,
            reason="mid_funnel",
            advisory=(
                "Customer is mid-funnel. Stay focused on completing "
                "the order; do not derail with broader topics."
            ),
            framing="focused_funnel",
        )

    # 8) Informational neutral.
    info_intents = {
        "ask_location", "ask_shipping", "ask_store_info", "ask_payment_info",
        "ask_owner_contact", "ask_product", "ask_price",
    }
    if intent_norm in info_intents and sentiment in (
        Sentiment.NEUTRAL, Sentiment.UNKNOWN, Sentiment.POSITIVE,
    ):
        return _make(
            ConversationMoment.INFORMATIONAL_NEUTRAL, lifecycle, sentiment, ppw, urgency,
            reason="info_intent_neutral",
            advisory=(
                "Customer asked an informational question with no "
                "complaint or commerce in flight. Brief warmth "
                "BEFORE the fact is welcome; safety nets must not "
                "strip a one-line greeting if the brain provided it."
            ),
            framing="warmth_before_fact",
        )

    # 9) Pure social check-in (greeting / blessing).
    if intent_norm in ("greeting", "social") and not has_selected_product:
        return _make(
            ConversationMoment.SOCIAL_CHECK_IN, lifecycle, sentiment, ppw, urgency,
            reason="greeting_or_blessing",
            advisory=(
                "Customer is greeting / blessing without a commerce "
                "ask. Match the register naturally."
            ),
            framing="match_register",
        )

    # 10) Default — no relational signal.
    return RelationalState()


def _make(
    moment: ConversationMoment,
    lifecycle: LifecycleStage,
    sentiment: Sentiment,
    ppw: PostPurchaseWindow,
    urgency: Urgency,
    *,
    reason: str,
    advisory: str,
    framing: str,
) -> RelationalState:
    return RelationalState(
        moment=moment,
        lifecycle_stage=lifecycle,
        sentiment=sentiment,
        post_purchase_window=ppw,
        urgency=urgency,
        advisory_for_brain=advisory,
        framing_directive=framing,
        reason=reason,
    )


def log_relational_state(
    *,
    tenant_id: Any,
    phone: Optional[str],
    state: RelationalState,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit a single ``[CX]`` log line per turn. Operators grep
    ``cx_moment=`` to answer "why did the bot reply that way?"."""
    try:
        masked_phone = ""
        if phone:
            try:
                masked_phone = "*" + str(phone)[-4:]
            except Exception:
                masked_phone = ""
        kv = state.to_log_dict()
        logger.info(
            "[CX] tenant=%s phone=%s %s extra=%s",
            tenant_id, masked_phone,
            " ".join(f"{k}={v}" for k, v in kv.items()),
            extra or {},
        )
    except Exception:
        pass


# Re-export the architectural rule so consumers can pin it in their
# own docstrings without importing :mod:`contracts` directly.
RELATIONAL_LAYER_RULE = ARCHITECTURAL_RULE_TEXT


__all__ = [
    "RelationalState",
    "compute_relational_state",
    "log_relational_state",
    "RELATIONAL_LAYER_RULE",
]
