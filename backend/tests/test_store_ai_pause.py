"""
tests/test_store_ai_pause.py
────────────────────────────
Store-wide AI pause — independent of per-conversation ai_paused flags.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in [_BACKEND, os.path.join(_BACKEND, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ai_pause_guard import REASON_MANUAL_PAUSE
from core.ai_disabled_gate import (
    REASON_STORE_AI_DISABLED,
    StoreAIModeDecision,
    evaluate_ai_disabled_send_block,
    is_ai_disabled_for_conversation,
    is_store_ai_enabled,
)
from core.tenant import STORE_AI_MODE_OFF, STORE_AI_MODE_ON


def _run(coro):
    return asyncio.run(coro)


def _convo(**kwargs):
    defaults = dict(
        id=42,
        tenant_id=33,
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
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _quota_ok():
    return SimpleNamespace(allowed=True, used_total=0, limit=1000, reason="", pct=0)


def _settings(ai_settings: dict | None):
    return SimpleNamespace(ai_settings=ai_settings)


def _store_mode_off() -> StoreAIModeDecision:
    return StoreAIModeDecision(allowed=False, reason=REASON_STORE_AI_DISABLED, mode=STORE_AI_MODE_OFF)


def _store_mode_on() -> StoreAIModeDecision:
    return StoreAIModeDecision(allowed=True, mode=STORE_AI_MODE_ON)


class TestStoreAIEnabledHelper:
    def test_defaults_to_true_when_missing(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(None),
        ):
            assert is_store_ai_enabled(db, 33) is True

    def test_respects_false_in_ai_settings(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings({"store_ai_enabled": False}),
        ):
            assert is_store_ai_enabled(db, 33) is False


class TestStoreAIDisabledGate:
    def test_store_off_disables_even_when_conversation_active(self) -> None:
        convo = _convo(ai_paused=False)
        db = MagicMock()

        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_off(),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )

        assert decision.disabled is True
        assert decision.reason == REASON_STORE_AI_DISABLED

    def test_store_on_respects_individual_pause(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = MagicMock()

        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_on(),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )

        assert decision.disabled is True
        assert decision.reason == REASON_MANUAL_PAUSE

    def test_store_off_then_on_preserves_individual_pause(self) -> None:
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_MANUAL_PAUSE)
        db = MagicMock()

        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            side_effect=[_store_mode_off(), _store_mode_on()],
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            off = is_ai_disabled_for_conversation(
                db, tenant_id=33, customer_phone="966551459303",
            )
            on = is_ai_disabled_for_conversation(
                db, tenant_id=33, customer_phone="966551459303",
            )

        assert off.reason == REASON_STORE_AI_DISABLED
        assert on.reason == REASON_MANUAL_PAUSE
        assert convo.ai_paused is True
        assert convo.ai_paused_reason == REASON_MANUAL_PAUSE


class TestMerchantWebhookStorePause:
    def _call_handle_merchant_message(self, *, store_ai_enabled: bool, text: str):
        from routers.whatsapp_webhook import _handle_merchant_message

        convo = _convo(ai_paused=False)
        db = MagicMock()
        db.commit = MagicMock()
        db.rollback = MagicMock()
        db.add = MagicMock()
        db.flush = MagicMock()

        posted: list = []

        async def fake_post(*_args, **kwargs):
            posted.append(kwargs.get("json"))
            return {"messages": [{"id": "wamid.X"}]}

        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_off() if not store_ai_enabled else _store_mode_on(),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "routers.conversations._get_or_create_conversation",
            return_value=convo,
        ), patch(
            "core.conversation_engine.StateManager.save_message",
        ) as mock_save, patch(
            "services.whatsapp_platform.service.provider_post_with_context",
            new=fake_post,
        ), patch(
            "services.whatsapp_platform.service.get_token_for_operation",
            new=AsyncMock(return_value=MagicMock(token="tok", source="test")),
        ), patch(
            "modules.ai.brain.pipeline.get_brain",
        ) as mock_brain:
            mock_brain.return_value.process = AsyncMock(
                return_value={"reply": "should not send", "buttons": []},
            )
            _run(_handle_merchant_message(
                phone_id="PH1",
                to="966551459303",
                text=text,
                tenant_id=33,
                db=db,
            ))

        return posted, mock_save, mock_brain

    def test_store_off_persists_inbound_without_llm_or_outbound(self) -> None:
        posted, mock_save, mock_brain = self._call_handle_merchant_message(
            store_ai_enabled=False,
            text="مرحبا",
        )
        assert posted == []
        mock_save.assert_called()
        mock_brain.return_value.process.assert_not_called()

    def test_store_on_does_not_hit_store_disabled_gate(self) -> None:
        db = MagicMock()
        convo = _convo(ai_paused=False)

        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_on(),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
            )

        assert decision.disabled is False


class TestBrainProcessStorePause:
    def test_brain_process_skips_when_store_off(self) -> None:
        from modules.ai.brain.pipeline import MerchantBrain

        convo = _convo(ai_paused=False)
        db = MagicMock()

        brain = MerchantBrain(
            classifier=MagicMock(),
            state_store=MagicMock(),
            facts_loader=MagicMock(),
            decision_engine=MagicMock(),
            policy_gate=MagicMock(),
            executor=MagicMock(),
            composer=MagicMock(),
            memory_updater=MagicMock(),
        )

        with patch("core.billing.has_billing_access", return_value=True), patch(
            "core.wa_usage.check_limit",
            return_value=_quota_ok(),
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_off(),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            result = _run(brain.process(
                db=db,
                tenant_id=33,
                customer_phone="966551459303",
                message="test",
                history=[],
                profile={},
            ))

        assert result.get("skipped") is True
        assert result.get("reason") == "ai_disabled_gate"
        assert result.get("ai_disabled_reason") == REASON_STORE_AI_DISABLED
        brain._classifier.classify.assert_not_called()


class TestSubscriptionIndependent:
    def test_billing_still_blocks_when_store_on(self) -> None:
        from modules.ai.brain.pipeline import MerchantBrain

        convo = _convo(ai_paused=False)
        db = MagicMock()

        brain = MerchantBrain(
            classifier=MagicMock(),
            state_store=MagicMock(),
            facts_loader=MagicMock(),
            decision_engine=MagicMock(),
            policy_gate=MagicMock(),
            executor=MagicMock(),
            composer=MagicMock(),
            memory_updater=MagicMock(),
        )

        with patch("core.billing.has_billing_access", return_value=False), patch(
            "core.wa_usage.check_limit",
            return_value=_quota_ok(),
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_on(),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            result = _run(brain.process(
                db=db,
                tenant_id=33,
                customer_phone="966551459303",
                message="test",
                history=[],
                profile={},
            ))

        assert result.get("skipped") is True
        assert result.get("reason") == "billing_access_denied"


class TestManualSendBypass:
    def test_manual_dashboard_send_not_blocked_by_store_pause(self) -> None:
        db = MagicMock()
        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_off(),
        ):
            blocked, decision = evaluate_ai_disabled_send_block(
                db,
                tenant_id=33,
                customer_phone="966551459303",
                blocked_path="dashboard_manual",
                allow_manual=True,
            )

        assert blocked is False
        assert decision.disabled is False


class TestAutomationGuardStorePause:
    def test_automation_blocked_when_store_off_even_without_conversation(self) -> None:
        from core.automation_send_guard import (
            REASON_STORE_AI_DISABLED,
            should_block_automation_for_conversation,
        )

        db = MagicMock()
        with patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=_store_mode_off(),
        ):
            decision = should_block_automation_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966551459303",
                conversation=None,
            )

        assert decision.block is True
        assert decision.reason == REASON_STORE_AI_DISABLED


class TestPutSettingsPreservesStoreAI:
    def test_put_ai_personality_does_not_clear_store_ai_enabled(self) -> None:
        from routers.settings import AISettingsIn, update_settings, AllSettingsIn

        settings = SimpleNamespace(
            ai_settings={"store_ai_enabled": False, "assistant_name": "نحلة"},
            whatsapp_settings=None,
            store_settings=None,
            notification_settings=None,
            extra_metadata=None,
            updated_at=None,
        )
        db = MagicMock()
        db.commit = MagicMock()
        db.refresh = MagicMock()
        request = MagicMock()

        body = AllSettingsIn(
            ai=AISettingsIn(assistant_name="مساعد جديد"),
        )

        with patch("routers.settings.resolve_tenant_id", return_value=33), patch(
            "routers.settings.get_or_create_settings",
            return_value=settings,
        ), patch(
            "routers.settings.require_not_support_impersonation",
            return_value={},
        ), patch(
            "core.merchant_payment_methods.load_merchant_payment_methods",
        ) as mock_pm:
            mock_pm.return_value.to_dict.return_value = {}
            _run(update_settings(
                body=body,
                request=request,
                db=db,
                _no_support={},
            ))

        assert settings.ai_settings["store_ai_enabled"] is False
        assert settings.ai_settings["assistant_name"] == "مساعد جديد"


class TestPatchEndpointDoesNotBulkUpdateConversations:
    def test_patch_only_updates_ai_settings(self) -> None:
        from routers.settings import patch_store_ai_settings, StoreAISettingsPatch

        settings = SimpleNamespace(
            ai_settings={"assistant_name": "نحلة", "store_ai_enabled": True, "coupon_cap_hours": 48},
            updated_at=None,
        )
        db = MagicMock()
        db.commit = MagicMock()
        db.refresh = MagicMock()

        request = MagicMock()
        with patch(
            "routers.settings.resolve_tenant_id",
            return_value=33,
        ), patch(
            "routers.settings.get_or_create_settings",
            return_value=settings,
        ), patch(
            "routers.settings.require_not_support_impersonation",
            return_value={},
        ), patch(
            "sqlalchemy.orm.attributes.flag_modified",
        ):
            result = _run(patch_store_ai_settings(
                body=StoreAISettingsPatch(store_ai_enabled=False),
                request=request,
                db=db,
                _no_support={},
            ))

        assert result["store_ai_enabled"] is False
        assert result["store_ai_mode"] == "off"
        assert settings.ai_settings["store_ai_enabled"] is False
        assert settings.ai_settings["coupon_cap_hours"] == 48
        db.query.assert_not_called()
