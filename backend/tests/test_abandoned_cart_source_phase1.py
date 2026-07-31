"""
Phase 1 — abandoned cart source tenant configuration model.

Config/resolution only. Does not gate sends or change runtime recovery.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.abandoned_cart_source import (  # noqa: E402
    ABANDONED_CART_SOURCE_DISABLED,
    ABANDONED_CART_SOURCE_NAHLA_SHOP,
    ABANDONED_CART_SOURCE_SALLA_STOREFRONT,
    AbandonedCartSourceValidationError,
    get_configured_abandoned_cart_source,
    normalize_configured_abandoned_cart_source,
    resolve_effective_abandoned_cart_source,
    resolve_tenant_abandoned_cart_source,
    set_configured_abandoned_cart_source,
    tenant_is_salla_connected,
)
from models import Base, Integration, Tenant, TenantSettings  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)()


def _seed_tenant(db, *, name: str) -> Tenant:
    tenant = Tenant(name=name, is_active=True)
    db.add(tenant)
    db.flush()
    db.add(TenantSettings(tenant_id=tenant.id, store_settings={"store_name": name}))
    db.commit()
    db.refresh(tenant)
    return tenant


def _seed_salla(db, tenant_id: int, *, store_id: str = "store-100") -> Integration:
    intg = Integration(
        tenant_id=tenant_id,
        provider="salla",
        external_store_id=store_id,
        enabled=True,
        config={
            "api_key": "test-key",
            "store_id": store_id,
            "app_type": "easy",
        },
    )
    db.add(intg)
    db.commit()
    db.refresh(intg)
    return intg


def _settings(db, tenant_id: int) -> TenantSettings:
    return (
        db.query(TenantSettings)
        .filter(TenantSettings.tenant_id == tenant_id)
        .one()
    )


def test_salla_connected_without_override_defaults_to_storefront():
    db = _make_db()
    tenant = _seed_tenant(db, name="Salla Merchant A")
    _seed_salla(db, tenant.id)

    resolution = resolve_tenant_abandoned_cart_source(db, tenant.id)
    assert resolution.configured_source is None
    assert resolution.salla_connected is True
    assert resolution.effective_source == ABANDONED_CART_SOURCE_SALLA_STOREFRONT


def test_non_salla_without_override_defaults_to_nahla_shop():
    db = _make_db()
    tenant = _seed_tenant(db, name="Generic Shop B")

    resolution = resolve_tenant_abandoned_cart_source(db, tenant.id)
    assert resolution.configured_source is None
    assert resolution.salla_connected is False
    assert resolution.effective_source == ABANDONED_CART_SOURCE_NAHLA_SHOP


def test_salla_tenant_override_nahla_shop():
    db = _make_db()
    tenant = _seed_tenant(db, name="Salla Override Shop")
    _seed_salla(db, tenant.id)
    settings = _settings(db, tenant.id)

    set_configured_abandoned_cart_source(
        settings,
        ABANDONED_CART_SOURCE_NAHLA_SHOP,
        salla_connected=True,
    )
    db.commit()

    resolution = resolve_tenant_abandoned_cart_source(db, tenant.id)
    assert resolution.configured_source == ABANDONED_CART_SOURCE_NAHLA_SHOP
    assert resolution.effective_source == ABANDONED_CART_SOURCE_NAHLA_SHOP


def test_non_salla_cannot_select_salla_storefront():
    """Unavailable source must fail validation — not silently accept."""
    db = _make_db()
    tenant = _seed_tenant(db, name="Non-Salla Cannot Pick Storefront")
    settings = _settings(db, tenant.id)
    connected = tenant_is_salla_connected(db, tenant.id)
    assert connected is False

    with pytest.raises(AbandonedCartSourceValidationError) as exc:
        set_configured_abandoned_cart_source(
            settings,
            ABANDONED_CART_SOURCE_SALLA_STOREFRONT,
            salla_connected=connected,
        )
    assert exc.value.code == "unavailable_abandoned_cart_source"
    assert get_configured_abandoned_cart_source(settings) is None
    assert (
        resolve_tenant_abandoned_cart_source(db, tenant.id).effective_source
        == ABANDONED_CART_SOURCE_NAHLA_SHOP
    )


def test_override_disabled():
    db = _make_db()
    tenant = _seed_tenant(db, name="Disabled Journey Merchant")
    _seed_salla(db, tenant.id)
    settings = _settings(db, tenant.id)

    set_configured_abandoned_cart_source(
        settings,
        ABANDONED_CART_SOURCE_DISABLED,
        salla_connected=True,
    )
    db.commit()

    resolution = resolve_tenant_abandoned_cart_source(db, tenant.id)
    assert resolution.configured_source == ABANDONED_CART_SOURCE_DISABLED
    assert resolution.effective_source == ABANDONED_CART_SOURCE_DISABLED


def test_unknown_source_validation_error():
    with pytest.raises(AbandonedCartSourceValidationError) as exc:
        normalize_configured_abandoned_cart_source("both")
    assert exc.value.code == "invalid_abandoned_cart_source"

    with pytest.raises(AbandonedCartSourceValidationError):
        normalize_configured_abandoned_cart_source("hybrid")

    assert resolve_effective_abandoned_cart_source(
        configured_source=None,
        salla_connected=True,
    ) == ABANDONED_CART_SOURCE_SALLA_STOREFRONT


def test_webhook_path_does_not_mutate_configured_source():
    """Webhook/cart ingest modules must not write abandoned_cart_source."""
    db = _make_db()
    tenant = _seed_tenant(db, name="Webhook Isolation Merchant")
    _seed_salla(db, tenant.id)
    before = resolve_tenant_abandoned_cart_source(db, tenant.id)
    assert before.configured_source is None

    import core.webhook_dispatcher as webhook_dispatcher  # noqa: PLC0415
    import services.store_sync as store_sync  # noqa: PLC0415

    for mod in (store_sync, webhook_dispatcher):
        src = inspect.getsource(mod)
        assert "abandoned_cart_source" not in src
        assert "set_configured_abandoned_cart_source" not in src

    after = resolve_tenant_abandoned_cart_source(db, tenant.id)
    assert after.configured_source is None
    assert after.effective_source == before.effective_source


def test_tenant_a_override_does_not_affect_tenant_b():
    db = _make_db()
    tenant_a = _seed_tenant(db, name="Tenant A Cart Source")
    tenant_b = _seed_tenant(db, name="Tenant B Cart Source")
    _seed_salla(db, tenant_a.id, store_id="store-a")
    _seed_salla(db, tenant_b.id, store_id="store-b")

    set_configured_abandoned_cart_source(
        _settings(db, tenant_a.id),
        ABANDONED_CART_SOURCE_NAHLA_SHOP,
        salla_connected=True,
    )
    db.commit()

    res_a = resolve_tenant_abandoned_cart_source(db, tenant_a.id)
    res_b = resolve_tenant_abandoned_cart_source(db, tenant_b.id)
    assert res_a.effective_source == ABANDONED_CART_SOURCE_NAHLA_SHOP
    assert res_b.configured_source is None
    assert res_b.effective_source == ABANDONED_CART_SOURCE_SALLA_STOREFRONT


def test_missing_configured_value_on_existing_tenant_does_not_crash():
    db = _make_db()
    tenant = _seed_tenant(db, name="Legacy Tenant Null Source")
    settings = _settings(db, tenant.id)
    assert settings.abandoned_cart_source is None
    assert get_configured_abandoned_cart_source(settings) is None

    resolution = resolve_tenant_abandoned_cart_source(db, tenant.id, settings=settings)
    assert resolution.effective_source == ABANDONED_CART_SOURCE_NAHLA_SHOP

    # Settings row missing entirely still resolves via default.
    orphan = Tenant(name="Orphan Settings Tenant", is_active=True)
    db.add(orphan)
    db.commit()
    resolution_orphan = resolve_tenant_abandoned_cart_source(db, orphan.id)
    assert resolution_orphan.configured_source is None
    assert resolution_orphan.effective_source == ABANDONED_CART_SOURCE_NAHLA_SHOP


def test_both_sources_not_supported_in_model():
    assert "both" not in {
        ABANDONED_CART_SOURCE_SALLA_STOREFRONT,
        ABANDONED_CART_SOURCE_NAHLA_SHOP,
        ABANDONED_CART_SOURCE_DISABLED,
    }
    with pytest.raises(AbandonedCartSourceValidationError):
        set_configured_abandoned_cart_source(
            type("S", (), {"abandoned_cart_source": None})(),
            "both",
            salla_connected=True,
        )
