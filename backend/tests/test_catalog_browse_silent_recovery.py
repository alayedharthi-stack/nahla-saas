"""P1 — catalog browse must not fall through to empty_reply_fallback on brain silence."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.fallback_policy import EMPTY_REPLY_OPERATIONAL_AR, empty_reply_fallback  # noqa: E402
from modules.ai.brain.commerce.catalog_body_policy import TECHNICAL_CATALOG_BODY  # noqa: E402
from modules.ai.brain.compose import templates as T  # noqa: E402
from modules.ai.brain.postprocess.catalog_browse_silent_recovery import (  # noqa: E402
    RECOVERY_SOURCE,
    is_catalog_browse_silent_recovery_message,
    resolve_catalog_browse_silent_recovery_reply,
    try_catalog_browse_silent_recovery,
)
from modules.ai.brain.postprocess.conversation_recovery import (  # noqa: E402
    try_guard_recovery_reply,
)

GENERIC_MERCHANT = 9001
BROWSE_PHRASES = (
    "وش عندكم منتجات؟",
    "أرسلوا الكتالوج",
    "وش المتوفر",
    "عندكم منتجات؟",
)
NON_COMMERCE_EMPTY = "في البيت"


class TestCatalogBrowseDetection:
    @pytest.mark.parametrize("phrase", BROWSE_PHRASES)
    def test_browse_phrases_detected(self, phrase: str) -> None:
        assert is_catalog_browse_silent_recovery_message(phrase) is True

    def test_non_commerce_not_detected(self) -> None:
        assert is_catalog_browse_silent_recovery_message(NON_COMMERCE_EMPTY) is False

    def test_product_visual_request_is_not_browse_recovery(self) -> None:
        assert is_catalog_browse_silent_recovery_message("وريني صورته") is False
        assert try_catalog_browse_silent_recovery(inbound_text="وريني صورته") is None


class TestCatalogBrowseSilentRecoveryReply:
    def test_with_products_uses_technical_catalog_body(self) -> None:
        reply = resolve_catalog_browse_silent_recovery_reply(has_products=True)
        assert reply == TECHNICAL_CATALOG_BODY
        assert "تعذّرت صياغة" not in reply

    def test_without_products_honest_no_products(self) -> None:
        reply = resolve_catalog_browse_silent_recovery_reply(has_products=False)
        assert reply == T.no_products(variant=0)
        assert "تعذّرت صياغة" not in reply

    def test_unknown_product_state_defaults_to_catalog_direction(self) -> None:
        reply = resolve_catalog_browse_silent_recovery_reply(has_products=None)
        assert reply == TECHNICAL_CATALOG_BODY


class TestTryCatalogBrowseSilentRecovery:
    def test_browse_with_mocked_products(self) -> None:
        mock_db = MagicMock()
        with patch(
            "modules.ai.brain.postprocess.catalog_browse_silent_recovery._tenant_has_catalog_products",
            return_value=True,
        ):
            reply = try_catalog_browse_silent_recovery(
                inbound_text="وش عندكم منتجات؟",
                tenant_id=GENERIC_MERCHANT,
                db=mock_db,
            )
        assert reply == TECHNICAL_CATALOG_BODY

    def test_browse_no_products(self) -> None:
        with patch(
            "modules.ai.brain.postprocess.catalog_browse_silent_recovery._tenant_has_catalog_products",
            return_value=False,
        ):
            reply = try_catalog_browse_silent_recovery(
                inbound_text="أرسلوا الكتالوج",
                tenant_id=GENERIC_MERCHANT,
                db=object(),
            )
        assert reply == T.no_products(variant=0)

    def test_non_browse_returns_none(self) -> None:
        assert try_catalog_browse_silent_recovery(inbound_text=NON_COMMERCE_EMPTY) is None


class TestGuardRecoveryIntegration:
    @pytest.mark.parametrize("phrase", BROWSE_PHRASES)
    def test_browse_phrases_recover_via_guard(self, phrase: str) -> None:
        with patch(
            "modules.ai.brain.postprocess.catalog_browse_silent_recovery._tenant_has_catalog_products",
            return_value=True,
        ):
            rec = try_guard_recovery_reply(
                inbound_text=phrase,
                tenant_id=GENERIC_MERCHANT,
                db=object(),
            )
        assert rec.source == RECOVERY_SOURCE
        assert rec.reply == TECHNICAL_CATALOG_BODY
        assert empty_reply_fallback() not in rec.reply

    def test_tenant1_audit_message_39696_style(self) -> None:
        """Regression for live audit inbound 39696 — must not use technical fallback."""
        with patch(
            "modules.ai.brain.postprocess.catalog_browse_silent_recovery._tenant_has_catalog_products",
            return_value=True,
        ):
            rec = try_guard_recovery_reply(
                inbound_text="وش عندكم منتجات؟",
                tenant_id=1,
                db=object(),
            )
        assert EMPTY_REPLY_OPERATIONAL_AR not in (rec.reply or "")
        assert rec.reply == TECHNICAL_CATALOG_BODY

    def test_non_commerce_empty_still_defers_to_persona_compose(self) -> None:
        rec = try_guard_recovery_reply(inbound_text=NON_COMMERCE_EMPTY)
        assert not rec.reply
        assert rec.needs_persona_compose is True

    def test_no_llm_required_for_browse_recovery(self) -> None:
        with patch(
            "modules.ai.brain.postprocess.catalog_browse_silent_recovery._tenant_has_catalog_products",
            return_value=True,
        ):
            rec = try_guard_recovery_reply(
                inbound_text="وش المتوفر",
                tenant_id=GENERIC_MERCHANT,
                db=object(),
            )
        assert rec.reply
        assert rec.needs_persona_compose is False
