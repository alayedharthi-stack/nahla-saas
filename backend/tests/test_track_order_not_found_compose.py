"""NL-V001 — track_order_not_found constitution compose regression tests."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.compose.track_order_not_found_compose import (  # noqa: E402
    claims_invented_order_facts,
    extract_track_order_not_found_facts,
)
from modules.ai.brain.decision.actions import ACTION_TRACK_ORDER  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.execution.orders import TrackOrderHandler  # noqa: E402
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_order,
    seed_tenant,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
_GENERIC_ITEM = {
    "product_id": "sku-shirt-blue",
    "product_name": "قميص قطني أزرق",
    "quantity": 1,
    "unit_price": 149.0,
}


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=DEFAULT_PHONE_E164,
    )


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(tenant_ctx, message: str, *, db=None, slots: dict | None = None) -> BrainContext:
    matched = rules.match(message)
    assert matched is not None
    if slots:
        matched = Intent(
            name=matched.name,
            confidence=float(matched.confidence or 0.95),
            slots=dict(slots),
            raw_message=message,
            extraction_method=matched.extraction_method or "rules",
        )
    brain = BrainContext(
        tenant_id=tenant_ctx.tenant_id,
        customer_phone=tenant_ctx.phone,
        customer_id=tenant_ctx.customer_id,
        conversation_id=tenant_ctx.conversation_id,
        message=message,
        intent=matched,
        state=MerchantConversationState(greeted=True, stage="discovery"),
        facts=_facts(),
        history=[],
    )
    if db is not None:
        brain._db = db  # noqa: SLF001
    return brain


async def _compose_not_found(
    db,
    tenant_ctx,
    *,
    llm_reply: str | None,
    force_compose_fail: bool = False,
) -> tuple:
    seed_order(
        db,
        tenant_ctx.tenant_id,
        source="manual",
        external_id="manual-ord-other",
        external_order_number="11111",
        status="processing",
        customer_info={"phone": tenant_ctx.phone},
        line_items=[_GENERIC_ITEM],
    )
    ctx = _ctx(tenant_ctx, "طلبي رقم 999999", db=db, slots={"order_id": "999999"})
    decision = DefaultDecisionEngine().decide(ctx)
    result = await TrackOrderHandler().handle(decision, ctx)
    composer = DefaultComposer()
    with patch(
        "modules.ai.brain.intent.link_disambiguation.should_use_generative_tracking_follow_up",
        return_value=False,
    ):
        if force_compose_fail:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(side_effect=RuntimeError("compose_failed")),
            ):
                reply = await composer.compose(decision, result, ctx)
        elif llm_reply is not None:
            with patch.object(
                composer,
                "_llm_compose",
                new=AsyncMock(return_value=llm_reply),
            ):
                reply = await composer.compose(decision, result, ctx)
        else:
            reply = await composer.compose(decision, result, ctx)
    return decision, result, reply, ctx


class TestTrackOrderNotFoundCompose:
    def test_normal_path_uses_llm_compose_metadata(self, db, tenant_ctx) -> None:
        llm_reply = "ما لقيت طلب بهذا الرقم، أرسل رقم الطلب مرة ثانية لو سمحت."
        _decision, result, reply, _ctx = asyncio.run(
            _compose_not_found(db, tenant_ctx, llm_reply=llm_reply)
        )
        assert result.data.get("chosen_path") == "track_order_not_found"
        assert result.data.get("compose_source") == "llm"
        assert result.data.get("response_mode") == "llm"
        assert reply == llm_reply
        assert reply != T.order_status_not_found()

    def test_two_identical_lookups_allow_natural_wording_variance(self, db, tenant_ctx) -> None:
        replies = (
            "ما لقيت طلب بهذا الرقم، تأكد من الرقم لو سمحت.",
            "لم أجد طلباً مطابقاً، ممكن ترسل رقم الطلب مرة ثانية؟",
        )

        async def _run_pair() -> tuple:
            outputs = []
            for text in replies:
                _d, result, reply, _c = await _compose_not_found(db, tenant_ctx, llm_reply=text)
                outputs.append((result, reply))
            return outputs[0], outputs[1]

        (result_a, first), (result_b, second) = asyncio.run(_run_pair())
        for result, reply in ((result_a, first), (result_b, second)):
            assert result.data.get("compose_source") == "llm"
            assert result.data.get("response_mode") == "llm"
            assert result.data.get("track_order_lookup", {}).get("order_verified") is False
            assert reply != T.order_status_not_found()

    def test_forced_compose_failure_uses_deterministic_fallback_metadata(
        self, db, tenant_ctx,
    ) -> None:
        _decision, result, reply, _ctx = asyncio.run(
            _compose_not_found(db, tenant_ctx, llm_reply=None, force_compose_fail=True)
        )
        assert result.data.get("compose_source") == "fallback_deterministic"
        assert result.data.get("fallback_reason") == "compose_failed_or_empty"
        assert result.data.get("fallback_action_type") == "track_order_not_found"
        assert reply == T.order_status_not_found()

    def test_sanitizer_does_not_replace_order_reference_llm_reply(self) -> None:
        llm_reply = "ما لقيت طلب بهذا الرقم، تأكد من رقم الطلب لو سمحت."
        scrubbed, changed, _asset = maybe_scrub_unkept_asset_promise(
            llm_reply,
            has_url=False,
            has_media=False,
            has_phone=False,
        )
        assert changed is False
        assert scrubbed == llm_reply

    def test_sanitizer_still_scrubs_staff_phone_promise_without_digits(self) -> None:
        llm_reply = "ما لقيت طلبك، تفضل رقم أبو هشام للتواصل."
        scrubbed, changed, asset = maybe_scrub_unkept_asset_promise(
            llm_reply,
            has_url=False,
            has_media=False,
            has_phone=False,
        )
        assert changed is True
        assert asset == "phone"
        assert "تفضل رقم أبو هشام" not in scrubbed
        assert "بحالياً" not in scrubbed

    def test_sanitizer_scrubs_phone_promise_but_preserves_order_reference_clause(
        self,
    ) -> None:
        llm_reply = (
            "ما لقيت طلب بهذا الرقم، تفضل رقم أبو هشام للتواصل."
        )
        scrubbed, changed, asset = maybe_scrub_unkept_asset_promise(
            llm_reply,
            has_url=False,
            has_media=False,
            has_phone=False,
        )
        assert changed is True
        assert asset == "phone"
        assert "بهذا الرقم" in scrubbed
        assert "تفضل رقم أبو هشام" not in scrubbed
        assert "بحالياً" not in scrubbed

    def test_order_verified_false_facts_do_not_invent_operational_claims(self) -> None:
        grounded = "ما لقيت طلب بهذا الرقم. أرسل رقم الطلب مرة ثانية للتحقق."
        assert not claims_invented_order_facts(grounded)
        assert claims_invented_order_facts("تم الشحن عبر شركة الشحن")

    def test_extract_facts_include_not_found_shape(self, tenant_ctx) -> None:
        ctx = _ctx(tenant_ctx, "طلبي رقم 999999", slots={"order_id": "999999"})
        facts = extract_track_order_not_found_facts(ctx, SimpleNamespace(data={}))
        assert facts["lookup_result"] == "not_found"
        assert facts["order_verified"] is False
        assert facts["order_reference"] == "999999"
