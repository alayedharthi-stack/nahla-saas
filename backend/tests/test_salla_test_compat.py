"""Tests for flagged Salla Tenant 1 owner test-store compatibility."""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_BACKEND = os.path.join(_REPO, "backend")
for p in (_REPO, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from database.models import Base, Integration, Tenant  # noqa: E402

if not getattr(Base.metadata, "_salla_test_compat_jsonb_shim", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()

    Base.metadata._salla_test_compat_jsonb_shim = True  # type: ignore[attr-defined]

PARTNER_STORE = "22825873"
PARTNER_ALT = "1979048767"
PARTNER_TENANT = 1
TENANT_35 = 35


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


def _seed(db, *, tenant_id: int, external_store_id: str, cfg: dict | None = None) -> Integration:
    db.merge(Tenant(id=tenant_id, name=f"Tenant {tenant_id}"))
    row = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=external_store_id,
        config=cfg or {},
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_compat_disabled_returns_none(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", False)
    assert compat.resolve_salla_test_compat_tenant(db, merchant_account_id=PARTNER_STORE) is None


def test_tenant1_authorized_when_canonical_external_store_matches(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", True)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", PARTNER_TENANT)
    assert compat.resolve_salla_test_compat_tenant(db, merchant_account_id=PARTNER_STORE) == PARTNER_TENANT


def test_tenant1_authorized_via_config_store_id_anchor(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(
        db,
        tenant_id=PARTNER_TENANT,
        external_store_id=PARTNER_ALT,
        cfg={"store_id": PARTNER_STORE, "salla_merchant_id_alt": PARTNER_ALT},
    )
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", True)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", PARTNER_TENANT)
    assert compat.resolve_salla_test_compat_tenant(db, merchant_account_id=PARTNER_STORE) == PARTNER_TENANT


def test_tenant35_cannot_enter_tenant1(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=TENANT_35, external_store_id=PARTNER_ALT)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", True)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", PARTNER_TENANT)
    assert compat.resolve_salla_test_compat_tenant(db, merchant_account_id=PARTNER_ALT) is None


def test_unknown_merchant_blocked(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", True)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", PARTNER_TENANT)
    assert compat.resolve_salla_test_compat_tenant(db, merchant_account_id="99001122") is None
