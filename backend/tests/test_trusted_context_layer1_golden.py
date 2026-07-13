"""Layer 1 Trusted Context golden scenarios — local mocks only, no provider I/O."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.contract import (  # noqa: E402
    TrustedContextSnapshot,
    TrustedDomain,
    TrustedFact,
    TruthSource,
)
from modules.ai.brain.truth_surface.coupon_offer_loader import (  # noqa: E402
    build_coupon_eligibility_record,
    build_promotion_eligibility_record,
    load_coupon_promotion_facts,
)
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    current_trusted_context,
    run_trusted_context_shadow,
    safe_shadow_trace_metadata,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coupon(**overrides):
    value = dict(
        id=1,
        tenant_id=101,
        code="SAVE10",
        source_type="manual",
        expires_at=_now() + timedelta(days=7),
        extra_metadata={},
        rules=[],
    )
    value.update(overrides)
    return SimpleNamespace(**value)


def _promotion(**overrides):
    value = dict(
        id=7,
        tenant_id=101,
        status="active",
        promotion_type="percentage",
        discount_value=10,
        conditions={},
        starts_at=_now() - timedelta(hours=1),
        ends_at=_now() + timedelta(days=7),
        usage_count=0,
        usage_limit=None,
        extra_metadata={},
    )
    value.update(overrides)
    return SimpleNamespace(**value)


def _snapshot(**overrides) -> TrustedContextSnapshot:
    value = dict(
        tenant_id=101,
        customer_phone="966500000099",
        facts=[],
        loaded_domains=["customer", "capabilities"],
        sources=["test"],
        shadow_observability={"loader_duration_ms": 1},
    )
    value.update(overrides)
    result = TrustedContextSnapshot(**value)
    result.ensure_snapshot_id()
    return result


def _coupon_db(coupons, promotions):
    db = MagicMock()
    db.commit = MagicMock()
    db.add = MagicMock()

    def _query(model):
        query = MagicMock()
        name = getattr(model, "__name__", str(model))
        if name == "Coupon":
            query.filter.return_value.limit.return_value.all.return_value = coupons
        elif name == "Promotion":
            query.filter.return_value.limit.return_value.all.return_value = promotions
        elif name == "CustomerProfile":
            query.filter.return_value.first.return_value = None
        return query

    db.query.side_effect = _query
    return db


def test_golden_01_social_greeting_uses_base_without_offer_loader() -> None:
    from modules.ai.brain.truth_surface import trusted_context

    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts"
    ) as offers:
        snap = trusted_context.build_trusted_context_snapshot(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001", message="السلام عليكم"
        )
    offers.assert_not_called()
    assert TrustedDomain.COUPONS.value not in snap.loaded_domains
    assert TrustedDomain.PROMOTIONS.value not in snap.loaded_domains


def test_golden_02_active_coupon_is_eligible_and_masked_from_telemetry() -> None:
    record = build_coupon_eligibility_record(
        _coupon(), tenant_id=101, customer_id=9, basket_total=200.0,
        applied_codes=set(), observed_at=_now().isoformat(),
    )
    assert record["eligible"] is True
    fact = TrustedFact(TrustedDomain.COUPONS, "coupon:1", {"code": "SAVE10", **record},
                       TruthSource.COUPON_TABLE)
    trace = safe_shadow_trace_metadata(_snapshot(facts=[fact], loaded_domains=["coupons"]))
    assert "SAVE10" not in json.dumps(trace)
    assert "facts" not in trace


def test_golden_03_expired_coupon_is_not_eligible() -> None:
    record = build_coupon_eligibility_record(
        _coupon(expires_at=_now() - timedelta(seconds=1)), tenant_id=101, customer_id=9,
        basket_total=200.0, applied_codes=set(), observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "expired"


@pytest.mark.parametrize("metadata,reason", [
    ({"active": False}, "disabled"),
    ({"usage_count": 3, "usage_limit": 3}, "usage_limit_reached"),
])
def test_golden_04_disabled_or_exhausted_coupon_is_not_eligible(metadata, reason) -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata=metadata), tenant_id=101, customer_id=9, basket_total=200.0,
        applied_codes=set(), observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == reason


def test_golden_05_personal_coupon_rejects_wrong_customer() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"customer_id": 99}), tenant_id=101, customer_id=9,
        basket_total=200.0, applied_codes=set(), observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "customer_restriction"


def test_golden_06_missing_cart_total_is_unverified_not_false_claim() -> None:
    record = build_coupon_eligibility_record(
        _coupon(extra_metadata={"min_order_amount": 300}), tenant_id=101, customer_id=9,
        basket_total=None, applied_codes=set(), observed_at=_now().isoformat(),
    )
    assert record["eligible"] is None
    assert record["verified"] is False


def test_golden_07_active_offer_is_read_only_and_no_usage_mutation() -> None:
    promotion = _promotion()
    before = promotion.usage_count
    record = build_promotion_eligibility_record(
        promotion, tenant_id=101, customer_profile=None, basket_total=100.0,
        observed_at=_now().isoformat(),
    )
    assert record["active_window_result"] == "active"
    assert promotion.usage_count == before


def test_golden_08_product_category_offer_is_advisory() -> None:
    record = build_promotion_eligibility_record(
        _promotion(conditions={"applicable_categories": [4]}), tenant_id=101, basket_total=100.0,
        customer_profile=None, observed_at=_now().isoformat(),
    )
    assert record["eligible"] is None
    assert record["verified"] is False


def test_golden_09_multiple_promotions_do_not_fabricate_winner() -> None:
    db = _coupon_db([], [_promotion(id=1), _promotion(id=2)])
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader._resolve_customer_id",
        return_value=9,
    ):
        facts, _obs = load_coupon_promotion_facts(
            db=db, tenant_id=101, customer_phone="966500000001", message="عندكم عرض؟"
        )
    promotions = [f for f in facts if f.domain == TrustedDomain.PROMOTIONS]
    assert len(promotions) >= 2
    assert all("winner" not in str(f.value).lower() for f in promotions)
    db.commit.assert_not_called()
    db.add.assert_not_called()


def test_golden_10_cross_tenant_coupon_records_are_not_loaded() -> None:
    record = build_coupon_eligibility_record(
        _coupon(tenant_id=202), tenant_id=101, customer_id=9, basket_total=200.0,
        applied_codes=set(), observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "tenant_mismatch"


def test_golden_11_cross_tenant_promotion_records_are_not_loaded() -> None:
    record = build_promotion_eligibility_record(
        _promotion(tenant_id=202), tenant_id=101, customer_profile=None,
        basket_total=200.0, observed_at=_now().isoformat(),
    )
    assert record["eligible"] is False
    assert record["reason_when_unavailable"] == "tenant_mismatch"


def test_golden_12_top_level_build_exception_is_fail_open_with_class_only() -> None:
    clear_trusted_context()
    brain = AsyncMock(return_value={"reply": "unchanged", "buttons": []})
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        side_effect=RuntimeError("secret body"),
    ), patch("modules.ai.brain.truth_surface.trusted_context.logger") as logger:
        result = run_trusted_context_shadow(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001"
        )
    assert result is None
    assert "RuntimeError" in " ".join(str(x) for x in logger.warning.call_args.args)
    assert "secret body" not in " ".join(str(x) for x in logger.warning.call_args.args)
    assert asyncio.run(brain())["reply"] == "unchanged"
    clear_trusted_context()


def test_golden_12b_coupon_loader_exception_is_fail_open_with_class_only() -> None:
    from modules.ai.brain.truth_surface import trusted_context
    from modules.ai.brain.truth_surface.trusted_context import pop_shadow_build_error_class

    clear_trusted_context()
    db = MagicMock()
    db.commit = MagicMock()
    db.add = MagicMock()
    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]), patch(
        "core.active_order_context.load_commerce_bundle_from_db", return_value={}
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts",
        side_effect=RuntimeError("secret-value"),
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch("modules.ai.brain.truth_surface.trusted_context.logger") as logger:
        result = run_trusted_context_shadow(
            db=db,
            tenant_id=101,
            customer_phone="966500000001",
            message="عندكم كوبون؟",
        )

    warning_text = " ".join(
        str(arg)
        for call in logger.warning.call_args_list
        for arg in call.args
    )
    info_text = " ".join(
        str(arg)
        for call in logger.info.call_args_list
        for arg in call.args
    )

    assert result is None
    assert current_trusted_context() is None
    assert pop_shadow_build_error_class() == "RuntimeError"
    assert pop_shadow_build_error_class() is None
    assert "stage=coupon_promotion_loader" in warning_text
    assert "stage=build" in warning_text
    assert "RuntimeError" in warning_text
    assert "secret-value" not in warning_text
    assert "secret-value" not in info_text
    assert "snapshot_id" not in info_text
    assert logger.exception.call_count == 0
    db.commit.assert_not_called()
    db.add.assert_not_called()
    clear_trusted_context()


def test_golden_13_duplicate_call_same_turn_builds_once() -> None:
    clear_trusted_context()
    snap = _snapshot()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ) as build:
        one = run_trusted_context_shadow(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001", conversation_id=1
        )
        two = run_trusted_context_shadow(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001", conversation_id=1
        )
    assert one is two
    assert build.call_count == 1
    clear_trusted_context()


def test_golden_14_new_turn_does_not_reuse_snapshot() -> None:
    clear_trusted_context()
    first, second = _snapshot(), _snapshot()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        side_effect=[first, second],
    ):
        assert run_trusted_context_shadow(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001", conversation_id=1
        ) is first
        assert run_trusted_context_shadow(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001", conversation_id=2
        ) is second
    clear_trusted_context()


def test_golden_15_concurrent_turn_contextvars_are_isolated() -> None:
    snapshots = {
        ("966500000001", 1): _snapshot(customer_phone="966500000001", conversation_id=1),
        ("966500000002", 2): _snapshot(customer_phone="966500000002", conversation_id=2),
    }

    def _build_side_effect(**kwargs):
        phone = str(kwargs.get("customer_phone") or "").strip()
        conversation_id = kwargs.get("conversation_id")
        return snapshots[(phone, conversation_id)]

    async def _turn(phone: str, conversation_id: int):
        clear_trusted_context()
        with patch(
            "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
            return_value=True,
        ):
            result = run_trusted_context_shadow(
                db=MagicMock(), tenant_id=101, customer_phone=phone, conversation_id=conversation_id
            )
            await asyncio.sleep(0)
            return result.snapshot_id, current_trusted_context().snapshot_id

    async def _run_concurrent():
        return await asyncio.gather(
            _turn("966500000001", 1), _turn("966500000002", 2),
        )

    with patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        side_effect=_build_side_effect,
    ):
        one, two = asyncio.run(_run_concurrent())
    assert one[0] == one[1]
    assert two[0] == two[1]
    assert one[0] != two[0]
    clear_trusted_context()


def test_golden_16_disabled_flag_has_no_build_or_telemetry() -> None:
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
    ) as build, patch("modules.ai.brain.truth_surface.trusted_context.logger") as logger:
        assert run_trusted_context_shadow(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001"
        ) is None
    build.assert_not_called()
    logger.info.assert_not_called()


def test_golden_17_internal_code_never_reaches_trace_or_log_payload() -> None:
    fact = TrustedFact(
        TrustedDomain.COUPONS, "coupon:1", {"code": "PRIVATE10", "code_masked": "***10#x"},
        TruthSource.COUPON_TABLE,
    )
    snap = _snapshot(facts=[fact], loaded_domains=["coupons"])
    assert "PRIVATE10" not in json.dumps(snap.to_log_dict())
    assert "PRIVATE10" not in json.dumps(safe_shadow_trace_metadata(snap))


def test_golden_18_snapshot_build_does_not_mutate_db() -> None:
    from modules.ai.brain.truth_surface import trusted_context

    db = MagicMock()
    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]):
        trusted_context.build_trusted_context_snapshot(
            db=db, tenant_id=101, customer_phone="966500000001", message="مرحبا"
        )
    db.commit.assert_not_called()
    db.add.assert_not_called()


def test_golden_19_local_loader_latency_has_generous_budget() -> None:
    from modules.ai.brain.truth_surface import trusted_context

    started = time.perf_counter()
    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]):
        trusted_context.build_trusted_context_snapshot(
            db=MagicMock(), tenant_id=101, customer_phone="966500000001", message="مرحبا"
        )
    assert (time.perf_counter() - started) < 1.0


def test_golden_20_handler_path_keeps_brain_and_dispatch_shape_unchanged() -> None:
    """The existing wire-up integration suite owns full handler patch coverage."""
    from routers.whatsapp_webhook import _handle_merchant_message

    convo = SimpleNamespace(
        id=42, tenant_id=101, customer_id=7, ai_paused=False, ai_paused_reason=None,
        is_human_handoff=False, needs_human=False, handoff_active=False,
        paused_by_human=False, taken_over_at=None, taken_over_by=None, status="active",
        extra_metadata={},
    )
    db = MagicMock()
    state = SimpleNamespace(turn=0, stage="active", order_prep={})
    snapshot = _snapshot(conversation_id=42)
    with patch("core.ai_disabled_gate.is_ai_disabled_for_conversation",
               return_value=SimpleNamespace(disabled=False, reason=None, conversation=convo)), patch(
        "routers.conversations._get_or_create_conversation", return_value=convo
    ), patch("routers.whatsapp_webhook.StateManager.save_message"), patch(
        "routers.whatsapp_webhook.StateManager.load_history", return_value=[]
    ), patch("routers.whatsapp_webhook.StateManager.load", return_value=state), patch(
        "routers.whatsapp_webhook.StateManager.save"
    ), patch("modules.operations.structured_admin_contact_policy.evaluate_structured_admin_contact_policy",
             return_value=None), patch("core.wa_usage.check_limit",
             return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason="")), patch(
        "modules.ai.brain.commerce.conversational_priority.has_payment_outbound_consent",
        return_value=False
    ), patch("modules.ai.brain.pipeline.get_brain") as get_brain, patch(
        "modules.ai.routing.conversation_mode.resolve_conversation_mode"
    ), patch("modules.ai.routing.conversation_mode.save_lease"), patch(
        "core.ownership_state.resolve_ownership_state",
        return_value=SimpleNamespace(state="ai_active", takeover_class="")
    ), patch("core.ownership_state.attempt_implicit_takeover_recovery",
             return_value=SimpleNamespace(released=False, reason="")), patch(
        "core.ai_pause_guard.should_skip_ai", return_value=(False, None)
    ), patch("modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
             return_value=SimpleNamespace(handled=False, reason="not_handled")), patch(
        "modules.ai.brain.commerce.inbound_fragment_guard.evaluate_duplicate_fragment_turn",
        return_value=SimpleNamespace(process_turn=True, send_clarification_once=False, reason="")
    ), patch("core.store_knowledge.build_ai_context", return_value={}), patch(
        "routers.whatsapp_webhook._send_whatsapp_message", new=AsyncMock(return_value=True)
    ) as send, patch(
        "services.customer_intelligence.CustomerIntelligenceService"
    ) as intelligence, patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snapshot
    ):
        intelligence.return_value.upsert_lead_customer.return_value = SimpleNamespace(id=7, name="", email="")
        intelligence.return_value.ensure_profile.return_value = SimpleNamespace(
            segment="", customer_status="", rfm_segment="", is_returning=False,
            total_orders=0, total_spend_sar=0.0, last_order_at=None
        )
        get_brain.return_value.process = AsyncMock(return_value={"reply": "model output", "buttons": []})
        asyncio.run(_handle_merchant_message(
            phone_id="test", to="966500000001", text="مرحبا", tenant_id=101, db=db
        ))
    get_brain.return_value.process.assert_called_once()
    assert "trusted_context" not in get_brain.return_value.process.call_args.kwargs
    send.assert_awaited()
