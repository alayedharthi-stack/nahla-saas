"""Lifecycle canary ownership — one sender, allowlist, zero AI."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.automation_engine import _execute_ai_recovery_step  # noqa: E402
from core.commerce_lifecycle.dispatch import (  # noqa: E402
    lifecycle_canary_legacy_send_block_reason,
)


def _enable_canary(monkeypatch, *, tenants="20", recipients="+966500111222"):
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", tenants)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", recipients)


class TestLegacyCanaryGate:
    def test_order_notifications_owned_by_lifecycle(self, monkeypatch):
        _enable_canary(monkeypatch)
        reason = lifecycle_canary_legacy_send_block_reason(
            20, "order_notifications", "+966500111222"
        )
        assert reason == "lifecycle_dispatch_owns_event"

    def test_outside_allowlist_blocked(self, monkeypatch):
        _enable_canary(monkeypatch)
        reason = lifecycle_canary_legacy_send_block_reason(
            20, "abandoned_cart", "+966500999888"
        )
        assert reason == "recipient_not_allowlisted"

    def test_allowlisted_cart_may_proceed(self, monkeypatch):
        _enable_canary(monkeypatch)
        reason = lifecycle_canary_legacy_send_block_reason(
            20, "abandoned_cart", "+966500111222"
        )
        assert reason is None

    def test_other_tenant_not_gated(self, monkeypatch):
        _enable_canary(monkeypatch)
        reason = lifecycle_canary_legacy_send_block_reason(
            33, "abandoned_cart", "+966500999888"
        )
        assert reason is None


class TestZeroAiOnCanaryTenant:
    def test_ai_recovery_does_not_call_model(self, monkeypatch):
        _enable_canary(monkeypatch)
        ok, info = asyncio.run(
            _execute_ai_recovery_step(
                MagicMock(),
                tenant_id=20,
                event=SimpleNamespace(payload={}),
                customer=SimpleNamespace(name="أحمد سالم"),
                wa_conn=SimpleNamespace(),
                to_phone="+966500111222",
                config={},
                active_step={},
                automation_id=1,
            )
        )
        assert ok is False
        assert info.get("error_code") == "lifecycle_canary_zero_ai"
