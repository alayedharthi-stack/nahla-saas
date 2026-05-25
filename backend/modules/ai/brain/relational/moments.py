"""
brain/relational/moments.py
───────────────────────────
Closed enums for the relational layer.

Why this file exists
────────────────────
Tenant 33 (May 2026) — production audit revealed Nahla wins
*logically* but loses *humanly*: complaints get blunt escalation
ACKs, post-delivery praise gets a "we don't find any orders for
your number" lookup, location asks get a cold "موقعنا 📍\\n{url}"
artifact rewrite. The brain has intent + stance, but no answer to:

    "In WHAT relational moment is the customer right now?"

This module owns the closed taxonomy that answers that question,
plus the orthogonal lifecycle / sentiment / post-purchase / urgency
dimensions that compose with it.

Architectural rules pinned by the merchant directive
──────────────────────────────────────────────────────

  1. Moment ≠ Intent. They are independent dimensions.
     ``INTENT_TRACK_ORDER`` + ``moment=COMPLAINT_SHIPPING_DELAY`` is
     a normal, expected combination — not a contradiction.

  2. Moment NEVER selects the reply text. Moment may ONLY:
       * adjust framing in the brain prompt,
       * suppress transactional artifacts in safety nets,
       * prevent obviously stupid action choices,
       * adjust priority among existing actions.
     There is no rule of the form ``moment == X → reply Y``.

  3. The moment vocabulary stays small and CLOSED. Adding a value
     requires the merchant directive level — every consumer must
     handle every value (or fall back to ``NONE`` behaviour).

These constants are also written to structured logs and (eventually)
to the brain's understanding overlay. Renaming any of them is a
breaking change.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


# ── Conversation moments ────────────────────────────────────────────


class ConversationMoment(str, Enum):
    """The relational moment the customer is in for THIS turn.

    Members are stable string values — they appear in logs, in
    BrainReplyState (eventually), and in tests. ``str`` mixin so a
    moment is also its own JSON-friendly token.
    """

    # No relational signal in the inbound. Caller behaves exactly
    # as before; the relational layer is inert.
    NONE = "none"

    # Customer is greeting / chit-chatting with no commerce on the
    # turn (salaam, blessing, social ping). Distinct from
    # ``GRATITUDE_GENERIC``: there is no thanks here, just contact.
    SOCIAL_CHECK_IN = "social_check_in"

    # Pure thanks ("شكرا", "تسلم") with no post-delivery context.
    # Lighter than ``PRAISE_POST_DELIVERY`` — we acknowledge but do
    # not celebrate, because nothing about the customer's order is
    # known to be settled.
    GRATITUDE_GENERIC = "gratitude_generic"

    # Customer is praising the product / packaging / delivery AFTER
    # we know an order has shipped or been delivered to them. This
    # is the loyalty-moment a transactional bot would step on by
    # running a customer lookup. Brain owns the wording; safety
    # nets are gated to NOT inject artifacts on this turn.
    PRAISE_POST_DELIVERY = "praise_post_delivery"

    # Customer is dissatisfied with the PRODUCT itself (taste,
    # texture, quality, storage damage). Routes to LLM with a
    # complaint-recovery framing — NOT to blunt handoff.
    COMPLAINT_PRODUCT_QUALITY = "complaint_product_quality"

    # Customer is concerned about SHIPPING (late, lost, tracking
    # silent). Routes to LLM with a delay-acknowledgment framing —
    # ``needs_human=True`` may still be flagged for the dashboard,
    # but the AI keeps replying (per #46 policy).
    COMPLAINT_SHIPPING_DELAY = "complaint_shipping_delay"

    # Negative sentiment + complaint markers, but the specific axis
    # (product vs shipping vs price) cannot be inferred. Generic
    # complaint-recovery framing.
    COMPLAINT_GENERIC = "complaint_generic"

    # Customer is back AFTER a previous complaint was acknowledged
    # in the conversation history. Distinct because the social
    # contract is different: an apology was already given; what
    # they need now is reassurance + ownership + a real next step.
    # Per merchant directive: "هذه لحظة حساسة جدًا تجاريًا".
    RECOVERY_AFTER_FAILURE = "recovery_after_failure"

    # First-time / lapsed customer asking pre-purchase questions
    # with hesitation markers ("محتار", "مش متأكد", "خايف من
    # الجودة"). Suppresses aggressive coupon push; prefers
    # trust-building framing.
    CONCERN_PRE_PURCHASE = "concern_pre_purchase"

    # Customer is repeat / loyal and is on the line for ANY reason
    # (info ask, follow-up, casual question). The relational lens
    # is "we know this customer; mirror that warmth" — even on
    # ordinary questions. NOT to be confused with
    # ``LifecycleStage.LOYAL`` which is just demographic. This
    # moment fires only when other moments don't, AND the
    # lifecycle is loyal/repeat.
    LOYAL_REPEAT_CUSTOMER = "loyal_repeat_customer"

    # Customer is mid-funnel: product picked, price acknowledged,
    # awaiting receipt / address / confirmation. Pure transactional
    # lens — we don't add empathy fluff that delays the funnel.
    TRANSACTIONAL_ACTIVE = "transactional_active"

    # Customer is asking an info question (location, hours,
    # shipping policy, recipe, …) with neutral sentiment and no
    # commerce in flight. The "soft warmth" lane: brain is allowed
    # to add a one-line greeting / acknowledgment before delivering
    # the fact, and safety nets must not strip that warmth.
    INFORMATIONAL_NEUTRAL = "informational_neutral"

    # Customer explicitly asked to talk to a human. Independent of
    # any complaint moment — sometimes it's a routine handoff
    # ("أبي أحجز مع الفرع"). Routes to existing handoff path with
    # a softer framing.
    ESCALATION_REQUEST = "escalation_request"


ALL_MOMENTS: Tuple[ConversationMoment, ...] = tuple(ConversationMoment)


# ── Lifecycle stage ─────────────────────────────────────────────────


class LifecycleStage(str, Enum):
    """Customer's commercial lifecycle. Read from
    ``CustomerProfile`` (``is_returning``, ``total_orders``,
    ``rfm_segment``, ``customer_status``). Independent of moment;
    a loyal customer can be in any moment.
    """

    UNKNOWN = "unknown"
    FIRST_TIME = "first_time"        # zero prior orders
    RETURNING = "returning"          # 1 prior order, recent
    REPEAT = "repeat"                # 2+ prior orders, healthy cadence
    LOYAL = "loyal"                  # 5+ orders OR rfm vip / champion
    LAPSED = "lapsed"                # had orders but high churn risk


# ── Sentiment ───────────────────────────────────────────────────────


class Sentiment(str, Enum):
    """Coarse sentiment of THIS turn. Built from a tight rule-based
    scan over the inbound + the rolling summary's ``sentiment``
    field if present. Not a full NLP sentiment classifier."""

    UNKNOWN = "unknown"
    POSITIVE = "positive"            # gratitude, praise, satisfaction
    NEUTRAL = "neutral"              # info ask, factual statement
    CONCERNED = "concerned"          # mild worry, hesitation, doubt
    FRUSTRATED = "frustrated"        # explicit dissatisfaction
    ANGRY = "angry"                  # escalated dissatisfaction


# ── Post-purchase window ────────────────────────────────────────────


class PostPurchaseWindow(str, Enum):
    """Did the customer recently receive a shipment? Bucketed so
    the moment classifier can lock onto the post-delivery praise /
    complaint window without doing free-form date math."""

    NONE = "none"
    AWAITING_DELIVERY = "awaiting_delivery"          # shipped, not yet delivered
    DELIVERED_WITHIN_48H = "delivered_within_48h"    # praise / quality complaints concentrate here
    DELIVERED_WITHIN_7D = "delivered_within_7d"      # late praise still meaningful
    DELIVERED_OLDER = "delivered_older"              # > 7d


# ── Urgency ─────────────────────────────────────────────────────────


class Urgency(str, Enum):
    """How time-sensitive the moment is, from the customer's
    perspective. Used by future suppression layers to deprioritise
    upsell / coupon / promo language when urgency is high."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# ── Helper: stringification stable across log lines ─────────────────


@dataclass(frozen=True)
class _MomentLogTokens:
    """Stable short tokens for log-line greppability. Operators
    grep ``moment=...`` not ``ConversationMoment.X``."""

    moment: str
    lifecycle: str
    sentiment: str
    post_purchase: str
    urgency: str


def to_log_tokens(
    *,
    moment: ConversationMoment,
    lifecycle: LifecycleStage,
    sentiment: Sentiment,
    post_purchase: PostPurchaseWindow,
    urgency: Urgency,
) -> _MomentLogTokens:
    return _MomentLogTokens(
        moment=str(moment.value),
        lifecycle=str(lifecycle.value),
        sentiment=str(sentiment.value),
        post_purchase=str(post_purchase.value),
        urgency=str(urgency.value),
    )


__all__ = [
    "ConversationMoment",
    "ALL_MOMENTS",
    "LifecycleStage",
    "Sentiment",
    "PostPurchaseWindow",
    "Urgency",
    "to_log_tokens",
]
