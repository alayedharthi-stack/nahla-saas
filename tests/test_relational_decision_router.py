"""
tests/test_relational_decision_router.py
────────────────────────────────────────
Commit 2 of the Tenant 33 #49 relational architecture rollout —
decision-router unit + integration tests.

Headline guarantees pinned here:

  * PRAISE_POST_DELIVERY blocks the dry ``track_order`` lookup.
  * COMPLAINT_SHIPPING_DELAY / COMPLAINT_PRODUCT_QUALITY route to
    LLM with a complaint-recovery goal token, NOT to a dry handoff
    template.
  * CONCERN_PRE_PURCHASE suppresses the instant coupon push.
  * Kill switch off OR moment NONE OR no relational state -> router
    is inert (decision returned UNCHANGED, identity-equal).
  * Router NEVER mutates the input decision.
  * Router NEVER injects business-state args (payment / order /
    shipping / tracking / sku / IBAN / …).
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_FAQ_REPLY,
    ACTION_GREET,
    ACTION_HANDOFF,
    ACTION_LLM_REPLY,
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SOCIAL_REPLY,
    ACTION_SUGGEST_COUPON,
    ACTION_TRACK_ORDER,
)
from modules.ai.brain.relational import (  # noqa: E402
    BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS,
    ConversationMoment,
    RelationalState,
    apply_relational_preference,
    is_decision_router_enabled,
)
from modules.ai.brain.relational.decision_router import (  # noqa: E402
    RESPONSE_GOAL_APPRECIATION_ACK,
    RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC,
    RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT,
    RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING,
    RESPONSE_GOAL_TRUST_BUILDING,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_GREETING,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
)


# ── helpers ─────────────────────────────────────────────────────────


class _Ctx:
    """Minimal duck-typed context used by router pure-function tests."""

    def __init__(
        self,
        moment: ConversationMoment = ConversationMoment.NONE,
        tenant_id: int = 33,
        phone: str = "+966500000001",
    ) -> None:
        self.tenant_id = tenant_id
        self.customer_phone = phone
        self.relational_state = (
            RelationalState(moment=moment)
            if moment != ConversationMoment.NONE or False
            else None
        )


def _ctx_with_moment(moment: ConversationMoment) -> _Ctx:
    c = _Ctx()
    c.relational_state = RelationalState(moment=moment)
    return c


@pytest.fixture(autouse=True)
def _enable_router(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: router flag ON for the unit tests below. Tests that
    need it OFF flip it explicitly."""
    monkeypatch.setenv("RELATIONAL_DECISION_ROUTER_ENABLED", "1")


# ── kill switch ─────────────────────────────────────────────────────


def test_router_kill_switch_off_returns_decision_identity_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RELATIONAL_DECISION_ROUTER_ENABLED", "0")
    assert is_decision_router_enabled() is False
    ctx = _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY)
    d = Decision(action=ACTION_TRACK_ORDER, args={"order_id": ""}, reason="orig")
    out = apply_relational_preference(d, ctx)
    assert out is d  # identity-equal, no allocation


def test_router_inert_when_no_relational_state() -> None:
    ctx = _Ctx()
    ctx.relational_state = None
    d = Decision(action=ACTION_HANDOFF, args={}, reason="x")
    out = apply_relational_preference(d, ctx)
    assert out is d


def test_router_inert_when_moment_is_none() -> None:
    ctx = _ctx_with_moment(ConversationMoment.NONE)
    d = Decision(action=ACTION_HANDOFF, args={}, reason="x")
    out = apply_relational_preference(d, ctx)
    assert out is d


def test_router_inert_for_unrelated_action_moment_pair() -> None:
    """A moment with no rule + no default tag -> decision unchanged.
    Example: SOCIAL_CHECK_IN + ACTION_GREET. We don't tag GREETs."""
    ctx = _ctx_with_moment(ConversationMoment.SOCIAL_CHECK_IN)
    d = Decision(action=ACTION_GREET, args={}, reason="greet")
    out = apply_relational_preference(d, ctx)
    assert out is d


# ── HEADLINE 1: praise post-delivery blocks dry customer-lookup ────


def test_praise_post_delivery_blocks_dry_customer_lookup() -> None:
    """HEADLINE TEST per merchant directive — closes the production
    bug where post-delivery praise triggered a track_order lookup
    and the bot answered 'no orders found for your number'."""
    ctx = _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY)
    d = Decision(
        action=ACTION_TRACK_ORDER,
        args={"order_id": ""},
        reason="customer asked for order status",
    )
    out = apply_relational_preference(d, ctx)
    assert out.action == ACTION_LLM_REPLY
    assert out.args["preferred_response_goal"] == RESPONSE_GOAL_APPRECIATION_ACK
    assert out.args["relational_routing_applied"] is True
    assert out.args["relational_moment"] == "praise_post_delivery"
    assert "relational_router=" in out.reason


def test_praise_post_delivery_with_llm_reply_only_tags_goal() -> None:
    """Action already LLM_REPLY -> no re-route, only goal tag added."""
    ctx = _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY)
    d = Decision(action=ACTION_LLM_REPLY, args={}, reason="praise turn")
    out = apply_relational_preference(d, ctx)
    assert out.action == ACTION_LLM_REPLY  # unchanged
    assert out.args["preferred_response_goal"] == RESPONSE_GOAL_APPRECIATION_ACK
    assert out.args["relational_routing_applied"] is False  # tag-only branch


# ── HEADLINE 2: complaint moments route to complaint_recovery ──────


@pytest.mark.parametrize(
    "moment,expected_goal",
    [
        (ConversationMoment.COMPLAINT_SHIPPING_DELAY,  RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING),
        (ConversationMoment.COMPLAINT_PRODUCT_QUALITY, RESPONSE_GOAL_COMPLAINT_RECOVERY_PRODUCT),
        (ConversationMoment.COMPLAINT_GENERIC,         RESPONSE_GOAL_COMPLAINT_RECOVERY_GENERIC),
    ],
)
def test_complaint_handoff_reroutes_to_llm_complaint_recovery(
    moment: ConversationMoment, expected_goal: str,
) -> None:
    """HEADLINE TEST — closes the 'flat handoff ACK on a Hajj
    shipping-delay complaint' production bug. ``ACTION_HANDOFF``
    becomes ``ACTION_LLM_REPLY`` so the brain composes empathically."""
    ctx = _ctx_with_moment(moment)
    d = Decision(action=ACTION_HANDOFF, args={}, reason="auto handoff")
    out = apply_relational_preference(d, ctx)
    assert out.action == ACTION_LLM_REPLY
    assert out.args["preferred_response_goal"] == expected_goal
    assert out.args["relational_routing_applied"] is True


def test_complaint_with_llm_reply_only_tags_goal() -> None:
    ctx = _ctx_with_moment(ConversationMoment.COMPLAINT_SHIPPING_DELAY)
    d = Decision(action=ACTION_LLM_REPLY, args={}, reason="brain default")
    out = apply_relational_preference(d, ctx)
    assert out.action == ACTION_LLM_REPLY
    assert out.args["preferred_response_goal"] == RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING
    assert out.args["relational_routing_applied"] is False


def test_complaint_does_not_swap_unrelated_actions() -> None:
    """A complaint moment must not change ACTION_PROPOSE_DRAFT_ORDER —
    that is a deterministic order-flow action and the relational
    layer has no business overriding it."""
    ctx = _ctx_with_moment(ConversationMoment.COMPLAINT_SHIPPING_DELAY)
    d = Decision(
        action=ACTION_PROPOSE_DRAFT_ORDER,
        args={"product": {"id": 1}},
        reason="customer ready",
    )
    out = apply_relational_preference(d, ctx)
    # Action must stay; only the goal token gets tagged via the
    # default-tag branch.
    assert out.action == ACTION_PROPOSE_DRAFT_ORDER
    assert out.args["preferred_response_goal"] == RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING
    # Critically: the original product arg is preserved.
    assert out.args["product"] == {"id": 1}


# ── HEADLINE 3: concern_pre_purchase suppresses early coupon push ──


def test_concern_pre_purchase_blocks_early_coupon_push() -> None:
    """HEADLINE TEST — prevents the bot from leading with a discount
    when the first-time customer is hesitating. Trust comes before
    coupon."""
    ctx = _ctx_with_moment(ConversationMoment.CONCERN_PRE_PURCHASE)
    d = Decision(
        action=ACTION_SUGGEST_COUPON,
        args={"product": {"id": 7}},
        reason="customer hesitating — nudge with a coupon",
    )
    out = apply_relational_preference(d, ctx)
    assert out.action == ACTION_LLM_REPLY
    assert out.args["preferred_response_goal"] == RESPONSE_GOAL_TRUST_BUILDING
    assert out.args["relational_routing_applied"] is True
    # Existing args preserved.
    assert out.args["product"] == {"id": 7}


# ── architectural invariants ───────────────────────────────────────


def test_router_never_raises_on_garbage_inputs() -> None:
    """The router is a hot-path safety wrapper — must NEVER raise."""
    # Garbage decision
    out = apply_relational_preference(None, _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY))  # type: ignore[arg-type]
    assert out is None
    # Garbage ctx — no attributes
    d = Decision(action=ACTION_HANDOFF, args={}, reason="r")
    out = apply_relational_preference(d, object())
    assert out is d


def test_router_does_not_mutate_input_decision() -> None:
    """Decision is a frozen-shape contract: caller can hold the
    pre-router copy for logs / before-after diffs."""
    ctx = _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY)
    original_args = {"order_id": "ABC"}
    d = Decision(action=ACTION_TRACK_ORDER, args=original_args, reason="orig")
    out = apply_relational_preference(d, ctx)
    # Input is intact.
    assert d.action == ACTION_TRACK_ORDER
    assert d.args == {"order_id": "ABC"}
    assert d.reason == "orig"
    # Output is a different object.
    assert out is not d


@pytest.mark.parametrize(
    "moment,action",
    [
        (ConversationMoment.PRAISE_POST_DELIVERY,      ACTION_TRACK_ORDER),
        (ConversationMoment.COMPLAINT_SHIPPING_DELAY,  ACTION_HANDOFF),
        (ConversationMoment.COMPLAINT_PRODUCT_QUALITY, ACTION_HANDOFF),
        (ConversationMoment.COMPLAINT_GENERIC,         ACTION_HANDOFF),
        (ConversationMoment.CONCERN_PRE_PURCHASE,      ACTION_SUGGEST_COUPON),
        (ConversationMoment.PRAISE_POST_DELIVERY,      ACTION_LLM_REPLY),
        (ConversationMoment.COMPLAINT_SHIPPING_DELAY,  ACTION_LLM_REPLY),
        (ConversationMoment.CONCERN_PRE_PURCHASE,      ACTION_LLM_REPLY),
    ],
)
def test_router_never_injects_business_state_args(
    moment: ConversationMoment, action: str,
) -> None:
    """HEADLINE INVARIANT — for every (moment, action) pair, the
    args added by the router must NOT carry a business-state key.
    The relational layer may shape the conversation, never fabricate
    business state."""
    ctx = _ctx_with_moment(moment)
    d = Decision(action=action, args={}, reason="r")
    out = apply_relational_preference(d, ctx)
    new_keys = set((out.args or {}).keys()) - set((d.args or {}).keys())
    for key in new_keys:
        for forbidden in BUSINESS_FACT_FIELD_FORBIDDEN_TOKENS:
            assert forbidden not in key.lower(), (
                f"router added arg {key!r} containing forbidden "
                f"business-state token {forbidden!r} for moment "
                f"{moment.value!r} action {action!r}"
            )
        # And no value that smells like state mutation.
        assert key not in (
            "payment_receipt_received", "order_status", "order_paid",
            "tracking_number", "shipped", "shipment_id", "iban",
        )


def test_router_preserves_existing_args() -> None:
    """The router may ADD args (preferred_response_goal, etc.) but
    must never DELETE or OVERWRITE an existing arg key."""
    ctx = _ctx_with_moment(ConversationMoment.CONCERN_PRE_PURCHASE)
    d = Decision(
        action=ACTION_SUGGEST_COUPON,
        args={
            "product":          {"id": 1, "title": "عسل"},
            "campaign_id":      "summer-2026",
            "discount_pct":     10,
        },
        reason="hesitating",
    )
    out = apply_relational_preference(d, ctx)
    # All original keys preserved.
    assert out.args["product"] == {"id": 1, "title": "عسل"}
    assert out.args["campaign_id"] == "summer-2026"
    assert out.args["discount_pct"] == 10
    # New router keys present alongside originals.
    assert out.args["preferred_response_goal"] == RESPONSE_GOAL_TRUST_BUILDING


def test_router_never_mutates_ctx_state_or_relational_state() -> None:
    """Pure-function check — running the router must not mutate any
    field of ``ctx`` or ``ctx.relational_state``."""
    ctx = _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY)
    rs_before = ctx.relational_state
    rs_moment_before = rs_before.moment
    d = Decision(action=ACTION_TRACK_ORDER, args={}, reason="r")
    apply_relational_preference(d, ctx)
    assert ctx.relational_state is rs_before
    assert ctx.relational_state.moment == rs_moment_before


# ── log emission ────────────────────────────────────────────────────


def test_router_emits_cx_log_line_on_reroute(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="nahla.relational.router")
    ctx = _ctx_with_moment(ConversationMoment.PRAISE_POST_DELIVERY)
    d = Decision(action=ACTION_TRACK_ORDER, args={}, reason="r")
    apply_relational_preference(d, ctx)
    msgs = [r.getMessage() for r in caplog.records]
    cx_lines = [m for m in msgs if "[CX] router" in m]
    assert len(cx_lines) == 1
    assert "before_action=track_order" in cx_lines[0]
    assert "after_action=llm_reply" in cx_lines[0]
    assert f"moment={ConversationMoment.PRAISE_POST_DELIVERY.value}" in cx_lines[0]
    assert "kind=rule" in cx_lines[0]


def test_router_emits_cx_log_line_on_tag_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="nahla.relational.router")
    ctx = _ctx_with_moment(ConversationMoment.COMPLAINT_SHIPPING_DELAY)
    d = Decision(action=ACTION_LLM_REPLY, args={}, reason="r")
    apply_relational_preference(d, ctx)
    cx_lines = [
        r.getMessage() for r in caplog.records if "[CX] router" in r.getMessage()
    ]
    assert len(cx_lines) == 1
    assert "kind=tag" in cx_lines[0]
    assert "before_action=llm_reply" in cx_lines[0]
    assert "after_action=llm_reply" in cx_lines[0]


# ── pipeline-level integration ─────────────────────────────────────


def _make_facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True, product_count=2, in_stock_count=2,
        has_active_integration=True, orderable=True, has_coupons=False,
        snapshot_fresh=True, store_name="متجر", store_url="https://x.test",
        store_description="x", store_contact_phone="+966500000000",
        shipping_policy="x", support_hours="9-22",
        shipping_methods=["سمسا"], integration_platform="salla",
    )


def _build_brain():
    from modules.ai.brain.compose.responder import DefaultComposer
    from modules.ai.brain.decision.engine import DefaultDecisionEngine
    from modules.ai.brain.decision.policy import PassThroughPolicyGate
    from modules.ai.brain.execution.executor import DefaultActionExecutor
    from modules.ai.brain.memory.updater import DefaultMemoryUpdater
    from modules.ai.brain.pipeline import MerchantBrain

    intent = Intent(name=INTENT_GREETING, confidence=0.95, raw_message="مرحبا")
    state = MerchantConversationState(stage="discovery", greeted=False)

    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=intent)
    state_store = MagicMock()
    state_store.load.return_value = state
    state_store.save.return_value = None
    state_store.transition.return_value = state
    facts_loader = MagicMock()
    facts_loader.load.return_value = _make_facts()
    memory_updater = MagicMock()
    memory_updater.update.return_value = None
    return MerchantBrain(
        classifier=classifier,
        state_store=state_store,
        facts_loader=facts_loader,
        decision_engine=DefaultDecisionEngine(),
        policy_gate=PassThroughPolicyGate(),
        executor=DefaultActionExecutor(),
        composer=DefaultComposer(),
        memory_updater=memory_updater,
    )


def _db():
    db = MagicMock()
    db.add.return_value = None
    db.commit.return_value = None
    return db


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_pipeline_router_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both flags default OFF — pipeline produces the legacy reply
    even if the relational layer would have a verdict."""
    monkeypatch.delenv("RELATIONAL_LAYER_ENABLED", raising=False)
    monkeypatch.delenv("RELATIONAL_DECISION_ROUTER_ENABLED", raising=False)
    brain = _build_brain()
    reply = _run(brain.process(
        db=_db(), tenant_id=33, customer_phone="+966500000001",
        message="مرحبا", history=[], profile={},
    ))
    assert isinstance(reply, dict)
    assert isinstance(reply.get("reply"), str)


def test_pipeline_router_inert_when_layer_off_router_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router flag on, layer flag off => no relational_state on
    ctx => router cannot fire. Pipeline runs unchanged."""
    monkeypatch.delenv("RELATIONAL_LAYER_ENABLED", raising=False)
    monkeypatch.setenv("RELATIONAL_DECISION_ROUTER_ENABLED", "1")
    brain = _build_brain()
    reply = _run(brain.process(
        db=_db(), tenant_id=33, customer_phone="+966500000001",
        message="مرحبا", history=[], profile={},
    ))
    assert isinstance(reply, dict)


# ── compose-level: preferred_response_goal flows into response_goal


def test_response_goal_includes_relational_goal_token_when_set() -> None:
    """The brain prompt builder must read
    ``decision.args['preferred_response_goal']`` as a goal prefix."""
    from modules.ai.brain.pipeline import _compose_base_response_goal
    from modules.ai.brain.types import SuggestionSnapshot

    decision = Decision(
        action=ACTION_LLM_REPLY,
        args={"preferred_response_goal": RESPONSE_GOAL_COMPLAINT_RECOVERY_SHIPPING},
        reason="brain default",
    )
    suggestion = SuggestionSnapshot()
    goal = _compose_base_response_goal(decision, suggestion)
    assert "relational_goal=complaint_recovery_shipping_delay" in goal
    # And the original reason still appears.
    assert "brain default" in goal


def test_response_goal_unchanged_when_no_relational_token() -> None:
    """No relational tag -> goal builder behaves byte-identical to
    the pre-Commit-2 baseline."""
    from modules.ai.brain.pipeline import _compose_base_response_goal
    from modules.ai.brain.types import SuggestionSnapshot

    decision = Decision(action=ACTION_LLM_REPLY, args={}, reason="default reason")
    goal = _compose_base_response_goal(decision, SuggestionSnapshot())
    assert "relational_goal=" not in goal
    assert "default reason" in goal
