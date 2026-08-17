"""
test_ai_test_mode_allowlist.py
──────────────────────────────
Canary allowlist — store_ai_mode off | test | on.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in [_BACKEND, os.path.join(_BACKEND, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ai_disabled_gate import (  # noqa: E402
    REASON_AI_PAUSED,
    REASON_STORE_AI_DISABLED,
    REASON_STORE_AI_TEST_MODE_NOT_ALLOWED,
    evaluate_ai_disabled_send_block,
    is_ai_allowed_by_store_mode,
    is_ai_disabled_for_conversation,
    is_store_ai_enabled,
)
from core.automation_send_guard import (  # noqa: E402
    REASON_STORE_AI_TEST_MODE_NOT_ALLOWED as AUTO_TEST_BLOCKED,
    evaluate_automation_send,
)
from core.tenant import (  # noqa: E402
    STORE_AI_MODE_OFF,
    STORE_AI_MODE_ON,
    STORE_AI_MODE_TEST,
    merge_ai_defaults,
    resolve_store_ai_mode,
    sync_store_ai_enabled_from_mode,
)
from routers.settings import StoreAISettingsPatch, patch_store_ai_settings  # noqa: E402
from services.ai_playground_dry_run import run_playground_dry_run  # noqa: E402
from utils.phone_utils import (  # noqa: E402
    normalize_whatsapp_phone_for_ai_allowlist,
    phone_matches_ai_test_allowlist,
)


ALLOWED = "966500000001"
OTHER = "966500000099"


def _settings(ai_settings: dict | None):
    return SimpleNamespace(ai_settings=ai_settings)


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
        status="active",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ai(**overrides):
    base = {
        "store_ai_mode": STORE_AI_MODE_ON,
        "store_ai_enabled": True,
        "ai_test_allowed_numbers": [ALLOWED],
    }
    base.update(overrides)
    return base


class TestResolveStoreAIMode:
    def test_legacy_false_means_off(self) -> None:
        assert resolve_store_ai_mode({"store_ai_enabled": False}) == STORE_AI_MODE_OFF

    def test_legacy_missing_means_on(self) -> None:
        assert resolve_store_ai_mode({}) == STORE_AI_MODE_ON
        assert resolve_store_ai_mode({"store_ai_enabled": True}) == STORE_AI_MODE_ON

    def test_explicit_mode_wins(self) -> None:
        assert resolve_store_ai_mode({"store_ai_mode": "test", "store_ai_enabled": True}) == "test"

    def test_sync_boolean_from_mode(self) -> None:
        assert sync_store_ai_enabled_from_mode("on") is True
        assert sync_store_ai_enabled_from_mode("off") is False
        assert sync_store_ai_enabled_from_mode("test") is False


class TestStoreModeGate:
    def test_off_blocks_everyone(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_OFF, store_ai_enabled=False)),
        ):
            assert is_store_ai_enabled(db, 1) is False
            allowed = is_ai_allowed_by_store_mode(db, 1, ALLOWED)
            assert allowed.allowed is False
            assert allowed.reason == REASON_STORE_AI_DISABLED

    def test_test_allows_listed_number(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ):
            allowed = is_ai_allowed_by_store_mode(db, 1, ALLOWED)
            assert allowed.allowed is True
            assert allowed.mode == STORE_AI_MODE_TEST

    def test_test_blocks_unlisted_number(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ):
            allowed = is_ai_allowed_by_store_mode(db, 1, OTHER)
            assert allowed.allowed is False
            assert allowed.reason == REASON_STORE_AI_TEST_MODE_NOT_ALLOWED

    def test_on_allows_any_phone(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_ON)),
        ):
            assert is_ai_allowed_by_store_mode(db, 1, OTHER).allowed is True


class TestConversationGuardsStillApply:
    def test_listed_sender_still_blocked_when_paused(self) -> None:
        db = MagicMock()
        convo = _convo(ai_paused=True, ai_paused_reason=REASON_AI_PAUSED)
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=33, customer_phone=ALLOWED,
            )
            assert decision.disabled is True
            assert decision.reason == REASON_AI_PAUSED

    def test_listed_sender_still_blocked_when_genuine_takeover(self) -> None:
        db = MagicMock()
        from datetime import datetime, timezone  # noqa: PLC0415

        convo = _convo(
            paused_by_human=True,
            taken_over_at=datetime.now(timezone.utc),
            status="human",
        )
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=33, customer_phone=ALLOWED,
            )
            assert decision.disabled is True

    def test_listed_sender_not_blocked_by_advisory_handoff_flags(self) -> None:
        db = MagicMock()
        convo = _convo(handoff_active=True, is_human_handoff=True, needs_human=True)
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=33, customer_phone=ALLOWED,
            )
            assert decision.disabled is False

    def test_unlisted_sender_blocked_before_pause_check(self) -> None:
        db = MagicMock()
        convo = _convo(ai_paused=False)
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ):
            decision = is_ai_disabled_for_conversation(
                db, tenant_id=33, customer_phone=OTHER,
            )
            assert decision.disabled is True
            assert decision.reason == REASON_STORE_AI_TEST_MODE_NOT_ALLOWED


class TestManualReplyBypass:
    def test_manual_send_not_blocked_in_test_mode(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ):
            blocked, _ = evaluate_ai_disabled_send_block(
                db,
                tenant_id=33,
                customer_phone=OTHER,
                allow_manual=True,
            )
            assert blocked is False


class TestAutomationGuard:
    def test_test_mode_blocks_unlisted_automation(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ):
            decision = evaluate_automation_send(
                db,
                tenant_id=33,
                customer_phone=OTHER,
                blocked_path="review_request",
            )
            assert decision.block is True
            assert decision.reason == AUTO_TEST_BLOCKED

    def test_test_mode_allows_listed_then_respects_pause(self) -> None:
        db = MagicMock()
        convo = _convo(ai_paused=True)
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "core.automation_send_guard.lookup_conversation_for_phone",
            return_value=convo,
        ):
            decision = evaluate_automation_send(
                db,
                tenant_id=33,
                customer_phone=ALLOWED,
                conversation=convo,
                blocked_path="review_request",
            )
            assert decision.block is True
            assert decision.reason == "ai_disabled"


class TestPhoneNormalization:
    @pytest.mark.parametrize(
        "left,right",
        [
            ("+966500000001", "966500000001"),
            ("966500000001", "+966500000001"),
            ("0500000001", "966500000001"),
        ],
    )
    def test_allowlist_variants_match(self, left: str, right: str) -> None:
        allowlist = [normalize_whatsapp_phone_for_ai_allowlist(left)]
        assert phone_matches_ai_test_allowlist(right, allowlist) is True


class TestSettingsPatch:
    def test_patch_mode_test_syncs_boolean_and_numbers(self) -> None:
        db = MagicMock()
        settings = SimpleNamespace(
            ai_settings={"assistant_name": "نحلة", "store_ai_enabled": True},
            updated_at=None,
        )

        db.refresh = lambda obj: obj
        db.commit = MagicMock()

        with patch("routers.settings.resolve_tenant_id", return_value=33), patch(
            "routers.settings.get_or_create_settings",
            return_value=settings,
        ), patch(
            "sqlalchemy.orm.attributes.flag_modified",
        ), patch(
            "core.tenant_config_hygiene.apply_tenant_settings_hygiene",
        ):
            result = asyncio.run(patch_store_ai_settings(
                body=StoreAISettingsPatch(
                    store_ai_mode="test",
                    ai_test_allowed_numbers=["+966500000001", "0500000002"],
                ),
                request=MagicMock(),
                db=db,
                _no_support={},
            ))

        assert result["store_ai_mode"] == "test"
        assert result["store_ai_enabled"] is False
        assert "966500000001" in result["ai_test_allowed_numbers"]
        assert settings.ai_settings["assistant_name"] == "نحلة"

    def test_legacy_boolean_patch_sets_mode(self) -> None:
        db = MagicMock()
        settings = SimpleNamespace(ai_settings={}, updated_at=None)
        db.refresh = lambda obj: obj
        db.commit = MagicMock()

        with patch("routers.settings.resolve_tenant_id", return_value=33), patch(
            "routers.settings.get_or_create_settings",
            return_value=settings,
        ), patch(
            "sqlalchemy.orm.attributes.flag_modified",
        ), patch(
            "core.tenant_config_hygiene.apply_tenant_settings_hygiene",
        ):
            result = asyncio.run(patch_store_ai_settings(
                body=StoreAISettingsPatch(store_ai_enabled=False),
                request=MagicMock(),
                db=db,
                _no_support={},
            ))

        assert result["store_ai_mode"] == "off"
        assert result["store_ai_enabled"] is False


class TestPlaygroundTestPhone:
    def test_test_mode_blocks_without_test_phone(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "services.ai_playground_dry_run.has_billing_access",
            return_value=True,
        ):
            result = run_playground_dry_run(
                db, tenant_id=1, message="مرحبا",
            )
        assert result.blocked_reason == REASON_STORE_AI_TEST_MODE_NOT_ALLOWED

    def test_test_mode_allows_with_matching_test_phone(self) -> None:
        db = MagicMock()
        with patch(
            "core.tenant.get_or_create_settings",
            return_value=_settings(_ai(store_ai_mode=STORE_AI_MODE_TEST, store_ai_enabled=False)),
        ), patch(
            "services.ai_playground_dry_run.has_billing_access",
            return_value=True,
        ), patch(
            "services.ai_playground_dry_run._build_brain_context",
            return_value=MagicMock(message="مرحبا", facts=MagicMock(), commerce_bundle={}),
        ), patch(
            "services.ai_playground_dry_run._resolve_decision",
            return_value=MagicMock(action="noop", args={}),
        ), patch(
            "services.ai_playground_dry_run.build_turn_owner_contract",
            return_value=SimpleNamespace(owner="system"),
        ):
            result = run_playground_dry_run(
                db,
                tenant_id=1,
                message="مرحبا",
                test_phone=ALLOWED,
            )
        assert result.blocked_reason is None


class TestMergeDefaultsBackwardCompat:
    def test_merge_keeps_legacy_off(self) -> None:
        ai = merge_ai_defaults({"store_ai_enabled": False})
        assert resolve_store_ai_mode(ai) == STORE_AI_MODE_OFF
