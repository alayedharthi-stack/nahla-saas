"""
tests/test_relational_state_classifier.py
─────────────────────────────────────────
Per-moment classifier coverage for the relational layer.

For every ``ConversationMoment`` value we assert:
  * the rule fires on a representative inbound,
  * the rule does NOT fire on a clearly-different inbound,
  * priority is respected when multiple signals are present.

Tests are pure (no DB, no fixtures other than parametrize).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modules.ai.brain.relational import (
    ConversationMoment,
    LifecycleStage,
    PostPurchaseWindow,
    Sentiment,
    Urgency,
    compute_relational_state,
)


# ── helpers ─────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def _shipped_recently(hours_ago: int = 6) -> datetime:
    return _now() - timedelta(hours=hours_ago)


# ── 1. NONE / inert ─────────────────────────────────────────────────


def test_empty_inbound_returns_inert_verdict() -> None:
    rs = compute_relational_state(inbound_text="")
    assert rs.moment == ConversationMoment.NONE
    assert rs.is_inert()


def test_neutral_factual_inbound_returns_none_when_no_intent_match() -> None:
    rs = compute_relational_state(
        inbound_text="ممكن صورة المنتج",
        intent_name="general",
        now=_now(),
    )
    # intent isn't in the info-intents set and there's no other
    # signal — verdict is NONE (the layer has no opinion).
    assert rs.moment == ConversationMoment.NONE


# ── 2. ESCALATION_REQUEST has priority over everything ─────────────


def test_explicit_handoff_request_wins_even_with_complaint_text() -> None:
    rs = compute_relational_state(
        inbound_text="ابغى اتحدث مع الادارة الشحنه ما وصلت",
        intent_name="talk_to_human",
        handoff_signals={"is_explicit_handoff_request": True, "is_complaint": True},
        now=_now(),
    )
    assert rs.moment == ConversationMoment.ESCALATION_REQUEST
    assert "handoff" in rs.advisory_for_brain.lower() or "human" in rs.advisory_for_brain.lower()


# ── 3. RECOVERY_AFTER_FAILURE — prior complaint + continuation ─────


def test_recovery_after_failure_fires_when_summary_records_prior_complaint() -> None:
    rs = compute_relational_state(
        inbound_text="اخر تحديث عندي مفيش رد للحين",
        conversation_summary={"tags": ["complaint"]},
        now=_now(),
    )
    assert rs.moment == ConversationMoment.RECOVERY_AFTER_FAILURE
    assert rs.urgency in (Urgency.NORMAL, Urgency.HIGH)


def test_recovery_does_not_fire_without_prior_complaint_signal() -> None:
    rs = compute_relational_state(
        inbound_text="اخر تحديث عندي مفيش رد للحين",
        conversation_summary={"tags": ["browsing"]},  # no complaint
        now=_now(),
    )
    assert rs.moment != ConversationMoment.RECOVERY_AFTER_FAILURE


# ── 4. COMPLAINT_PRODUCT_QUALITY — needs delivery context ──────────


def test_product_quality_complaint_fires_post_delivery() -> None:
    rs = compute_relational_state(
        inbound_text="العسل تالف وطعمه سيء",
        last_shipment_event_at=_shipped_recently(hours_ago=24),
        order_state={"order_status": "delivered"},
        now=_now(),
    )
    assert rs.moment == ConversationMoment.COMPLAINT_PRODUCT_QUALITY
    assert rs.sentiment in (Sentiment.FRUSTRATED, Sentiment.ANGRY)
    assert rs.post_purchase_window == PostPurchaseWindow.DELIVERED_WITHIN_48H


# ── 5. COMPLAINT_SHIPPING_DELAY ─────────────────────────────────────


def test_shipping_delay_complaint_fires_with_delay_marker() -> None:
    rs = compute_relational_state(
        inbound_text="تاخرت الشحنه بسبب الحج للحين ما وصلت",
        order_state={"order_status": "shipped"},
        last_shipment_event_at=_shipped_recently(hours_ago=72),
        now=_now(),
    )
    assert rs.moment == ConversationMoment.COMPLAINT_SHIPPING_DELAY
    assert rs.sentiment in (Sentiment.FRUSTRATED, Sentiment.CONCERNED)
    # Awaiting-delivery + delay marker -> high urgency.
    assert rs.urgency == Urgency.HIGH


# ── 6. COMPLAINT_GENERIC ────────────────────────────────────────────


def test_generic_complaint_fires_on_anger_without_specific_axis() -> None:
    rs = compute_relational_state(
        inbound_text="هذا غش وانا غير راضي ابدا",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.COMPLAINT_GENERIC
    assert rs.sentiment == Sentiment.ANGRY


# ── 7. PRAISE_POST_DELIVERY beats GRATITUDE_GENERIC ────────────────


def test_praise_post_delivery_fires_after_recent_shipment() -> None:
    rs = compute_relational_state(
        inbound_text="ما شاء الله العسل اكثر من رائع",
        last_shipment_event_at=_shipped_recently(hours_ago=12),
        order_state={"order_status": "delivered"},
        social_category="strong_praise",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.PRAISE_POST_DELIVERY
    assert rs.sentiment == Sentiment.POSITIVE
    # Critical merchant directive: the advisory must explicitly
    # PROHIBIT a customer lookup, not encourage it.
    advisory = rs.advisory_for_brain.lower()
    assert "do not run a customer-lookup" in advisory or "do not run a customer lookup" in advisory
    # And no positive instruction telling the brain to run one.
    assert "run a customer lookup" not in advisory.replace("do not run a customer-lookup", "")


def test_gratitude_generic_fires_without_post_delivery_window() -> None:
    rs = compute_relational_state(
        inbound_text="شكرا لك",
        social_category="thanks",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.GRATITUDE_GENERIC


def test_strong_praise_without_delivery_falls_back_to_gratitude() -> None:
    """No shipment event -> we don't pretend the praise is post-
    delivery. The classifier degrades gracefully to gratitude."""
    rs = compute_relational_state(
        inbound_text="ما شاء الله ممتاز جدا",
        social_category="strong_praise",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.GRATITUDE_GENERIC


# ── 8. CONCERN_PRE_PURCHASE ────────────────────────────────────────


def test_concern_pre_purchase_fires_for_first_time_with_hesitation() -> None:
    rs = compute_relational_state(
        inbound_text="محتار بصراحه اول مره اطلب عسل",
        customer_profile={"total_orders": 0, "customer_status": "lead"},
        order_state={},
        now=_now(),
    )
    assert rs.moment == ConversationMoment.CONCERN_PRE_PURCHASE
    assert rs.lifecycle_stage == LifecycleStage.FIRST_TIME


def test_concern_pre_purchase_does_not_fire_when_product_already_selected() -> None:
    rs = compute_relational_state(
        inbound_text="محتار بصراحه",
        customer_profile={"total_orders": 0},
        order_state={"selected_product": {"id": 1}},
        now=_now(),
    )
    assert rs.moment != ConversationMoment.CONCERN_PRE_PURCHASE


# ── 9. LOYAL_REPEAT_CUSTOMER (only fires when no other moment) ─────


def test_loyal_repeat_customer_fires_for_loyal_with_neutral_inbound() -> None:
    rs = compute_relational_state(
        inbound_text="عساكم بخير",
        intent_name="general",
        customer_profile={"total_orders": 6, "rfm_segment": "loyal"},
        now=_now(),
    )
    assert rs.moment == ConversationMoment.LOYAL_REPEAT_CUSTOMER
    assert rs.lifecycle_stage == LifecycleStage.LOYAL


def test_complaint_beats_loyal_repeat_customer() -> None:
    rs = compute_relational_state(
        inbound_text="هذا غش وانا غير راضي",
        customer_profile={"total_orders": 10, "rfm_segment": "loyal"},
        now=_now(),
    )
    assert rs.moment == ConversationMoment.COMPLAINT_GENERIC


# ── 10. TRANSACTIONAL_ACTIVE ───────────────────────────────────────


def test_transactional_active_fires_when_product_is_selected() -> None:
    rs = compute_relational_state(
        inbound_text="ابي اكمل الطلب",
        order_state={
            "selected_product": {"id": 1, "title": "عسل"},
            "order_status": "awaiting_receipt",
        },
        now=_now(),
    )
    assert rs.moment == ConversationMoment.TRANSACTIONAL_ACTIVE


# ── 11. INFORMATIONAL_NEUTRAL ──────────────────────────────────────


def test_informational_neutral_fires_for_location_question() -> None:
    rs = compute_relational_state(
        inbound_text="وين موقع المناحل",
        intent_name="ask_location",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.INFORMATIONAL_NEUTRAL
    # The framing directive must allow brain warmth before the fact.
    assert "warmth" in rs.framing_directive.lower() or "fact" in rs.framing_directive.lower()


# ── 12. SOCIAL_CHECK_IN ────────────────────────────────────────────


def test_social_check_in_fires_for_greeting_only() -> None:
    rs = compute_relational_state(
        inbound_text="السلام عليكم",
        intent_name="greeting",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.SOCIAL_CHECK_IN


# ── 13. Lifecycle mapping ───────────────────────────────────────────


@pytest.mark.parametrize(
    "profile,expected",
    [
        ({}, LifecycleStage.UNKNOWN),
        ({"total_orders": 0, "customer_status": "lead"}, LifecycleStage.FIRST_TIME),
        ({"total_orders": 1, "is_returning": True}, LifecycleStage.RETURNING),
        ({"total_orders": 3}, LifecycleStage.REPEAT),
        ({"total_orders": 6}, LifecycleStage.LOYAL),
        ({"total_orders": 1, "churn_risk_score": 0.9}, LifecycleStage.LAPSED),
        ({"rfm_segment": "vip", "total_orders": 4}, LifecycleStage.LOYAL),
    ],
)
def test_lifecycle_detection(profile: dict, expected: LifecycleStage) -> None:
    rs = compute_relational_state(
        inbound_text="مرحبا",
        intent_name="greeting",
        customer_profile=profile,
        now=_now(),
    )
    assert rs.lifecycle_stage == expected


# ── 14. Post-purchase window mapping ────────────────────────────────


@pytest.mark.parametrize(
    "hours_ago,status,expected",
    [
        (None, "shipped", PostPurchaseWindow.AWAITING_DELIVERY),
        (12, "delivered", PostPurchaseWindow.DELIVERED_WITHIN_48H),
        (96, "delivered", PostPurchaseWindow.DELIVERED_WITHIN_7D),
        (24 * 30, "delivered", PostPurchaseWindow.DELIVERED_OLDER),
        (None, "", PostPurchaseWindow.NONE),
    ],
)
def test_post_purchase_window_detection(
    hours_ago, status, expected: PostPurchaseWindow,
) -> None:
    rs = compute_relational_state(
        inbound_text="شكرا",
        order_state={"order_status": status},
        last_shipment_event_at=(_shipped_recently(hours_ago=hours_ago) if hours_ago else None),
        now=_now(),
    )
    assert rs.post_purchase_window == expected


# ── 15. Headline regression — praise post-delivery never advises lookup


def test_post_delivery_praise_advisory_forbids_customer_lookup() -> None:
    """HEADLINE TEST: closes the production bug where the bot ran a
    customer-lookup on a post-delivery praise turn and replied
    'no orders found for your number'."""
    rs = compute_relational_state(
        inbound_text="وصل العسل اكثر من رائع تسلم الايادي",
        last_shipment_event_at=_shipped_recently(hours_ago=18),
        order_state={"order_status": "delivered"},
        social_category="strong_praise",
        now=_now(),
    )
    assert rs.moment == ConversationMoment.PRAISE_POST_DELIVERY
    advisory = (rs.advisory_for_brain or "").lower()
    # Explicit prohibition of customer-lookup is the headline rule.
    assert "do not run a customer-lookup" in advisory


# ── 16. Headline regression — shipping delay prefers empathy ───────


def test_shipping_delay_advisory_prefers_empathy_before_escalation() -> None:
    """HEADLINE TEST: closes the 'flat escalation ACK on a Hajj
    shipping-delay complaint' production bug."""
    rs = compute_relational_state(
        inbound_text="الشحنه تاخرت بسبب الحج للحين ما وصلت",
        order_state={"order_status": "shipped"},
        last_shipment_event_at=_shipped_recently(hours_ago=72),
        now=_now(),
    )
    assert rs.moment == ConversationMoment.COMPLAINT_SHIPPING_DELAY
    advisory = (rs.advisory_for_brain or "").lower()
    # The advisory must direct the brain toward empathy + ownership,
    # NOT toward a flat escalation ACK.
    assert "acknowledg" in advisory or "ownership" in advisory or "wait" in advisory
    assert "ستيم التواصل" not in advisory  # no canned escalation copy
