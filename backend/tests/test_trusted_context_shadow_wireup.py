"""Trusted Context shadow runtime wire-up tests (telemetry only)."""
from __future__ import annotations

import inspect
import json
import os
import sys
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
from modules.ai.brain.truth_surface.trusted_context import (  # noqa: E402
    clear_trusted_context,
    current_trusted_context,
    run_trusted_context_shadow,
    safe_shadow_trace_metadata,
)


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
    warning_msg = " ".join(str(c) for c in log_mock.warning.call_args[0])
    assert "RuntimeError" in warning_msg
    assert "SECRET" not in warning_msg
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


def test_no_templates_coupon_product_files_in_diff() -> None:
    import subprocess

    proc = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=os.path.abspath(os.path.join(_HERE, "../..")),
        capture_output=True,
        text=True,
    )
    files = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    forbidden = {
        "backend/routers/coupons.py",
        "backend/services/coupon_generator.py",
        "backend/services/promotion_engine.py",
        "backend/core/store_knowledge.py",
    }
    assert not files.intersection(forbidden)
    assert files <= {
        "backend/routers/whatsapp_webhook.py",
        "backend/modules/ai/brain/truth_surface/trusted_context.py",
        "backend/tests/test_trusted_context_shadow_wireup.py",
    } or files.issubset({
        "backend/routers/whatsapp_webhook.py",
        "backend/modules/ai/brain/truth_surface/trusted_context.py",
        "backend/tests/test_trusted_context_shadow_wireup.py",
    })
