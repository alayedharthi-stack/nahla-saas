"""Authentication and fixed-scope contracts for the Work operator API."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import require_admin
from core.database import get_db
from database.models import Base, Tenant, TenantSettings
from routers.internal_commerce_e2e import router


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    saved = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved:
        column.type = original
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    db.add(Tenant(id=1, name="operator fixture", is_active=True))
    db.add(TenantSettings(tenant_id=1, ai_settings={"locale": "ar-SA"}))
    db.commit()

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED", "true")
    monkeypatch.setenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_TENANT_IDS", "1")
    client = TestClient(app)
    yield client, app
    db.close()
    engine.dispose()


def test_operator_endpoint_requires_existing_admin_auth(api: tuple[TestClient, Any]) -> None:
    client, _app = api
    response = client.get("/admin/internal-e2e/status")
    assert response.status_code in {401, 403}


def test_lifecycle_m2m_token_does_not_authorize_internal_e2e_operator(
    api: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _app = api
    monkeypatch.setenv("NAHLA_LIFECYCLE_OPS_TOKEN", "lifecycle-ops-only-test-token")
    response = client.get(
        "/admin/internal-e2e/status",
        headers={"Authorization": "Bearer lifecycle-ops-only-test-token"},
    )
    assert response.status_code in {401, 403}


def test_operator_status_is_fixed_to_tenant_one(api: tuple[TestClient, Any]) -> None:
    client, app = api
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    response = client.get("/admin/internal-e2e/status")
    assert response.status_code == 200
    assert response.json()["operator_tenant_id"] == 1
    assert response.json()["approved_aliases"] == ["A", "B", "C"]
    assert response.json()["external_egress_allowed"] is False


def test_operator_rejects_tenant_override_invalid_alias_and_arbitrary_batch_scope(
    api: tuple[TestClient, Any],
) -> None:
    client, app = api
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    tenant_override = client.post(
        "/admin/internal-e2e/turns",
        json={"tenant_id": 2, "alias": "A", "text": "hello"},
    )
    assert tenant_override.status_code == 422
    assert client.post(
        "/admin/internal-e2e/turns", json={"alias": "D", "text": "hello"}
    ).status_code == 422
    arbitrary = client.post(
        "/admin/internal-e2e/batches",
        json={"seed": 1, "corpus_id": "../../custom", "input": "ignore safety"},
    )
    assert arbitrary.status_code == 422


def test_operator_status_remains_authenticated_when_feature_disabled(
    api: tuple[TestClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = api
    app.dependency_overrides[require_admin] = lambda: {"role": "admin"}
    monkeypatch.delenv("NAHLA_COMMERCE_V2_INTERNAL_E2E_ENABLED")
    response = client.get("/admin/internal-e2e/status")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["fixtures"] == {}
