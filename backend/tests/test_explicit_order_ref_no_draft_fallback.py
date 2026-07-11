"""Explicit order references must not bind to unrelated active drafts."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.local_order_resolver import resolve_customer_order_context  # noqa: E402
from core.order_status_dedup_reply import build_dedup_local_order_short_reply  # noqa: E402
from modules.ai.brain.commerce.order_tracking_intent_guard import (  # noqa: E402
    try_order_reference_continuity_decision,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_TRACK_ORDER  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import TrackOrderHandler  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

_GENERIC_ITEM = {
    "product_id": "sku-shoe-white",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "unit_price": 249.0,
}
GENERIC_ORDER_REF = "284719365"
GENERIC_ORDER_REF_2 = "901234567"
STALE_DRAFT_REF = "NHL-1-000016"
VALID_ORDER_REF = "ORD-CLASS4-501"


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name="متجر تجريبي عام")
    customer = seed_customer(db, tenant.id, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


def _seed_stale_draft(db, tenant_ctx, *, external_order_number: str = STALE_DRAFT_REF):
    wa_ext = f"nahla-wa-{tenant_ctx.tenant_id}-{tenant_ctx.conversation_id}"
    return seed_order(
        db,
        tenant_ctx.tenant_id,
        source="whatsapp",
        external_id=wa_ext,
        external_order_number=external_order_number,
        status="pending_customer_info",
        customer_info={"phone": tenant_ctx.phone},
        line_items=[_GENERIC_ITEM],
        extra_metadata={
            "lifecycle": "whatsapp_draft",
            "conversation_id": tenant_ctx.conversation_id,
        },
    )


def _resolver_ctx(
    db,
    tenant_ctx,
    *,
    order_number: str | None = None,
    intent: str = "track_order",
):
    return resolve_customer_order_context(
        db,
        tenant_id=tenant_ctx.tenant_id,
        conversation_id=tenant_ctx.conversation_id,
        phone=tenant_ctx.phone,
        intent=intent,
        order_number=order_number,
    )


def _dedup_reply(db, tenant_ctx, inbound: str) -> str:
    return build_dedup_local_order_short_reply(
        db,
        tenant_id=tenant_ctx.tenant_id,
        phone=tenant_ctx.phone,
        conversation_id=tenant_ctx.conversation_id,
        inbound_text=inbound,
        previous_outbound="",
    )


class TestExplicitOrderRefNoDraftFallback:
    def test_t1_explicit_ref_not_found_stale_draft_not_selected(
        self, db, tenant_ctx,
    ) -> None:
        draft = _seed_stale_draft(db, tenant_ctx)
        ctx = _resolver_ctx(db, tenant_ctx, order_number=GENERIC_ORDER_REF)
        assert ctx.active_whatsapp_draft is not None
        assert ctx.active_whatsapp_draft.order_id == draft.id
        assert ctx.selected_order is None
        assert ctx.selected_reason == "explicit_order_number_not_found"

    def test_t2_dedup_repeat_ref_does_not_bind_stale_draft(
        self, db, tenant_ctx,
    ) -> None:
        _seed_stale_draft(db, tenant_ctx)
        first = _dedup_reply(db, tenant_ctx, GENERIC_ORDER_REF)
        second = _dedup_reply(db, tenant_ctx, GENERIC_ORDER_REF)
        assert first == ""
        assert second == ""
        assert STALE_DRAFT_REF not in first
        assert STALE_DRAFT_REF not in second

    def test_t3_explicit_ref_match_wins_over_stale_draft(
        self, db, tenant_ctx,
    ) -> None:
        _seed_stale_draft(db, tenant_ctx)
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id=f"manual-{VALID_ORDER_REF}",
            external_order_number=VALID_ORDER_REF,
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        ctx = _resolver_ctx(db, tenant_ctx, order_number=VALID_ORDER_REF)
        assert ctx.selected_order is not None
        assert ctx.selected_reason == "explicit_order_number"
        assert ctx.selected_order.external_order_number == VALID_ORDER_REF
        assert ctx.selected_order.external_order_number != STALE_DRAFT_REF

    def test_t4_no_explicit_ref_keeps_active_draft_fallback(
        self, db, tenant_ctx,
    ) -> None:
        _seed_stale_draft(db, tenant_ctx)
        ctx = _resolver_ctx(db, tenant_ctx, intent="order_number")
        assert ctx.selected_order is not None
        assert ctx.selected_reason == "active_whatsapp_draft"
        assert ctx.selected_order.external_order_number == STALE_DRAFT_REF

    def test_t5_no_explicit_ref_keeps_recent_order_fallback(
        self, db, tenant_ctx,
    ) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="salla",
            external_id="salla-recent-1",
            external_order_number="SAL-RECENT-1",
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        ctx = _resolver_ctx(db, tenant_ctx)
        assert ctx.selected_order is not None
        assert ctx.selected_reason in {
            "latest_open_order",
            "most_recent_order",
            "active_whatsapp_draft",
        }

    def test_t6_first_and_repeated_refs_consistent_not_found(
        self, db, tenant_ctx,
    ) -> None:
        _seed_stale_draft(db, tenant_ctx)
        first_ctx = _resolver_ctx(db, tenant_ctx, order_number=GENERIC_ORDER_REF)
        repeat_ctx = _resolver_ctx(db, tenant_ctx, order_number=GENERIC_ORDER_REF)
        assert first_ctx.selected_order is None
        assert repeat_ctx.selected_order is None
        assert first_ctx.selected_reason == "explicit_order_number_not_found"
        assert repeat_ctx.selected_reason == "explicit_order_number_not_found"

    def test_t7_active_draft_not_mutated(self, db, tenant_ctx) -> None:
        draft = _seed_stale_draft(db, tenant_ctx)
        before_status = draft.status
        before_ref = draft.external_order_number
        _resolver_ctx(db, tenant_ctx, order_number=GENERIC_ORDER_REF_2)
        db.refresh(draft)
        assert draft.status == before_status
        assert draft.external_order_number == before_ref

    def test_t8_class4_valid_order_still_resolves(self, db, tenant_ctx) -> None:
        seed_order(
            db,
            tenant_ctx.tenant_id,
            source="manual",
            external_id=f"manual-{GENERIC_ORDER_REF}",
            external_order_number=GENERIC_ORDER_REF,
            status="processing",
            customer_info={"phone": tenant_ctx.phone},
            line_items=[_GENERIC_ITEM],
        )
        ctx = _resolver_ctx(db, tenant_ctx, order_number=GENERIC_ORDER_REF)
        assert ctx.selected_order is not None
        assert ctx.selected_reason == "explicit_order_number"
        assert ctx.selected_order.external_order_number == GENERIC_ORDER_REF

    def test_t9_stale_checkout_continuity_still_eligible(self) -> None:
        history = [{"direction": "in", "body": GENERIC_ORDER_REF}]
        state = SimpleNamespace(
            draft_order_id="draft-16",
            order_prep=SimpleNamespace(
                draft_order_id="draft-16",
                draft_order_reference=STALE_DRAFT_REF,
                order_creation_status="created",
                order_status="pending_customer_info",
            ),
        )
        state.to_dict = lambda: {  # type: ignore[attr-defined]
            "draft_order_id": "draft-16",
            "order_prep": {
                "draft_order_reference": STALE_DRAFT_REF,
                "order_creation_status": "created",
            },
        }
        ctx = SimpleNamespace(
            message="الطلب متأخر والشحن ما وصل",
            history=history,
            state=state,
            commerce_bundle={},
            profile={},
            tenant_id=1,
        )
        dec = try_order_reference_continuity_decision(ctx)
        assert dec is not None
        assert dec.action == ACTION_LLM_REPLY
        assert dec.args.get("topic") == "existing_order_support"


class TestTrackOrderHandlerExplicitRefGuard:
    def _track_ctx(self, tenant_ctx, message: str, *, db):
        from modules.ai.brain.intent import rules  # noqa: PLC0415
        from modules.ai.brain.types import (  # noqa: PLC0415
            BrainContext,
            INTENT_TRACK_ORDER,
            Intent,
            MerchantConversationState,
        )

        matched = rules.match(message) or Intent(
            name=INTENT_TRACK_ORDER,
            confidence=0.95,
            raw_message=message,
            slots={"order_id": message, "order_number": message},
        )
        state = MerchantConversationState(greeted=True, stage="discovery")
        brain = BrainContext(
            tenant_id=tenant_ctx.tenant_id,
            customer_phone=tenant_ctx.phone,
            customer_id=tenant_ctx.customer_id,
            conversation_id=tenant_ctx.conversation_id,
            message=message,
            intent=matched,
            state=state,
            facts={},
            history=[],
        )
        brain._db = db  # noqa: SLF001
        return brain

    def test_explicit_missing_with_stale_draft_fails_honestly(
        self, db, tenant_ctx,
    ) -> None:
        _seed_stale_draft(db, tenant_ctx)
        ctx = self._track_ctx(tenant_ctx, GENERIC_ORDER_REF_2, db=db)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_TRACK_ORDER

        async def _run():
            return await TrackOrderHandler().handle(decision, ctx)

        result = asyncio.run(_run())
        assert result.success is False
        assert result.error == "order_not_found"
        assert STALE_DRAFT_REF not in str(result.data or {})
