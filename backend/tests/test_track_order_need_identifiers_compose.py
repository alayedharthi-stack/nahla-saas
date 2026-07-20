"""NL-V002 — track_order_need_identifiers constitution compose regression tests."""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.outbound_sanitizer import maybe_scrub_unkept_asset_promise  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.compose.responder import DefaultComposer  # noqa: E402
from modules.ai.brain.compose.track_order_need_identifiers_compose import (  # noqa: E402
    claims_invented_order_facts,
    extract_track_order_need_identifiers_facts,
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
    seed_tenant,
)
from tests.test_trusted_context_shadow_wireup import (  # noqa: E402
    _merchant_handler_convo,
    _merchant_handler_db,
    _merchant_handler_patch_ctx,
    _run,
)

GENERIC_MERCHANT = "متجر تجريبي عام"
_PHONE = DEFAULT_PHONE_E164
_DEDUP_SUBSTITUTE = "نحتاج تفاصيل إضافية عن طلبك لمساعدتك بشكل أدق."


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
        phone=_PHONE,
    )


def _facts() -> CommerceFacts:
    return CommerceFacts(
        has_products=True,
        product_count=5,
        in_stock_count=5,
        orderable=True,
        store_name=GENERIC_MERCHANT,
    )


def _ctx(
    tenant_ctx,
    message: str,
    *,
    db=None,
    slots: dict | None = None,
) -> BrainContext:
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


async def _compose_need_identifiers(
    db,
    tenant_ctx,
    message: str,
    *,
    llm_reply: str | None,
    force_compose_fail: bool = False,
) -> tuple:
    ctx = _ctx(tenant_ctx, message, db=db)
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
            ) as llm_mock:
                reply = await composer.compose(decision, result, ctx)
                assert llm_mock.await_count == 1
        else:
            reply = await composer.compose(decision, result, ctx)
    return decision, result, reply, ctx


class TestTrackOrderNeedIdentifiersCompose:
    def test_normal_path_uses_llm_compose_metadata(self, db, tenant_ctx) -> None:
        llm_reply = "أرسل رقم الطلب لو سمحت حتى أقدر أتحقق لك من حالته."
        _decision, result, reply, _ctx = asyncio.run(
            _compose_need_identifiers(
                db,
                tenant_ctx,
                "وين طلبي؟",
                llm_reply=llm_reply,
            )
        )
        assert result.data.get("chosen_path") == "track_order_need_order_number"
        assert result.data.get("compose_source") == "llm"
        assert result.data.get("response_mode") == "llm"
        assert result.data.get("final_customer_text_source") == "llm"
        assert result.data.get("track_order_need_identifiers", {}).get(
            "lookup_started"
        ) is False
        assert reply == llm_reply
        assert reply != T.track_order_need_identifiers_emergency_fallback()

    def test_english_input_allows_natural_llm_wording(self, db, tenant_ctx) -> None:
        llm_reply = "Please share your order number so I can check the status."
        _decision, result, reply, _ctx = asyncio.run(
            _compose_need_identifiers(
                db,
                tenant_ctx,
                "where is my order?",
                llm_reply=llm_reply,
            )
        )
        assert result.data.get("compose_source") == "llm"
        assert "order number" in reply.lower()
        assert "shipped" not in reply.lower()
        assert "delivered" not in reply.lower()

    def test_forced_compose_failure_uses_deterministic_fallback_metadata(
        self, db, tenant_ctx,
    ) -> None:
        _decision, result, reply, _ctx = asyncio.run(
            _compose_need_identifiers(
                db,
                tenant_ctx,
                "وين طلبي؟",
                llm_reply=None,
                force_compose_fail=True,
            )
        )
        assert result.data.get("compose_source") == "fallback_deterministic"
        assert result.data.get("fallback_reason") == "compose_failed_or_empty"
        assert result.data.get("fallback_action_type") == "track_order_need_identifiers"
        assert result.data.get("llm_candidate_present") is False
        assert result.data.get("final_customer_text_source") == "fallback_deterministic"
        assert reply == T.track_order_need_identifiers_emergency_fallback()

    def test_false_escalation_candidate_fails_closed_after_one_compose(
        self, db, tenant_ctx,
    ) -> None:
        _decision, result, reply, _ctx = asyncio.run(
            _compose_need_identifiers(
                db,
                tenant_ctx,
                "وين طلبي؟",
                llm_reply="تم تحويلك لفريق الدعم وبيتواصلون معك.",
            )
        )
        assert result.data.get("compose_source") == "fallback_deterministic"
        assert result.data.get("fallback_reason") == "compose_false_escalation_claim"
        assert result.data.get("fallback_action_type") == "track_order_need_identifiers"
        assert result.data.get("llm_candidate_present") is True
        assert reply == T.track_order_need_identifiers_emergency_fallback()
        assert "تم تحويلك" not in reply

    def test_sanitizer_does_not_replace_identifier_clarification_llm_reply(self) -> None:
        llm_reply = "أرسل رقم الطلب لو سمحت حتى أتحقق لك."
        scrubbed, changed, _asset = maybe_scrub_unkept_asset_promise(
            llm_reply,
            has_url=False,
            has_media=False,
            has_phone=False,
        )
        assert changed is False
        assert scrubbed == llm_reply

    def test_facts_do_not_invent_operational_claims(self) -> None:
        grounded = "أرسل رقم الطلب لو سمحت حتى أتحقق لك."
        assert not claims_invented_order_facts(grounded)
        assert claims_invented_order_facts("تم الشحن عبر شركة الشحن")

    def test_extract_facts_include_missing_identifier_shape(self, tenant_ctx) -> None:
        ctx = _ctx(tenant_ctx, "وين طلبي؟")
        facts = extract_track_order_need_identifiers_facts(
            ctx,
            SimpleNamespace(data={"message": "need_order_number"}),
        )
        assert facts["tracking_intent_recognized"] is True
        assert facts["lookup_started"] is False
        assert facts["order_verified"] is False
        assert facts["requested_identifier_types"] == ["order_number"]


class TestTrackOrderNeedIdentifiersWebhookProvenance:
    def test_webhook_dedup_retains_compose_provenance_metadata(self) -> None:
        pytest.importorskip("observability.event_logger")

        import models as _models  # noqa: PLC0415

        sys.modules.setdefault("database.models", _models)

        from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

        convo = _merchant_handler_convo()
        db = _merchant_handler_db()
        saved_metadata: dict = {}
        llm_reply = "أرسل رقم الطلب لو سمحت حتى أتحقق لك من حالته."

        def _capture_save(*_args, **kwargs):
            meta = kwargs.get("extra_metadata")
            if isinstance(meta, dict):
                saved_metadata.update(meta)

        def _dedup_history(*_args, **_kwargs):
            return [
                {"direction": "inbound", "body": "وين طلبي؟"},
                {"direction": "outbound", "body": llm_reply},
            ]

        brain_return = {
            "reply": llm_reply,
            "buttons": [],
            "handoff": False,
            "chosen_path": "track_order_need_order_number",
            "track_order_need_identifiers_compose_active": True,
            "compose_source": "llm",
            "response_mode": "llm",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
            "final_customer_text_source": "llm",
        }

        with _merchant_handler_patch_ctx(
            convo=convo,
            history_side_effect=_dedup_history,
        ) as (mock_brain, _state):
            with patch(
                "routers.whatsapp_webhook.StateManager.save_message",
                side_effect=_capture_save,
            ), patch(
                "core.order_flow.context_aware_dedup_fallback",
                return_value=_DEDUP_SUBSTITUTE,
            ):
                mock_brain.return_value.process = AsyncMock(return_value=dict(brain_return))
                _run(
                    _handle_merchant_message(
                        phone_id="PH1",
                        to=_PHONE,
                        text="وين طلبي؟",
                        tenant_id=1,
                        db=db,
                    )
                )

        assert saved_metadata
        assert saved_metadata.get("compose_source") == "llm"
        assert saved_metadata.get("chosen_path") == "track_order_need_order_number"
        assert saved_metadata.get("final_text_transformed") is True
        assert saved_metadata.get("final_customer_text_source") == "dedup_substitution"
        assert "chat_dedup_substitution" in list(
            saved_metadata.get("final_transform_reasons") or []
        )

    @pytest.mark.parametrize("tenant_id", [77, 88])
    def test_webhook_guard_routes_false_claim_to_existing_compose_fallback(
        self,
        tenant_id: int,
    ) -> None:
        pytest.importorskip("observability.event_logger")

        import models as _models  # noqa: PLC0415

        sys.modules.setdefault("database.models", _models)

        from routers.whatsapp_webhook import _handle_merchant_message  # noqa: PLC0415

        convo = _merchant_handler_convo(tenant_id=tenant_id)
        db = _merchant_handler_db()
        saved_metadata: dict = {}

        def _capture_save(*_args, **kwargs):
            meta = kwargs.get("extra_metadata")
            if isinstance(meta, dict):
                saved_metadata.update(meta)

        false_claim = "تم تحويلك لفريق الدعم وبيتواصلون معك."
        brain_return = {
            "reply": false_claim,
            "buttons": [],
            "handoff": False,
            "chosen_path": "track_order_need_order_number",
            "track_order_need_identifiers_compose_active": True,
            "compose_source": "llm",
            "response_mode": "llm",
            "llm_candidate_present": True,
            "final_text_transformed": False,
            "final_transform_reasons": [],
            "final_customer_text_source": "llm",
            "track_order_need_identifiers": {
                "tracking_intent_recognized": True,
                "lookup_started": False,
                "requested_identifier_types": ["order_number"],
            },
        }

        with _merchant_handler_patch_ctx(convo=convo) as (mock_brain, _state):
            with patch(
                "routers.whatsapp_webhook.StateManager.save_message",
                side_effect=_capture_save,
            ):
                mock_brain.return_value.process = AsyncMock(return_value=dict(brain_return))
                _run(
                    _handle_merchant_message(
                        phone_id="PH1",
                        to=_PHONE,
                        text="وين طلبي؟",
                        tenant_id=tenant_id,
                        db=db,
                    )
                )

        assert saved_metadata.get("compose_source") == "fallback_deterministic"
        assert saved_metadata.get("fallback_reason") == (
            "staff_escalation_truth_guard_false_claim"
        )
        assert saved_metadata.get("fallback_action_type") == (
            "track_order_need_identifiers"
        )
        assert saved_metadata.get("final_customer_text_source") == (
            "fallback_deterministic"
        )
        assert saved_metadata.get("final_text_transformed") is True
        assert saved_metadata.get("track_order_need_identifiers", {}).get(
            "lookup_started"
        ) is False

    def test_compose_uses_single_llm_call_without_trusted_context_load(
        self, db, tenant_ctx,
    ) -> None:
        llm_reply = "أرسل رقم الطلب لو سمحت."
        composer = DefaultComposer()
        ctx = _ctx(tenant_ctx, "وين طلبي؟", db=db)
        decision = DefaultDecisionEngine().decide(ctx)
        result = asyncio.run(TrackOrderHandler().handle(decision, ctx))

        shadow_mock = MagicMock()
        with patch(
            "modules.ai.brain.intent.link_disambiguation.should_use_generative_tracking_follow_up",
            return_value=False,
        ), patch(
            "modules.ai.brain.truth_surface.trusted_context.run_trusted_context_shadow",
            shadow_mock,
        ), patch.object(
            composer,
            "_llm_compose",
            new=AsyncMock(return_value=llm_reply),
        ) as llm_mock:
            reply = asyncio.run(composer.compose(decision, result, ctx))

        assert llm_mock.await_count == 1
        assert reply == llm_reply
        shadow_mock.assert_not_called()
