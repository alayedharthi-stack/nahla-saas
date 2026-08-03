"""P0-C OrderFlowV2 per-tenant readiness — enforce allowlist, shadow no-write, P0-A/P0-B."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_disabled_gate import REASON_AI_PAUSED  # noqa: E402
from models import CommercePermissions  # noqa: E402
from modules.ai.order_flow_v2.enforcement import resolve_order_flow_v2_operational  # noqa: E402
from modules.ai.order_flow_v2.owner import (  # noqa: E402
    OrderFlowV2Result,
    persist_order_flow_v2_result,
    try_handle_order_flow_v2,
)
from modules.ai.order_flow_v2.tenant_rollout import parse_order_flow_v2_tenant_ids  # noqa: E402
from tests.commerce_scenario_fixtures import make_scenario_db, seed_tenant  # noqa: E402

_GENERIC_ITEM = {
    "product_id": "sku-sport-shoe-01",
    "product_name": "حذاء رياضي أبيض",
    "quantity": 1,
    "catalog_price": 249.0,
}

_PERFUME_ITEM = {
    "product_id": "sku-perfume-rose",
    "product_name": "عطر ورد 100ml",
    "quantity": 1,
    "catalog_price": 180.0,
}


@pytest.fixture(autouse=True)
def _reset_rollout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORDER_FLOW_V2_ENFORCE_TENANTS", raising=False)
    monkeypatch.delenv("ORDER_FLOW_V2_DISABLED_TENANTS", raising=False)
    monkeypatch.setenv("ORDER_FLOW_V2_ENABLED", "false")
    monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "true")
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", False, raising=False)
    monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", True, raising=False)


def _conversation(*, conv_id: int = 42, ai_paused: bool = False, status: str = "active"):
    return SimpleNamespace(
        id=conv_id,
        tenant_id=1,
        ai_paused=ai_paused,
        ai_paused_reason=REASON_AI_PAUSED if ai_paused else "",
        status=status,
        is_human_handoff=False,
        needs_human=False,
        handoff_active=False,
        paused_by_human=False,
        taken_over_at=None,
        extra_metadata={"brain_state": {"order_prep": {}, "cart_items": []}},
    )


def _seed_permissions(db, tenant_id: int, *, can_create_orders: bool = True) -> None:
    row = CommercePermissions(
        tenant_id=tenant_id,
        can_create_orders=can_create_orders,
        can_create_checkout_links=True,
        can_send_payment_links=True,
        can_apply_coupons=True,
        can_auto_generate_coupons=False,
        can_cancel_orders=False,
    )
    db.add(row)
    db.commit()


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


class TestTenantRolloutParsing:
    def test_parse_tenant_ids(self) -> None:
        assert parse_order_flow_v2_tenant_ids("10, 20,30") == frozenset({10, 20, 30})
        assert parse_order_flow_v2_tenant_ids("") == frozenset()
        assert parse_order_flow_v2_tenant_ids(None) == frozenset()


class TestEnforcementAllowlist:
    def test_disabled_tenant_list_blocks_all_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_DISABLED_TENANTS", "7")
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "7")
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        decision = resolve_order_flow_v2_operational(
            MagicMock(),
            tenant_id=7,
            customer_phone="966500000001",
            conversation=_conversation(),
        )
        assert decision.live is False
        assert decision.shadow_log is False
        assert decision.reason == "tenant_disabled_allowlist"

    def test_enforce_allowlisted_live_when_global_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "101")
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=""),
        ):
            with patch("core.billing.has_billing_access", return_value=True):
                decision = resolve_order_flow_v2_operational(
                    MagicMock(),
                    tenant_id=101,
                    customer_phone="966500000001",
                    conversation=_conversation(),
                )
        assert decision.live is True
        assert decision.reason == "tenant_enforce_allowlist"

    def test_enforce_non_allowlisted_not_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "101")
        decision = resolve_order_flow_v2_operational(
            MagicMock(),
            tenant_id=202,
            customer_phone="966500000001",
            conversation=_conversation(),
        )
        assert decision.live is False
        assert decision.shadow_log is True
        assert decision.reason == "shadow_only"

    def test_tenant_isolation_enforce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "10")
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=""),
        ):
            with patch("core.billing.has_billing_access", return_value=True):
                live_a = resolve_order_flow_v2_operational(
                    MagicMock(), tenant_id=10, customer_phone="966500000001", conversation=_conversation()
                )
                live_b = resolve_order_flow_v2_operational(
                    MagicMock(), tenant_id=11, customer_phone="966500000001", conversation=_conversation()
                )
        assert live_a.live is True
        assert live_b.live is False

    def test_global_enabled_when_enforce_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ORDER_FLOW_V2_ENFORCE_TENANTS", raising=False)
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=""),
        ):
            decision = resolve_order_flow_v2_operational(
                MagicMock(),
                tenant_id=1,
                customer_phone="966500000001",
                conversation=_conversation(),
            )
        assert decision.live is True
        assert decision.reason == "global_enabled"

    def test_rollback_remove_from_enforce_returns_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "55")
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=""),
        ):
            with patch("core.billing.has_billing_access", return_value=True):
                decision_listed = resolve_order_flow_v2_operational(
                    MagicMock(), tenant_id=55, customer_phone="966500000001", conversation=_conversation()
                )
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "")
        decision_removed = resolve_order_flow_v2_operational(
            MagicMock(), tenant_id=55, customer_phone="966500000001", conversation=_conversation()
        )
        assert decision_listed.live is True
        assert decision_removed.live is False
        assert decision_removed.shadow_log is True

    def test_p0a_ai_paused_blocks_live_enforce(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "88")
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=True, reason=REASON_AI_PAUSED),
        ):
            decision = resolve_order_flow_v2_operational(
                MagicMock(),
                tenant_id=88,
                customer_phone="966500000001",
                conversation=_conversation(ai_paused=True),
            )
        assert decision.live is False
        assert decision.shadow_log is True
        assert decision.reason == REASON_AI_PAUSED

    def test_p0a_human_status_blocks_live_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_ENABLED", True, raising=False)
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=True, reason="human_supervision"),
        ):
            decision = resolve_order_flow_v2_operational(
                MagicMock(),
                tenant_id=1,
                customer_phone="966500000001",
                conversation=_conversation(status="human"),
            )
        assert decision.live is False
        assert decision.reason == "human_supervision"


class TestOwnerShadowNoWrite:
    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(False, True, "shadow_only"))
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_shadow_empty_patch_no_persist(self, load_state, _op) -> None:
        prep = {
            "local_draft_authoritative": True,
            "line_items": [dict(_GENERIC_ITEM)],
            "order_flow_v2_active": True,
        }
        load_state.return_value = (_conversation(), {"order_prep": prep})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="الرياض",
        )
        assert result.handled is False
        assert result.shadow_only is True
        assert result.state_patch == {}
        assert result.operational_reason == "shadow_only"

    def test_persist_guard_shadow_and_unhandled(self) -> None:
        db = MagicMock()
        with patch("modules.ai.order_flow_v2.owner.apply_state_patch") as apply_patch:
            with patch("modules.ai.order_flow_v2.owner._sync_draft_order") as sync_draft:
                persist_order_flow_v2_result(
                    db,
                    tenant_id=1,
                    customer_phone="966500000001",
                    result=OrderFlowV2Result(
                        handled=False,
                        shadow_only=True,
                        reason="shadow_only",
                        state_patch={"line_items": [_GENERIC_ITEM]},
                    ),
                )
        apply_patch.assert_not_called()
        sync_draft.assert_not_called()


class TestCommercePermissionsGate:
    def test_create_orders_denied_falls_through(self, db, monkeypatch: pytest.MonkeyPatch) -> None:
        tenant = seed_tenant(db, name="متجر تجريبي عام")
        _seed_permissions(db, tenant.id, can_create_orders=False)
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", str(tenant.id))
        prep = {
            "line_items": [dict(_PERFUME_ITEM)],
            "order_flow_v2_active": True,
            "local_draft_authoritative": True,
        }
        conv = _conversation(conv_id=99)
        conv.tenant_id = tenant.id
        with patch("modules.ai.order_flow_v2.owner._load_brain_state", return_value=(conv, {"order_prep": prep})):
            with patch(
                "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
                return_value=SimpleNamespace(disabled=False, reason=""),
            ):
                with patch("core.billing.has_billing_access", return_value=True):
                    result = try_handle_order_flow_v2(
                        db,
                        tenant_id=tenant.id,
                        customer_phone="966500000001",
                        message="الرياض",
                    )
        assert result.handled is False
        assert result.reason == "commerce_permission_denied:create_orders"
        assert result.skip_brain is False

    def test_finalize_sets_operational_and_permission_telemetry(self) -> None:
        from modules.ai.commerce.permissions import CommercePermissionSet
        from modules.ai.commerce.permission_loader import PermissionLoadResult
        from modules.ai.order_flow_v2.owner import _finalize_result

        perm = PermissionLoadResult(
            permissions=CommercePermissionSet(tenant_id=1),
            source="db_row",
        )
        result = _finalize_result(
            live=True,
            shadow_log=False,
            reply="تمام",
            reason="collect_next_field",
            state_patch={},
            tenant_id=1,
            operational_reason="tenant_enforce_allowlist",
            perm_load=perm,
        )
        assert result.handled is True
        assert result.operational_reason == "tenant_enforce_allowlist"
        assert result.permission_source == "db_row"


class TestDisabledAndFallback:
    def test_both_flags_off_does_not_take_over(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_SHADOW_ENABLED", "false")
        monkeypatch.setattr("core.config.ORDER_FLOW_V2_SHADOW_ENABLED", False, raising=False)
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="مرحبا",
        )
        assert result.handled is False

    @patch("modules.ai.order_flow_v2.owner.operational_tuple", return_value=(False, True, "shadow_only"))
    @patch("modules.ai.order_flow_v2.owner._load_brain_state")
    def test_fallback_to_merchant_brain_skip_brain_false(self, load_state, _op) -> None:
        load_state.return_value = (_conversation(), {"order_prep": {}})
        result = try_handle_order_flow_v2(
            MagicMock(),
            tenant_id=1,
            customer_phone="966500000001",
            message="وش الأنواع المتوفرة؟",
        )
        assert result.handled is False
        assert result.skip_brain is False

    def test_telemetry_reasons_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ORDER_FLOW_V2_ENFORCE_TENANTS", "5")
        with patch(
            "modules.ai.order_flow_v2.enforcement.is_ai_disabled_for_conversation",
            return_value=SimpleNamespace(disabled=False, reason=""),
        ):
            with patch("core.billing.has_billing_access", return_value=True):
                decision = resolve_order_flow_v2_operational(
                    MagicMock(),
                    tenant_id=5,
                    customer_phone="966500000001",
                    conversation=_conversation(),
                )
        assert decision.reason == "tenant_enforce_allowlist"


class TestCanaryPreserved:
    def test_test_mode_canary_still_live(self) -> None:
        with patch("modules.ai.order_flow_v2.enforcement.is_ai_allowed_by_store_mode") as mode:
            from core.ai_disabled_gate import StoreAIModeDecision  # noqa: PLC0415
            from core.tenant import STORE_AI_MODE_TEST  # noqa: PLC0415

            mode.return_value = StoreAIModeDecision(allowed=True, mode=STORE_AI_MODE_TEST)
            with patch("core.billing.has_billing_access", return_value=True):
                decision = resolve_order_flow_v2_operational(
                    MagicMock(),
                    tenant_id=1,
                    customer_phone="966500000001",
                    conversation=_conversation(),
                )
        assert decision.live is True
        assert decision.reason == "test_mode_canary_enforcement"
