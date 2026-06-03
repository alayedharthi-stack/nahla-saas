"""Regression: Salla JWT public prefixes must not expose protected /salla/* routes."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from core.middleware import (
    JWT_PUBLIC_EXACT_PATHS,
    JWT_PUBLIC_PREFIXES,
    is_jwt_public_path,
    jwt_enforcement_middleware,
)

_SALLA_PUBLIC_PREFIXES = (
    "/salla/token-login",
    "/salla/start",
    "/salla/session/launch-dashboard",
    "/salla/session/resolve-launch",
    "/salla/app-settings/webhook",
)

_SALLA_PUBLIC_EXACT = ("/salla/app",)


def test_salla_bare_prefix_is_not_jwt_public() -> None:
    assert "/salla" not in JWT_PUBLIC_PREFIXES


def test_salla_token_login_remains_public() -> None:
    assert is_jwt_public_path("/salla/token-login")
    assert is_jwt_public_path("/salla/token-login/")


def test_salla_app_html_landing_is_public_exact_only() -> None:
    assert is_jwt_public_path("/salla/app")
    assert not is_jwt_public_path("/salla/app-settings")


def test_salla_subscription_status_is_not_public() -> None:
    assert not is_jwt_public_path("/salla/subscription/status")


def test_salla_app_settings_get_is_not_public() -> None:
    assert not is_jwt_public_path("/salla/app-settings")


def test_salla_app_settings_webhook_remains_public() -> None:
    assert is_jwt_public_path("/salla/app-settings/webhook")


@pytest.mark.parametrize("prefix", _SALLA_PUBLIC_PREFIXES)
def test_documented_salla_public_prefixes_registered(prefix: str) -> None:
    assert prefix in JWT_PUBLIC_PREFIXES


@pytest.mark.parametrize("exact", _SALLA_PUBLIC_EXACT)
def test_documented_salla_public_exact_paths_registered(exact: str) -> None:
    assert exact in JWT_PUBLIC_EXACT_PATHS


def test_jwt_middleware_passes_token_login_without_bearer() -> None:
    async def _run() -> None:
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/salla/token-login",
                "headers": [],
                "query_string": b"",
            }
        )
        downstream = AsyncMock(return_value=MagicMock(status_code=200))
        response = await jwt_enforcement_middleware(request, downstream)
        downstream.assert_awaited_once()
        assert response.status_code == 200

    asyncio.run(_run())


@pytest.mark.parametrize(
    "path",
    [
        "/salla/subscription/status",
        "/salla/app-settings",
    ],
)
def test_jwt_middleware_blocks_protected_salla_routes_without_token(path: str) -> None:
    async def _run() -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [],
                "query_string": b"",
            }
        )
        downstream = AsyncMock()
        response = await jwt_enforcement_middleware(request, downstream)
        downstream.assert_not_awaited()
        assert response.status_code == 401

    asyncio.run(_run())


def test_jwt_middleware_sets_jwt_payload_on_subscription_status() -> None:
    payload = {
        "tenant_id": 47,
        "user_id": 16,
        "sub": "store-22825873@salla-merchant.nahlah.ai",
        "role": "merchant",
    }

    async def _run() -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/salla/subscription/status",
                "headers": [(b"authorization", b"Bearer test-jwt")],
                "query_string": b"",
            }
        )
        captured: dict = {}

        async def downstream(req: Request):
            captured["jwt_payload"] = getattr(req.state, "jwt_payload", None)
            return MagicMock(status_code=200)

        with patch("core.middleware.decode_token", return_value=payload):
            response = await jwt_enforcement_middleware(request, downstream)

        assert response.status_code == 200
        assert captured["jwt_payload"] == payload

    asyncio.run(_run())


def test_jwt_middleware_sets_jwt_payload_on_app_settings() -> None:
    payload = {"tenant_id": 47, "sub": "m@example.com", "role": "merchant", "user_id": 1}

    async def _run() -> None:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/salla/app-settings",
                "headers": [(b"authorization", b"Bearer test-jwt")],
                "query_string": b"",
            }
        )
        captured: dict = {}

        async def downstream(req: Request):
            captured["jwt_payload"] = getattr(req.state, "jwt_payload", None)
            return MagicMock(status_code=200)

        with patch("core.middleware.decode_token", return_value=payload):
            response = await jwt_enforcement_middleware(request, downstream)

        assert response.status_code == 200
        assert captured["jwt_payload"]["tenant_id"] == 47

    asyncio.run(_run())
