"""Tests for flagged Salla Tenant 1 owner test-store compatibility (PR #878 security controls)."""
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
NAHLA_APP_ID = "2067202718"


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


def _enable_compat(monkeypatch: pytest.MonkeyPatch, compat) -> None:
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", True)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", PARTNER_TENANT)
    monkeypatch.setattr(compat, "SALLA_CLIENT_ID", NAHLA_APP_ID)


# A — exact external_store_id + correct tenant → PASS
def test_exact_external_store_id_authorizes(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    row = _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    _enable_compat(monkeypatch, compat)
    match = compat.resolve_salla_test_compat_match(
        db,
        merchant_account_id=PARTNER_STORE,
        app_id=NAHLA_APP_ID,
    )
    assert match is not None
    assert match.tenant_id == PARTNER_TENANT
    assert match.integration_id == row.id
    assert match.external_store_id == PARTNER_STORE
    assert match.matched_via == "external_store_id"


# B — config.store_id matches but external_store_id differs → BLOCK
def test_config_store_id_without_external_store_id_blocked(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(
        db,
        tenant_id=PARTNER_TENANT,
        external_store_id=PARTNER_ALT,
        cfg={"store_id": PARTNER_STORE, "salla_merchant_id_alt": PARTNER_ALT},
    )
    _enable_compat(monkeypatch, compat)
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_STORE) is None


# C — stale alias matches → BLOCK
def test_stale_alias_identity_blocked(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(
        db,
        tenant_id=PARTNER_TENANT,
        external_store_id=PARTNER_STORE,
        cfg={"salla_merchant_id_alt": PARTNER_ALT},
    )
    _enable_compat(monkeypatch, compat)
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_ALT) is None


# D — Tenant 35 identity → BLOCK
def test_tenant35_identity_blocked(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=TENANT_35, external_store_id=PARTNER_ALT)
    _enable_compat(monkeypatch, compat)
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_ALT) is None


# E — unknown identity → BLOCK
def test_unknown_identity_blocked(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    _enable_compat(monkeypatch, compat)
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id="99001122") is None


# F — flag OFF → BLOCK
def test_flag_off_blocks(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", False)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", PARTNER_TENANT)
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_STORE) is None


# G — flag ON but required config missing → BLOCK
def test_flag_on_missing_config_blocked(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_ENABLED", True)
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TRUSTED_IDENTITY", "")
    monkeypatch.setattr(compat, "SALLA_TEST_COMPAT_TENANT_ID", 0)
    assert compat.salla_test_compat_config_ready() is False
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_STORE) is None


# H — exact matched integration id returned (no first-row lookup semantics)
def test_match_carries_exact_integration_id(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    first = _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_ALT, cfg={"store_id": PARTNER_ALT})
    second = _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    _enable_compat(monkeypatch, compat)
    match = compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_STORE)
    assert match is not None
    assert match.integration_id == second.id
    assert match.integration_id != first.id


def test_app_id_mismatch_blocked_when_server_app_configured(db, monkeypatch: pytest.MonkeyPatch):
    from services import salla_test_compat as compat

    _seed(db, tenant_id=PARTNER_TENANT, external_store_id=PARTNER_STORE)
    _enable_compat(monkeypatch, compat)
    assert compat.resolve_salla_test_compat_match(db, merchant_account_id=PARTNER_STORE, app_id="wrong-app") is None
