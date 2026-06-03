"""Regression tests for GET /api/salla/session (salla_check_session)."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from starlette.datastructures import QueryParams

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models import Integration, Tenant  # noqa: E402


def test_salla_check_session_does_not_raise_name_error() -> None:
    """require_authenticated must be imported inside salla_check_session."""
    from routers.salla_oauth import salla_check_session

    async def _run():
        request = MagicMock()
        request.query_params = QueryParams("store_id=22825873")
        request.state = MagicMock()
        request.state.jwt_payload = {
            "tenant_id": 47,
            "user_id": 16,
            "sub": "store-22825873@salla-merchant.nahlah.ai",
            "role": "merchant",
        }

        integration = MagicMock()
        integration.id = 3
        integration.config = {
            "api_key": "embedded-token",
            "refresh_token": "refresh",
            "api_key_source": "embedded_token",
        }

        tenant = MagicMock()
        tenant.id = 47

        def _query(model):
            chain = MagicMock()
            chain.filter.return_value = chain
            chain.limit.return_value = chain
            if model is Integration:
                chain.first.return_value = integration
            elif model is Tenant:
                chain.first.return_value = tenant
            else:
                chain.first.return_value = None
            return chain

        db = MagicMock()
        db.query.side_effect = _query

        with patch("routers.salla_oauth.create_token", return_value="fresh-jwt"):
            return await salla_check_session(request, db)

    result = asyncio.run(_run())

    assert result["connected"] is True
    assert result["tenant_id"] == 47
    assert result["token"] == "fresh-jwt"
    assert result["integration"]["id"] == 3


def test_salla_check_session_401_without_jwt() -> None:
    from fastapi import HTTPException

    from routers.salla_oauth import salla_check_session

    async def _run():
        request = MagicMock()
        request.query_params = QueryParams("")
        request.state = MagicMock()
        request.state.jwt_payload = None

        with pytest.raises(HTTPException) as exc_info:
            await salla_check_session(request, MagicMock())

        assert exc_info.value.status_code == 401

    asyncio.run(_run())
