"""Regression tests for GET /api/salla/session (salla_check_session)."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, Integration, Tenant  # noqa: E402

if not getattr(Base.metadata, "_salla_check_session_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_check_session_jsonb_shim = True  # type: ignore[attr-defined]


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def test_salla_check_session_does_not_raise_name_error(db) -> None:
    """require_authenticated must be imported inside salla_check_session."""
    from routers.salla_oauth import salla_check_session

    db.merge(Tenant(id=1, name="Partner"))
    integration = Integration(
        tenant_id=1,
        provider="salla",
        external_store_id="22825873",
        config={
            "store_id": "22825873",
            "api_key": "embedded-token",
            "refresh_token": "refresh",
            "api_key_source": "embedded_token",
        },
        enabled=True,
    )
    db.add(integration)
    db.commit()
    db.refresh(integration)

    async def _run():
        request = MagicMock()
        request.query_params = QueryParams("store_id=22825873")
        request.state = MagicMock()
        request.state.jwt_payload = {
            "tenant_id": 1,
            "user_id": 15,
            "sub": "cgcaqkpx5wgewsyv@email.partners",
            "role": "merchant",
        }

        with patch("routers.salla_oauth.create_token", return_value="fresh-jwt"):
            return await salla_check_session(request, db)

    result = asyncio.run(_run())

    assert result["connected"] is True
    assert result["tenant_id"] == 1
    assert result["store_id"] == "22825873"
    assert result["token"] == "fresh-jwt"
    assert result["integration"]["id"] == integration.id


def test_salla_check_session_401_without_jwt() -> None:
    from routers.salla_oauth import salla_check_session

    async def _run():
        request = MagicMock()
        request.query_params = QueryParams("store_id=22825873")
        request.state = MagicMock()
        request.state.jwt_payload = None

        with pytest.raises(HTTPException) as exc_info:
            await salla_check_session(request, MagicMock())

        assert exc_info.value.status_code == 401

    asyncio.run(_run())
