"""Fail-closed Tenant 1 lifecycle canary isolation — no production send."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.automation_engine import (  # noqa: E402
    _execute_action,
    _execute_ai_recovery_step,
    send_lifecycle_whatsapp_session_body,
    send_lifecycle_whatsapp_template,
)
from core.commerce_lifecycle.canary_guard import (  # noqa: E402
    MODE_LEGACY_LIFECYCLE,
    MODE_NEW_LIFECYCLE,
    evaluate_and_audit,
    evaluate_lifecycle_canary_send,
)
from services.cod_confirmation import send_cod_confirmation_template  # noqa: E402

ALLOWED_PHONE = "+966500111222"
OTHER_PHONE = "+966500999888"
CANARY_TENANT = 1
OTHER_TENANT = 33


def _enable_canary(monkeypatch, *, tenants="1", recipients=ALLOWED_PHONE):
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_TENANT_ALLOWLIST", tenants)
    monkeypatch.setenv("COMMERCE_LIFECYCLE_DISPATCH_RECIPIENT_ALLOWLIST", recipients)


def _eval(*, tenant_id, phone, mode, sender_path="test", automation_type=None):
    return evaluate_lifecycle_canary_send(
        tenant_id,
        phone=phone,
        sender_path=sender_path,
        mode=mode,
        automation_type=automation_type,
    )


class TestCentralGuardContract:
    def test_allowed_tenant_and_recipient_permitted(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=CANARY_TENANT,
            phone=ALLOWED_PHONE,
            mode=MODE_NEW_LIFECYCLE,
            sender_path="lifecycle_dispatch",
        )
        assert decision.allowed is True
        assert decision.reason == "permitted"

    def test_legacy_allowed_recipient_permitted(self, monkeypatch):
        _enable_canary(monkeypatch)
        for atype in (
            "cod_confirmation",
            "unpaid_order_reminder",
            "abandoned_cart",
        ):
            decision = _eval(
                tenant_id=CANARY_TENANT,
                phone=ALLOWED_PHONE,
                mode=MODE_LEGACY_LIFECYCLE,
                sender_path="automation_engine",
                automation_type=atype,
            )
            assert decision.allowed is True, atype
            assert decision.reason == "permitted"

    def test_canary_tenant_other_recipient_blocked(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=CANARY_TENANT,
            phone=OTHER_PHONE,
            mode=MODE_NEW_LIFECYCLE,
            sender_path="lifecycle_dispatch",
        )
        assert decision.allowed is False
        assert decision.reason == "recipient_not_allowlisted"

    def test_other_tenant_new_lifecycle_blocked(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=OTHER_TENANT,
            phone=ALLOWED_PHONE,
            mode=MODE_NEW_LIFECYCLE,
            sender_path="lifecycle_dispatch",
        )
        assert decision.allowed is False
        assert decision.reason == "tenant_not_allowlisted"

    def test_missing_recipient_blocked(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=CANARY_TENANT,
            phone="",
            mode=MODE_NEW_LIFECYCLE,
            sender_path="lifecycle_dispatch",
        )
        assert decision.allowed is False
        assert decision.reason == "recipient_missing"

    def test_unnormalizable_recipient_blocked(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=CANARY_TENANT,
            phone="not-a-phone",
            mode=MODE_NEW_LIFECYCLE,
            sender_path="lifecycle_dispatch",
        )
        assert decision.allowed is False
        assert decision.reason == "recipient_unnormalizable"

    def test_legacy_other_tenant_unchanged(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=OTHER_TENANT,
            phone=OTHER_PHONE,
            mode=MODE_LEGACY_LIFECYCLE,
            sender_path="automation_engine",
            automation_type="unpaid_order_reminder",
        )
        assert decision.allowed is True
        assert decision.reason == "legacy_not_in_scope"

    def test_legacy_winback_not_gated(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=CANARY_TENANT,
            phone=OTHER_PHONE,
            mode=MODE_LEGACY_LIFECYCLE,
            sender_path="automation_engine",
            automation_type="customer_winback",
        )
        assert decision.allowed is True

    def test_order_notifications_owned_by_dispatch(self, monkeypatch):
        _enable_canary(monkeypatch)
        decision = _eval(
            tenant_id=CANARY_TENANT,
            phone=ALLOWED_PHONE,
            mode=MODE_LEGACY_LIFECYCLE,
            sender_path="automation_engine",
            automation_type="order_notifications",
        )
        assert decision.allowed is False
        assert decision.reason == "lifecycle_dispatch_owns_event"


class TestLegacySenderPathsCannotBypass:
    def test_storesync_has_no_direct_provider_send(self):
        src = (BACKEND_DIR / "services" / "store_sync.py").read_text(encoding="utf-8")
        assert "await provider_send_message" not in src
        assert "dispatch_external_lifecycle_notification" in src
        assert "emit_automation_event" in src

    def test_poller_has_no_direct_provider_send(self):
        src = (BACKEND_DIR / "services" / "salla_orders_poller.py").read_text(encoding="utf-8")
        assert "await provider_send_message" not in src
        assert "emit_automation_event" in src

    def test_emitters_do_not_await_provider_send(self):
        src = (BACKEND_DIR / "core" / "automation_emitters.py").read_text(encoding="utf-8")
        assert "await provider_send_message" not in src
        assert "scan_unpaid_orders" in src
        assert "scan_cod_confirmations" in src
        assert "scan_abandoned_cart_followups" in src

    def test_abandoned_cart_scheduler_does_not_send(self):
        src = (BACKEND_DIR / "core" / "abandoned_cart_scheduler.py").read_text(encoding="utf-8")
        assert "await provider_send_message" not in src
        assert "sync_abandoned_carts" in src

    def test_campaign_dispatcher_not_wired_to_canary_guard(self):
        src = (BACKEND_DIR / "services" / "campaign_dispatcher.py").read_text(encoding="utf-8")
        assert "canary_guard" not in src

    def test_engine_blocks_unpaid_before_whatsapp(self, monkeypatch):
        _enable_canary(monkeypatch)
        monkeypatch.setattr("core.billing.has_billing_access", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "core.wa_usage.check_limit",
            lambda *_a, **_k: SimpleNamespace(
                allowed=True, reason="", used_total=0, limit=99
            ),
        )
        customer = SimpleNamespace(
            id=9, phone=OTHER_PHONE, tenant_id=CANARY_TENANT, name="أحمد سالم"
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = customer
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message",
            send,
        )
        ok, info = asyncio.run(
            _execute_action(
                db,
                CANARY_TENANT,
                SimpleNamespace(id=1, customer_id=9, payload={}),
                SimpleNamespace(id=4, automation_type="unpaid_order_reminder"),
                {},
            )
        )
        assert ok is False
        assert info.get("error_code") == "recipient_not_allowlisted"
        send.assert_not_awaited()

    def test_engine_blocks_abandoned_cart_before_whatsapp(self, monkeypatch):
        _enable_canary(monkeypatch)
        monkeypatch.setattr("core.billing.has_billing_access", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "core.wa_usage.check_limit",
            lambda *_a, **_k: SimpleNamespace(
                allowed=True, reason="", used_total=0, limit=99
            ),
        )
        customer = SimpleNamespace(
            id=9, phone=OTHER_PHONE, tenant_id=CANARY_TENANT, name="نورة عبدالله"
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = customer
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message",
            send,
        )
        ok, info = asyncio.run(
            _execute_action(
                db,
                CANARY_TENANT,
                SimpleNamespace(id=2, customer_id=9, payload={}),
                SimpleNamespace(id=5, automation_type="abandoned_cart"),
                {},
            )
        )
        assert ok is False
        assert info.get("error_code") == "recipient_not_allowlisted"
        send.assert_not_awaited()

    def test_cod_path_cannot_bypass(self, monkeypatch):
        _enable_canary(monkeypatch)
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message",
            send,
        )
        result = asyncio.run(
            send_cod_confirmation_template(
                MagicMock(),
                tenant_id=CANARY_TENANT,
                order=SimpleNamespace(id=77),
                customer_phone=OTHER_PHONE,
                customer_name="أحمد سالم",
                product_name="قميص قطني أزرق",
                total_amount="120",
            )
        )
        assert result["sent"] is False
        assert result["error"] == "recipient_not_allowlisted"
        send.assert_not_awaited()

    def test_cod_allowed_recipient_reaches_lookup_not_blocked_by_guard(self, monkeypatch):
        _enable_canary(monkeypatch)
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        result = asyncio.run(
            send_cod_confirmation_template(
                db,
                tenant_id=CANARY_TENANT,
                order=SimpleNamespace(id=77),
                customer_phone=ALLOWED_PHONE,
                customer_name="أحمد سالم",
                product_name="حذاء رياضي أبيض",
                total_amount="200",
            )
        )
        assert result.get("canary_blocked") is not True
        assert result["sent"] is False
        assert result["error"] == "no_whatsapp_connection"

    def test_lifecycle_template_last_mile_blocks_other_recipient(self, monkeypatch):
        _enable_canary(monkeypatch)
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message",
            send,
        )
        outcome, info = asyncio.run(
            send_lifecycle_whatsapp_template(
                MagicMock(),
                CANARY_TENANT,
                OTHER_PHONE,
                SimpleNamespace(name="order_confirmed_generic_ar"),
                {},
            )
        )
        assert outcome == "failed"
        assert info["error_code"] == "recipient_not_allowlisted"
        send.assert_not_awaited()

    def test_lifecycle_session_last_mile_blocks_other_recipient(self, monkeypatch):
        _enable_canary(monkeypatch)
        send = AsyncMock()
        monkeypatch.setattr(
            "services.whatsapp_platform.service.provider_send_message",
            send,
        )
        outcome, info = asyncio.run(
            send_lifecycle_whatsapp_session_body(
                MagicMock(),
                CANARY_TENANT,
                OTHER_PHONE,
                SimpleNamespace(name="order_confirmed_generic_ar", components=[]),
                {},
            )
        )
        assert outcome == "failed"
        assert info["error_code"] == "recipient_not_allowlisted"
        send.assert_not_awaited()


class TestZeroModelCalls:
    def test_ai_recovery_does_not_call_model_on_canary_tenant(self, monkeypatch):
        _enable_canary(monkeypatch)
        import types

        model = AsyncMock(return_value="should-not-run")
        fake_ai_client = types.ModuleType("services.ai_client")
        fake_ai_client.generate_cart_recovery_text = model
        monkeypatch.setitem(sys.modules, "services.ai_client", fake_ai_client)
        ok, info = asyncio.run(
            _execute_ai_recovery_step(
                MagicMock(),
                tenant_id=CANARY_TENANT,
                event=SimpleNamespace(payload={}),
                customer=SimpleNamespace(name="أحمد سالم"),
                wa_conn=SimpleNamespace(),
                to_phone=ALLOWED_PHONE,
                config={},
                active_step={},
                automation_id=1,
            )
        )
        assert ok is False
        assert info.get("error_code") == "lifecycle_canary_zero_ai"
        model.assert_not_called()
        model.assert_not_awaited()

    def test_canary_guard_source_has_no_model_client(self):
        src = (BACKEND_DIR / "core" / "commerce_lifecycle" / "canary_guard.py").read_text(
            encoding="utf-8"
        )
        assert "generate_cart_recovery_text" not in src
        assert "openai" not in src.lower()
        assert "anthropic" not in src.lower()


class TestAudit:
    def test_blocked_attempt_is_auditable(self, monkeypatch):
        _enable_canary(monkeypatch)
        seen: list[tuple] = []

        def _audit(event, **ctx):
            seen.append((event, ctx))

        monkeypatch.setattr("core.audit.audit", _audit)
        evaluate_and_audit(
            CANARY_TENANT,
            phone=OTHER_PHONE,
            sender_path="automation_engine",
            mode=MODE_LEGACY_LIFECYCLE,
            automation_type="cod_confirmation",
        )
        assert seen
        assert seen[0][0] == "lifecycle_canary_blocked"
        ctx = seen[0][1]
        assert ctx["reason"] == "recipient_not_allowlisted"
        assert "phone_normalized" not in ctx
        assert OTHER_PHONE not in str(ctx)
        assert ALLOWED_PHONE not in str(ctx)
        assert ctx["phone_last4"] == OTHER_PHONE[-4:]
        assert len(ctx["phone_fingerprint"]) == 64
        assert OTHER_PHONE not in ctx["phone_fingerprint"]
        assert ctx["phone_fingerprint"] != OTHER_PHONE
