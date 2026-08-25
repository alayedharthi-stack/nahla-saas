"""Regression tests for Salla embedded Communication App ID trust configuration."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND = os.path.join(_REPO, "backend")
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

NUMERIC_EMBEDDED_APP_ID = "2067202718"
OAUTH_CLIENT_UUID = "f0e12672-3682-4128-8846-000000000001"
TEST_EMBEDDED_APP_ID = "nahla-test-embedded-app"


@pytest.fixture(autouse=True)
def _clear_embedded_env(monkeypatch):
    monkeypatch.delenv("SALLA_EMBEDDED_APP_ID", raising=False)
    monkeypatch.delenv("SALLA_TEST_CLIENT_ID", raising=False)


def test_numeric_embedded_app_id_accepted_from_dedicated_config():
    from services import salla_embedded_app_identity as mod

    with patch.object(mod, "SALLA_EMBEDDED_APP_ID", NUMERIC_EMBEDDED_APP_ID):
        with patch.object(mod, "SALLA_TEST_CLIENT_ID", ""):
            assert mod.resolve_trusted_salla_embedded_app_id(NUMERIC_EMBEDDED_APP_ID) == NUMERIC_EMBEDDED_APP_ID
            assert mod.resolve_trusted_salla_embedded_app_id(None) == NUMERIC_EMBEDDED_APP_ID
            assert mod.is_trusted_salla_embedded_app_id(NUMERIC_EMBEDDED_APP_ID) is True


def test_oauth_client_id_is_not_implicitly_trusted_for_embedded():
    from services import salla_embedded_app_identity as mod

    with patch.object(mod, "SALLA_EMBEDDED_APP_ID", NUMERIC_EMBEDDED_APP_ID):
        with patch.object(mod, "SALLA_TEST_CLIENT_ID", ""):
            assert mod.resolve_trusted_salla_embedded_app_id(OAUTH_CLIENT_UUID) is None
            assert OAUTH_CLIENT_UUID not in mod.trusted_salla_embedded_app_ids()


def test_unknown_request_app_id_rejected_before_introspect():
    from routers.salla_oauth import salla_token_login
    from services import salla_embedded_app_identity as mod

    with patch.object(mod, "SALLA_EMBEDDED_APP_ID", NUMERIC_EMBEDDED_APP_ID):
        with patch.object(mod, "SALLA_TEST_CLIENT_ID", ""):
            request = MagicMock()
            request.json = AsyncMock(
                return_value={"token": "v4.public.test", "app_id": "evil-untrusted-app"}
            )
            request.headers = {}
            request.client = None

            async def _run():
                with pytest.raises(HTTPException) as exc_info:
                    await salla_token_login(request, MagicMock())
                return exc_info.value

            exc = asyncio.run(_run())
            assert exc.status_code == 403
            assert exc.detail["code"] == "invalid_salla_app_id"


def test_missing_trusted_configuration_fails_closed():
    from services import salla_embedded_app_identity as mod

    with patch.object(mod, "SALLA_EMBEDDED_APP_ID", ""):
        with patch.object(mod, "SALLA_TEST_CLIENT_ID", ""):
            assert mod.trusted_salla_embedded_app_ids() == frozenset()
            assert mod.resolve_trusted_salla_embedded_app_id(NUMERIC_EMBEDDED_APP_ID) is None
            assert mod.resolve_trusted_salla_embedded_app_id(None) is None


def test_rejection_emits_structured_log(caplog):
    from services import salla_embedded_app_identity as mod

    with patch.object(mod, "SALLA_EMBEDDED_APP_ID", NUMERIC_EMBEDDED_APP_ID):
        with patch.object(mod, "SALLA_TEST_CLIENT_ID", ""):
            with caplog.at_level(logging.WARNING):
                mod.log_rejected_embedded_app_id(
                    incoming_raw="evil-untrusted-app",
                    request_id="req-test-1",
                    client_ip="203.0.113.10",
                )
    assert any("[SallaEmbeddedAppId] rejected code=invalid_salla_app_id" in r.message for r in caplog.records)
    assert any("trusted_sources=SALLA_EMBEDDED_APP_ID" in r.message for r in caplog.records)
    assert any("request_id=req-test-1" in r.message for r in caplog.records)
    assert not any("evil-untrusted-app" in r.message for r in caplog.records)


def test_legacy_oauth_authorize_still_uses_salla_client_id():
    from routers import salla_oauth

    with patch.object(salla_oauth, "SALLA_CLIENT_ID", OAUTH_CLIENT_UUID):
        with patch.object(salla_oauth, "SALLA_REDIRECT_URI", "https://api.nahlah.ai/oauth/salla/callback"):
            with patch.object(salla_oauth, "resolve_tenant_id", return_value=7):
                result = asyncio.run(salla_oauth.salla_authorize(MagicMock()))
    assert OAUTH_CLIENT_UUID in result["url"]


def test_secondary_test_embedded_app_allowed_when_configured():
    from services import salla_embedded_app_identity as mod

    with patch.object(mod, "SALLA_EMBEDDED_APP_ID", NUMERIC_EMBEDDED_APP_ID):
        with patch.object(mod, "SALLA_TEST_CLIENT_ID", TEST_EMBEDDED_APP_ID):
            assert mod.resolve_trusted_salla_embedded_app_id(TEST_EMBEDDED_APP_ID) == TEST_EMBEDDED_APP_ID
            sources = mod.trusted_salla_embedded_app_id_sources()
            assert "SALLA_EMBEDDED_APP_ID" in sources
            assert "SALLA_TEST_CLIENT_ID" in sources
