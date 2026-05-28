"""Regression tests for Salla embedded iframe URL defaults + diagnostics."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def test_salla_embedded_url_default_includes_app_salla_path() -> None:
    from core.config import SALLA_EMBEDDED_URL

    assert SALLA_EMBEDDED_URL.rstrip("/").endswith("/app/salla")


def test_embedded_diag_matches_expected_when_configured() -> None:
    from routers.salla_oauth import salla_diag_embedded_config

    async def _run():
        with patch("routers.salla_oauth.SALLA_EMBEDDED_URL", "https://app.nahlah.ai/app/salla"), patch(
            "routers.salla_oauth.DASHBOARD_URL", "https://app.nahlah.ai"
        ), patch("routers.salla_oauth.BACKEND_URL", "https://api.nahlah.ai"):
            return await salla_diag_embedded_config()

    payload = asyncio.run(_run())

    assert payload["embedded_url"] == "https://app.nahlah.ai/app/salla"
    assert payload["expected_embedded_url"] == "https://app.nahlah.ai/app/salla"
    assert payload["matches_expected"] is True
    assert payload["embedded_path_ok"] is True
    assert payload["warning"] is None
    assert payload["dashboard_domain"] == "app.nahlah.ai"
    assert payload["api_domain"] == "api.nahlah.ai"
    assert set(payload["allowed_domains"]) == {"app.nahlah.ai", "api.nahlah.ai"}


def test_embedded_diag_warns_when_path_missing() -> None:
    from routers.salla_oauth import salla_diag_embedded_config

    async def _run():
        with patch("routers.salla_oauth.SALLA_EMBEDDED_URL", "https://app.nahlah.ai"), patch(
            "routers.salla_oauth.DASHBOARD_URL", "https://app.nahlah.ai"
        ), patch("routers.salla_oauth.BACKEND_URL", "https://api.nahlah.ai"):
            return await salla_diag_embedded_config()

    payload = asyncio.run(_run())

    assert payload["matches_expected"] is False
    assert payload["embedded_path_ok"] is False
    assert payload["warning"] is not None
    assert "/app/salla" in payload["warning"]
