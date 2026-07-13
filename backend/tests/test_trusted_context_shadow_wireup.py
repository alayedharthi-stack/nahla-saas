"""Trusted Context shadow runtime wire-up tests (telemetry only)."""
from __future__ import annotations

from contextlib import contextmanager
import asyncio
import inspect
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.trusted_context_layer1

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
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    current_trusted_context,
    pop_shadow_build_error_class,
    run_trusted_context_shadow,
    safe_shadow_trace_metadata,
)


def _run(coro):
    return asyncio.run(coro)


def _merchant_handler_db() -> MagicMock:
    db = MagicMock()
    db.commit = MagicMock()
    db.rollback = MagicMock()
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def _merchant_handler_convo(**kwargs) -> SimpleNamespace:
    defaults = dict(
        id=42,
        tenant_id=1,
        customer_id=7,
        ai_paused=False,
        ai_paused_reason=None,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        taken_over_by=None,
        status="active",
        extra_metadata={},
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@contextmanager
def _merchant_handler_patch_ctx(
    *,
    convo,
    shadow_mock=None,
    history_side_effect=None,
    whatsapp_send_mock=None,
):
    """Common patches to reach trusted-context wire-up and brain.process."""
    from contextlib import ExitStack

    state = SimpleNamespace(turn=0, stage="active", order_prep={})
    shadow_target = (
        "modules.ai.brain.truth_surface.trusted_context.run_trusted_context_shadow"
    )
    history_patch = patch(
        "routers.whatsapp_webhook.StateManager.load_history",
        side_effect=history_side_effect,
        return_value=None if history_side_effect else [{"role": "user", "content": "مرحبا"}],
    )
    with ExitStack() as stack:
        stack.enter_context(patch(
            "core.ai_disabled_gate.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=None, conversation=convo),
        ))
        stack.enter_context(patch(
            "modules.operations.structured_admin_contact_policy.evaluate_structured_admin_contact_policy",
            return_value=None,
        ))
        stack.enter_context(patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save_message"))
        stack.enter_context(history_patch)
        stack.enter_context(patch(
            "routers.whatsapp_webhook.StateManager.load",
            return_value=state,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.StateManager.save"))
        stack.enter_context(patch(
            "core.wa_usage.check_limit",
            return_value=SimpleNamespace(allowed=True, used_total=0, limit=1000, reason=""),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.conversational_priority.has_payment_outbound_consent",
            return_value=False,
        ))
        mock_brain = stack.enter_context(patch("modules.ai.brain.pipeline.get_brain"))
        stack.enter_context(patch(
            "modules.ai.routing.conversation_mode.resolve_conversation_mode",
        ))
        stack.enter_context(patch("modules.ai.routing.conversation_mode.save_lease"))
        stack.enter_context(patch(
            "core.ownership_state.resolve_ownership_state",
            return_value=SimpleNamespace(state="ai_active", takeover_class=""),
        ))
        stack.enter_context(patch(
            "core.ownership_state.attempt_implicit_takeover_recovery",
            return_value=SimpleNamespace(released=False, reason=""),
        ))
        stack.enter_context(patch(
            "core.ai_pause_guard.should_skip_ai",
            return_value=(False, None),
        ))
        stack.enter_context(patch(
            "modules.ai.order_flow_v2.owner.try_handle_order_flow_v2",
            return_value=SimpleNamespace(handled=False, reason="not_handled"),
        ))
        stack.enter_context(patch(
            "modules.ai.brain.commerce.inbound_fragment_guard.evaluate_duplicate_fragment_turn",
            return_value=SimpleNamespace(
                process_turn=True,
                send_clarification_once=False,
                reason="",
            ),
        ))
        stack.enter_context(patch("core.store_knowledge.build_ai_context", return_value={}))
        stack.enter_context(patch(
            "routers.whatsapp_webhook._send_whatsapp_message",
            new=whatsapp_send_mock or AsyncMock(return_value=True),
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=AsyncMock(return_value={"messages": [{"id": "wamid.test"}]}),
        ))
        stack.enter_context(patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ))
        cis_mock = stack.enter_context(patch(
            "services.customer_intelligence.CustomerIntelligenceService",
        ))
        cis_mock.return_value.upsert_lead_customer.return_value = SimpleNamespace(
            id=7, name="", email="",
        )
        cis_mock.return_value.ensure_profile.return_value = SimpleNamespace(
            segment="",
            customer_status="",
            rfm_segment="",
            is_returning=False,
            total_orders=0,
            total_spend_sar=0.0,
            last_order_at=None,
        )
        stack.enter_context(patch(
            "modules.ai.brain.truth_surface.flags.is_trusted_context_shadow_enabled",
            return_value=True,
        ))
        stack.enter_context(patch("routers.whatsapp_webhook.MERCHANT_BRAIN_ENABLED", True))
        stack.enter_context(patch("core.billing.has_billing_access", return_value=True))
        if shadow_mock is not None:
            stack.enter_context(patch(shadow_target, shadow_mock))
        yield mock_brain, state


def _snapshot(**kwargs) -> TrustedContextSnapshot:
    defaults = dict(
        tenant_id=1,
        customer_phone="966500000099",
        facts=[],
        loaded_domains=["customer"],
        sources=["order_context_builder"],
        shadow_observability={"coupon_count": 1, "loader_duration_ms": 9},
    )
    defaults.update(kwargs)
    snap = TrustedContextSnapshot(**defaults)
    snap.ensure_snapshot_id()
    return snap


def test_flag_disabled_no_loader_no_telemetry() -> None:
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
    ) as build_mock:
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
        )
    assert result is None
    build_mock.assert_not_called()


def test_flag_enabled_one_snapshot_id() -> None:
    clear_trusted_context()
    snap = _snapshot()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ):
        first = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000002",
        )
        second = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000002",
        )
    assert first is not None
    assert second is first
    assert first.snapshot_id == second.snapshot_id
    clear_trusted_context()


def test_social_turn_coupon_lazy_loader_not_invoked() -> None:
    with patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.should_load_coupon_promotion_facts",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.coupon_offer_loader.load_coupon_promotion_facts",
    ) as load_mock, patch(
        "modules.ai.brain.truth_surface.trusted_context._load_customer_order_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_state_order_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_payment_shipment_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_capability_facts",
        return_value=[],
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context._load_merchant_policy_facts",
        return_value=[],
    ):
        from modules.ai.brain.truth_surface.trusted_context import (  # noqa: PLC0415
            build_trusted_context_snapshot,
        )

        snap = build_trusted_context_snapshot(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000003",
            message="السلام عليكم",
        )
    load_mock.assert_not_called()
    assert TrustedDomain.COUPONS.value not in snap.loaded_domains


def test_coupon_question_loads_coupon_domain_masked_telemetry() -> None:
    clear_trusted_context()
    coupon_fact = TrustedFact(
        domain=TrustedDomain.COUPONS,
        key="coupon:1",
        value={
            "coupon_id": 1,
            "code_masked": "***10#abc",
            "code": "SECRET10",
            "eligible": True,
        },
        source=TruthSource.COUPON_TABLE,
    )
    snap = _snapshot(
        loaded_domains=["coupons", "promotions"],
        facts=[coupon_fact],
        shadow_observability={
            "coupon_count": 1,
            "eligible_coupon_count": 1,
            "loader_duration_ms": 11,
        },
    )
    trace = safe_shadow_trace_metadata(snap)
    log_payload = snap.to_log_dict()
    assert "coupons" in trace["loaded_domains"]
    assert trace["shadow_observability"]["coupon_count"] == 1
    assert "SECRET10" not in json.dumps(trace)
    assert "SECRET10" not in json.dumps(log_payload)
    assert "facts" not in log_payload
    clear_trusted_context()


def test_offer_question_promotion_domain_no_raw_conditions() -> None:
    promo_fact = TrustedFact(
        domain=TrustedDomain.PROMOTIONS,
        key="promotion:9",
        value={
            "promotion_id": 9,
            "applicable_products": ["sku-1"],
            "product_result": "advisory",
            "eligible": None,
        },
        source=TruthSource.PROMOTION_TABLE,
    )
    snap = _snapshot(
        loaded_domains=["promotions"],
        facts=[promo_fact],
        shadow_observability={"promotion_count": 1, "loader_duration_ms": 8},
    )
    trace = safe_shadow_trace_metadata(snap)
    assert "promotions" in trace["loaded_domains"]
    assert "sku-1" not in json.dumps(trace)
    assert "applicable_products" not in json.dumps(trace)


def test_internal_code_not_in_log_or_metadata() -> None:
    snap = _snapshot(
        facts=[
            TrustedFact(
                domain=TrustedDomain.COUPONS,
                key="coupon:2",
                value={"code": "HIDDEN99", "code_masked": "***99#hash"},
                source=TruthSource.COUPON_TABLE,
            )
        ],
    )
    meta = snap.to_metadata()
    log = snap.to_log_dict()
    trace = safe_shadow_trace_metadata(snap)
    for payload in (meta, log, trace):
        assert "HIDDEN99" not in json.dumps(payload)
        assert "facts" not in payload


def test_loader_exception_fail_open_safe_error_class() -> None:
    clear_trusted_context()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        side_effect=RuntimeError("sensitive coupon SECRET"),
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.logger",
    ) as log_mock:
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000004",
        )
    assert result is None
    assert current_trusted_context() is None
    assert pop_shadow_build_error_class() == "RuntimeError"
    assert pop_shadow_build_error_class() is None
    warning_msg = " ".join(str(c) for c in log_mock.warning.call_args[0])
    assert "RuntimeError" in warning_msg
    assert "SECRET" not in warning_msg
    clear_trusted_context()


def test_pop_shadow_build_error_class_before_webhook_consumes() -> None:
    clear_trusted_context()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        side_effect=TimeoutError("db timeout secret"),
    ):
        run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000099",
        )
    assert pop_shadow_build_error_class() == "TimeoutError"
    assert pop_shadow_build_error_class() is None
    clear_trusted_context()


def test_empty_snapshot_is_ok_not_build_error() -> None:
    snap = _snapshot(facts=[])
    meta = safe_shadow_trace_metadata(snap)
    assert meta["trusted_context_shadow_status"] == "ok"
    assert meta["fact_count"] == 0


def test_stale_contextvar_not_reused_for_different_turn() -> None:
    clear_trusted_context()
    snap_a = _snapshot(customer_phone="966500000010")
    snap_b = _snapshot(customer_phone="966500000011")
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        side_effect=[snap_a, snap_b],
    ) as build_mock:
        first = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000010",
            conversation_id=10,
        )
        second = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000011",
            conversation_id=11,
        )
    assert first is snap_a
    assert second is snap_b
    assert first is not second
    assert build_mock.call_count == 2
    clear_trusted_context()


def test_duplicate_prevention_single_build_call() -> None:
    clear_trusted_context()
    snap = _snapshot()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ) as build_mock:
        run_trusted_context_shadow(
            db=MagicMock(), tenant_id=1, customer_phone="966500000005",
        )
        run_trusted_context_shadow(
            db=MagicMock(), tenant_id=1, customer_phone="966500000005",
        )
    assert build_mock.call_count == 1
    clear_trusted_context()


def test_context_cleared_between_turns() -> None:
    clear_trusted_context()
    snap = _snapshot()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ):
        run_trusted_context_shadow(
            db=MagicMock(), tenant_id=1, customer_phone="966500000006",
        )
        assert current_trusted_context() is not None
        clear_trusted_context()
        assert current_trusted_context() is None


def test_webhook_wireup_does_not_touch_brain_process_args() -> None:
    source = inspect.getsource(
        __import__("routers.whatsapp_webhook", fromlist=["x"])._handle_merchant_message,
    )
    wire_idx = source.index("run_trusted_context_shadow")
    brain_idx = source.index("brain.process(")
    assert wire_idx < brain_idx
    assert "trusted_context_projection" not in source
    assert "projection(" not in source[wire_idx:brain_idx]


def test_legacy_coupon_context_builder_untouched() -> None:
    from core import store_knowledge  # noqa: PLC0415

    assert hasattr(store_knowledge.CouponContextBuilder, "build_context_block")


def test_no_decision_plan_or_projection_wiring() -> None:
    source = inspect.getsource(
        __import__("routers.whatsapp_webhook", fromlist=["x"])._handle_merchant_message,
    )
    wire_block = source.split("run_trusted_context_shadow", 1)[1].split("brain.process(", 1)[0]
    assert "decision_plan" not in wire_block.lower()
    assert "trusted_context_projection" not in wire_block
    assert "projection(" not in wire_block


def test_coupon_eligibility_module_unchanged_by_wireup() -> None:
    import subprocess

    proc = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD", "--", "backend/modules/ai/brain/truth_surface/coupon_offer_loader.py"],
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == ""


def test_constitution_compliance_green() -> None:
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "backend/tests/test_constitution_compliance.py", "-q", "--tb=no"],
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_branch_diff_excludes_other_agent_scope_paths() -> None:
    """Branch diff must not touch lifecycle/templates/coupon-order agent-owned paths."""
    import subprocess

    proc = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        capture_output=True,
        text=True,
    )
    files = {line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip()}

    forbidden_exact = {
        "backend/routers/coupons.py",
        "backend/services/coupon_generator.py",
        "backend/services/promotion_engine.py",
        "backend/core/store_knowledge.py",
    }
    forbidden_scope_markers = (
        "backend/core/commerce_lifecycle/",
        "backend/modules/ai/commerce_agent/",
        "backend/modules/ai/adapters/whatsapp_dispatch.py",
        "backend/modules/ai/postprocess/outbound_belt.py",
        "/templates/",
        "/campaigns/",
        "/lifecycle/",
        "/approved_template",
        "/order_execution/",
        "/checkout_execution/",
        "/payment_execution/",
        "coupon_generator",
        "promotion_engine",
        "/routers/coupons.py",
        "store_knowledge.py",
    )

    assert not files.intersection(forbidden_exact)
    for path in files:
        lowered = path.lower()
        assert not any(marker in lowered for marker in forbidden_scope_markers), path


def test_handle_merchant_message_shadow_wire_integration() -> None:
    from routers.whatsapp_webhook import _handle_merchant_message

    clear_trusted_context()
    convo = _merchant_handler_convo()
    db = _merchant_handler_db()
    snap = _snapshot()
    call_order: list[str] = []
    brain_reply = "رد تجريبي من الدماغ"

    def _shadow_side_effect(**_kwargs):
        call_order.append("shadow")
        return snap

    shadow_mock = MagicMock(side_effect=_shadow_side_effect)

    def _history_side_effect(*_args, **_kwargs):
        call_order.append("history")
        return [{"role": "user", "content": "مرحبا"}]

    with _merchant_handler_patch_ctx(
        convo=convo,
        shadow_mock=shadow_mock,
        history_side_effect=_history_side_effect,
    ) as (
        mock_brain,
        _state,
    ):
        mock_brain.return_value.process = AsyncMock(
            return_value={"reply": brain_reply, "buttons": []},
        )
        _run(_handle_merchant_message(
            phone_id="PH1",
            to="966500000099",
            text="مرحبا كيف الحال",
            tenant_id=1,
            db=db,
        ))

    assert call_order.index("history") < call_order.index("shadow")
    shadow_mock.assert_called_once()
    mock_brain.return_value.process.assert_called_once()
    brain_kwargs = mock_brain.return_value.process.call_args.kwargs
    assert "trusted_context" not in brain_kwargs
    assert "projection" not in brain_kwargs
    assert "known_facts" not in brain_kwargs
    assert current_trusted_context() is None
    clear_trusted_context()


def test_handle_merchant_message_shadow_fail_open_brain_unchanged() -> None:
    from routers.whatsapp_webhook import _handle_merchant_message

    clear_trusted_context()
    convo = _merchant_handler_convo()
    db = _merchant_handler_db()
    brain_reply = "نفس الرد"

    shadow_mock = MagicMock(return_value=None)

    with patch(
        "modules.ai.brain.truth_surface.trusted_context.pop_shadow_build_error_class",
        return_value="RuntimeError",
    ), _merchant_handler_patch_ctx(convo=convo, shadow_mock=shadow_mock) as (
        mock_brain,
        _state,
    ):
        mock_brain.return_value.process = AsyncMock(
            return_value={"reply": brain_reply, "buttons": []},
        )
        _run(_handle_merchant_message(
            phone_id="PH1",
            to="966500000099",
            text="عندكم عروض؟",
            tenant_id=1,
            db=db,
        ))

    shadow_mock.assert_called_once()
    mock_brain.return_value.process.assert_called_once()
    assert current_trusted_context() is None
    clear_trusted_context()


def test_handle_merchant_message_coupon_loader_failure_fail_open_unchanged(caplog) -> None:
    import logging

    from modules.ai.brain.truth_surface import trusted_context
    from modules.ai.brain.truth_surface import coupon_offer_loader
    from routers.whatsapp_webhook import _handle_merchant_message
    from services.turn_trace import new_trace as _real_new_trace

    caplog.set_level(logging.WARNING, logger="nahla.brain.trusted_context")
    caplog.set_level(logging.INFO, logger="nahla.brain.trusted_context")

    clear_trusted_context()
    convo = _merchant_handler_convo()
    db = _merchant_handler_db()
    brain_reply = "نفس الرد"
    send_mock = AsyncMock(return_value=True)
    captured_traces: list = []

    def _capture_new_trace(**kwargs):
        trace = _real_new_trace(**kwargs)
        captured_traces.append(trace)
        return trace

    with patch.object(trusted_context, "_load_customer_order_facts", return_value=[]), patch.object(
        trusted_context, "_load_state_order_facts", return_value=[]
    ), patch.object(trusted_context, "_load_payment_shipment_facts", return_value=[]), patch.object(
        trusted_context, "_load_capability_facts", return_value=[]
    ), patch.object(trusted_context, "_load_merchant_policy_facts", return_value=[]), patch(
        "core.active_order_context.load_commerce_bundle_from_db", return_value={}
    ), patch.object(
        coupon_offer_loader, "should_load_coupon_promotion_facts", return_value=True
    ), patch.object(
        coupon_offer_loader,
        "load_coupon_promotion_facts",
        side_effect=RuntimeError("secret-value"),
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "services.turn_trace.new_trace",
        side_effect=_capture_new_trace,
    ), _merchant_handler_patch_ctx(
        convo=convo,
        whatsapp_send_mock=send_mock,
    ) as (
        mock_brain,
        _state,
    ):
        mock_brain.return_value.process = AsyncMock(
            return_value={"reply": brain_reply, "buttons": []},
        )
        caplog.clear()
        _run(_handle_merchant_message(
            phone_id="PH1",
            to="966500000099",
            text="عندكم كوبون؟",
            tenant_id=1,
            db=db,
        ))

    mock_brain.return_value.process.assert_called_once()
    brain_kwargs = mock_brain.return_value.process.call_args.kwargs
    assert "trusted_context" not in brain_kwargs
    assert "projection" not in brain_kwargs
    assert "known_facts" not in brain_kwargs
    send_mock.assert_awaited()
    assert current_trusted_context() is None
    assert pop_shadow_build_error_class() is None
    assert len(captured_traces) == 1
    trace_extra = captured_traces[0].extra
    assert trace_extra["trusted_context_shadow_status"] == "build_error"
    assert trace_extra["trusted_context_shadow_error_class"] == "RuntimeError"
    assert trace_extra["trusted_context_shadow_stage"] == "build"

    log_text = caplog.text
    assert "stage=coupon_promotion_loader" in log_text
    assert "stage=build" in log_text
    assert "RuntimeError" in log_text
    assert "secret-value" not in log_text
    assert "snapshot_id" not in log_text
    clear_trusted_context()


def test_layer2_flag_disabled_no_envelope_no_builder_calls() -> None:
    clear_trusted_context()
    snap = _snapshot()
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_layer2_shadow_enabled",
        return_value=False,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ), patch(
        "modules.ai.brain.truth_surface.layer2.build_intent_evidence",
    ) as evidence_mock, patch(
        "modules.ai.brain.truth_surface.layer2.build_decision_plan_shadow",
    ) as plan_mock:
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000020",
            message="عندكم عروض؟",
        )
    assert result is snap
    assert "layer2_shadow" not in (result.shadow_observability or {})
    evidence_mock.assert_not_called()
    plan_mock.assert_not_called()
    clear_trusted_context()


def test_layer2_flag_enabled_attaches_safe_envelope_on_offer_turn() -> None:
    from modules.ai.brain.truth_surface.layer2 import (  # noqa: PLC0415
        build_decision_plan_shadow,
        build_intent_evidence,
    )

    clear_trusted_context()
    snap = _snapshot(loaded_domains=["customer", "capabilities", "promotions"])
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_layer2_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ):
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000021",
            message="عندكم عروض؟",
        )
    assert result is snap
    layer2 = (result.shadow_observability or {}).get("layer2_shadow")
    assert layer2 is not None
    assert layer2["status"] == "ok"
    assert "intent_evidence" in layer2
    assert "decision_plan" in layer2
    assert isinstance(layer2.get("duration_ms"), int)

    expected_evidence = build_intent_evidence(
        message="عندكم عروض؟",
        source_turn_ref=snap.snapshot_id or "",
    )
    expected_plan = build_decision_plan_shadow(evidence=expected_evidence, snapshot=snap)
    assert layer2["intent_evidence"] == expected_evidence.to_dict()
    assert layer2["decision_plan"] == expected_plan.to_metadata()
    assert "offer_intent" in layer2["intent_evidence"]["trigger_ids"]

    trace = safe_shadow_trace_metadata(snap)
    log_payload = snap.to_log_dict()
    for payload in (trace, log_payload):
        assert "facts" not in payload
        blob = json.dumps(payload, ensure_ascii=False)
        assert "عروض" not in blob
        assert "عندكم" not in blob
        assert layer2["decision_plan"]["proposed_action"] in blob
    clear_trusted_context()


def test_layer2_compare_failure_fail_open_preserves_snapshot() -> None:
    clear_trusted_context()
    snap = _snapshot()
    snap.snapshot_id = "1234567890abcdef1234567890abcdef"
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_layer2_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ), patch(
        "modules.ai.brain.truth_surface.layer2.build_decision_plan_shadow",
        side_effect=RuntimeError("secret coupon SECRET99"),
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.logger",
    ) as log_mock:
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000022",
            message="عندكم عروض؟",
        )
    assert result is snap
    layer2 = (result.shadow_observability or {}).get("layer2_shadow")
    assert layer2 == {
        "status": "error",
        "stage": "layer2_compare",
        "error_class": "RuntimeError",
    }
    warning_msg = " ".join(str(c) for c in log_mock.warning.call_args[0])
    assert "layer2_failed" in warning_msg
    assert "layer2_compare" in warning_msg
    assert "RuntimeError" in warning_msg
    assert "SECRET99" not in warning_msg
    assert pop_shadow_build_error_class() is None
    clear_trusted_context()


def test_layer2_uuid_snapshot_id_produces_ok_envelope() -> None:
    clear_trusted_context()
    snap = _snapshot(loaded_domains=["customer", "capabilities", "promotions"])
    snap.snapshot_id = "1234567890abcdef1234567890abcdef"
    with patch(
        "modules.ai.brain.truth_surface.trusted_context.is_trusted_context_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.is_layer2_shadow_enabled",
        return_value=True,
    ), patch(
        "modules.ai.brain.truth_surface.trusted_context.build_trusted_context_snapshot",
        return_value=snap,
    ):
        result = run_trusted_context_shadow(
            db=MagicMock(),
            tenant_id=1,
            customer_phone="966500000023",
            message="عندكم عروض؟",
        )
    assert result is snap
    layer2 = (result.shadow_observability or {}).get("layer2_shadow")
    assert layer2 is not None
    assert layer2["status"] == "ok"
    assert layer2["intent_evidence"]["source_turn_ref"] == snap.snapshot_id
    assert layer2["decision_plan"]["snapshot_ref"] == snap.snapshot_id
    assert isinstance(layer2.get("duration_ms"), int)
    clear_trusted_context()


def test_layer2_default_flag_disabled_without_patch(monkeypatch) -> None:
    monkeypatch.delenv("NAHLA_LAYER2_SHADOW_ENABLED", raising=False)
    from modules.ai.brain.truth_surface.flags import is_layer2_shadow_enabled  # noqa: PLC0415

    assert is_layer2_shadow_enabled() is False
