"""Resolver ambiguity tests for integration-first Salla routing (A1-v3.7)."""
from __future__ import annotations

import sys
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "backend"
for p in (str(_REPO), str(_BACKEND), str(_REPO / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import Base, Integration, Tenant  # noqa: E402
from services.salla_integration_resolver import (  # noqa: E402
    ResolvedSallaIntegration,
    UnresolvedSallaIntegration,
    resolve_salla_integration_connection,
)


@event.listens_for(Base.metadata, "before_create")
def _sqlite_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(Tenant(id=1, name="متجر تجريبي عام"))
    session.commit()
    yield session
    session.close()


def test_tier_a_ambiguity_returns_unresolved() -> None:
    row_a = SimpleNamespace(
        id=1,
        tenant_id=1,
        external_store_id="DUP-STORE",
        config={"app_type": "easy", "api_sync_enabled": False},
        enabled=True,
    )
    row_b = SimpleNamespace(
        id=2,
        tenant_id=2,
        external_store_id="DUP-STORE",
        config={"app_type": "easy", "api_sync_enabled": False},
        enabled=True,
    )
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [row_a, row_b]

    result = resolve_salla_integration_connection(
        db,
        webhook_provider_channel="salla",
        canonical_store_id="DUP-STORE",
    )
    assert isinstance(result, UnresolvedSallaIntegration)
    assert result.reason == "ambiguous_tier_a"


def test_tier_b_ambiguity_returns_unresolved(db) -> None:
    for suffix in ("A", "B"):
        db.add(
            Integration(
                tenant_id=1,
                provider="salla",
                external_store_id=None,
                config={
                    "api_key": f"k-{suffix}",
                    "store_id": "CFG-DUP",
                    "app_type": "easy",
                    "api_sync_enabled": False,
                },
                enabled=True,
            )
        )
    db.commit()

    result = resolve_salla_integration_connection(
        db,
        webhook_provider_channel="salla",
        canonical_store_id="CFG-DUP",
    )
    assert isinstance(result, UnresolvedSallaIntegration)
    assert result.reason == "ambiguous_tier_b"


def test_tier_a_single_match_resolves(db) -> None:
    db.add(
        Integration(
            tenant_id=1,
            provider="salla",
            external_store_id="UNIQUE-STORE",
            config={"api_key": "k", "app_type": "easy", "api_sync_enabled": False},
            enabled=True,
        )
    )
    db.commit()

    result = resolve_salla_integration_connection(
        db,
        webhook_provider_channel="salla",
        canonical_store_id="UNIQUE-STORE",
    )
    assert isinstance(result, ResolvedSallaIntegration)
    assert result.matched_via == "tier_a_external_store_id+channel"
