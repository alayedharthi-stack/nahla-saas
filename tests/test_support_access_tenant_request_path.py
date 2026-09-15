from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from core.auth import create_token, decode_token  # noqa: E402
from core.database import get_db  # noqa: E402
from models import AuditLog, Base, Tenant, TenantSettings, User  # noqa: E402
from routers.support_access import router  # noqa: E402


@pytest.fixture()
def support_app():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    saved_types = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, JSONB):
                saved_types.append((column, column.type))
                column.type = JSON()
    Base.metadata.create_all(engine)
    for column, original in saved_types:
        column.type = original

    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    tenant_one = Tenant(id=1, name="Tenant One", is_active=True)
    tenant_two = Tenant(id=2, name="Tenant Two", is_active=True)
    db.add_all([tenant_one, tenant_two])
    db.add_all([
        TenantSettings(tenant_id=1, extra_metadata={}),
        TenantSettings(tenant_id=2, extra_metadata={}),
    ])
    admin = User(
        id=10,
        tenant_id=1,
        username="platform@example.com",
        email="platform@example.com",
        password_hash="x",
        is_active=True,
        role="admin",
    )
    tenant_one_approver = User(
        id=11,
        tenant_id=1,
        username="tenant-one-owner@example.com",
        email="tenant-one-owner@example.com",
        password_hash="x",
        is_active=True,
        role="merchant_admin",
    )
    tenant_two_approver = User(
        id=12,
        tenant_id=2,
        username="tenant-two-owner@example.com",
        email="tenant-two-owner@example.com",
        password_hash="x",
        is_active=True,
        role="merchant",
    )
    db.add_all([admin, tenant_one_approver, tenant_two_approver])
    db.commit()

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_jwt(request: Request, call_next):
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            request.state.jwt_payload = decode_token(authorization.removeprefix("Bearer "))
        return await call_next(request)

    app.include_router(router)

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    with patch("routers.support_access._send_access_request_email") as email_mock:
        yield {
            "client": TestClient(app),
            "db": db,
            "admin": admin,
            "tenant_one_approver": tenant_one_approver,
            "tenant_two_approver": tenant_two_approver,
            "email_mock": email_mock,
        }
    db.close()
    engine.dispose()


def _token(user: User, *, role: str | None = None) -> str:
    return create_token(
        email=user.email,
        role=role or user.role,
        tenant_id=user.tenant_id,
        user_id=user.id,
    )


def _admin_headers(ctx) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(ctx['admin'])}"}


def _request_body(tenant_id: int = 1) -> dict[str, object]:
    return {
        "tenant_id": tenant_id,
        "purpose": "INTERNAL_E2E controlled A/B/C smoke verification",
        "duration_hours": 4,
    }


def test_tenant_target_is_discoverable_when_legacy_merchant_filter_is_empty(support_app):
    ctx = support_app
    db = ctx["db"]

    # This reproduces the production divergence: /admin/stats uses exactly
    # role=merchant, so Tenant 1's merchant_admin owner is absent.
    legacy_rows = db.query(User).filter(User.role == "merchant", User.tenant_id == 1).all()
    assert legacy_rows == []

    response = ctx["client"].get(
        "/admin/support-access/targets",
        headers=_admin_headers(ctx),
    )
    assert response.status_code == 200
    tenant_one = next(row for row in response.json()["targets"] if row["tenant_id"] == 1)
    assert tenant_one == {
        "tenant_id": 1,
        "tenant_name": "Tenant One",
        "status": "NONE",
        "can_request": True,
        "approval_recipient_available": True,
        "request_reference": None,
        "requested_at": None,
        "duration_hours": None,
    }
    assert "email" not in tenant_one


def test_authenticated_platform_admin_creates_pending_only_with_audit_and_notification(support_app):
    ctx = support_app
    response = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=_request_body(),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["tenant_id"] == 1
    assert set(payload) == {"request_id", "status", "tenant_id", "duration_hours", "message"}

    settings = ctx["db"].query(TenantSettings).filter_by(tenant_id=1).one()
    metadata = dict(settings.extra_metadata or {})
    rows = metadata["access_requests"]
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["approval_user_id"] == ctx["tenant_one_approver"].id
    assert not dict(metadata.get("support_access") or {}).get("enabled", False)
    assert metadata["notifications"][0]["type"] == "support_access_request"

    audit = ctx["db"].query(AuditLog).filter_by(
        tenant_id=1,
        category="support_access",
        action="support_access_requested",
    ).one()
    assert audit.details["req_id"] == payload["request_id"]
    ctx["email_mock"].assert_called_once()
    assert ctx["email_mock"].call_args.kwargs["merchant_email"] == ctx["tenant_one_approver"].email

    targets = ctx["client"].get(
        "/admin/support-access/targets",
        headers=_admin_headers(ctx),
    )
    tenant_one = next(row for row in targets.json()["targets"] if row["tenant_id"] == 1)
    assert tenant_one["status"] == "PENDING"
    assert tenant_one["can_request"] is False


@pytest.mark.parametrize(
    ("credential_kind", "expected_status"),
    [
        ("none", 401),
        ("merchant", 403),
        ("lifecycle", 401),
    ],
)
def test_request_creation_auth_boundary(support_app, credential_kind: str, expected_status: int):
    ctx = support_app
    headers: dict[str, str] = {}
    if credential_kind == "merchant":
        headers["Authorization"] = f"Bearer {_token(ctx['tenant_one_approver'])}"
    elif credential_kind == "lifecycle":
        headers["X-Nahlah-Lifecycle-Ops-Token"] = "l" * 48

    response = ctx["client"].post(
        "/admin/support-access/requests",
        headers=headers,
        json=_request_body(),
    )
    assert response.status_code == expected_status
    if credential_kind in {"none", "lifecycle"}:
        assert response.json()["detail"] == "Authentication required"
    settings = ctx["db"].query(TenantSettings).filter_by(tenant_id=1).one()
    assert list(dict(settings.extra_metadata or {}).get("access_requests", [])) == []


def test_only_bound_tenant_approver_can_activate_and_request_cannot_be_reused(support_app):
    ctx = support_app
    created = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=_request_body(),
    )
    request_id = created.json()["request_id"]

    platform_attempt = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers=_admin_headers(ctx),
        json={"approve": True, "ttl_hours": 4},
    )
    assert platform_attempt.status_code == 403

    cross_tenant_attempt = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers={"Authorization": f"Bearer {_token(ctx['tenant_two_approver'])}"},
        json={"approve": True, "ttl_hours": 4},
    )
    assert cross_tenant_attempt.status_code == 404

    approved = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers={"Authorization": f"Bearer {_token(ctx['tenant_one_approver'])}"},
        json={"approve": True, "ttl_hours": 4},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    settings = ctx["db"].query(TenantSettings).filter_by(tenant_id=1).one()
    assert dict(settings.extra_metadata or {})["support_access"]["enabled"] is True

    targets = ctx["client"].get(
        "/admin/support-access/targets",
        headers=_admin_headers(ctx),
    )
    tenant_one = next(row for row in targets.json()["targets"] if row["tenant_id"] == 1)
    assert tenant_one["status"] == "APPROVED"
    assert tenant_one["can_request"] is False

    replay = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers={"Authorization": f"Bearer {_token(ctx['tenant_one_approver'])}"},
        json={"approve": True, "ttl_hours": 4},
    )
    assert replay.status_code == 404


def test_end_support_session_still_revokes_active_grant(support_app):
    ctx = support_app
    created = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=_request_body(),
    )
    request_id = created.json()["request_id"]
    merchant_headers = {"Authorization": f"Bearer {_token(ctx['tenant_one_approver'])}"}
    approved = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers=merchant_headers,
        json={"approve": True, "ttl_hours": 4},
    )
    assert approved.status_code == 200

    entered = ctx["client"].post(
        "/admin/impersonate/1",
        headers=_admin_headers(ctx),
    )
    assert entered.status_code == 200
    assert entered.json()["role"] == "support_impersonation"
    assert entered.json()["merchant_email"] == ctx["tenant_one_approver"].email

    ended = ctx["client"].post("/merchant/support-access/resolve", headers=merchant_headers)
    assert ended.status_code == 200
    settings = ctx["db"].query(TenantSettings).filter_by(tenant_id=1).one()
    metadata = dict(settings.extra_metadata or {})
    assert metadata["support_access"]["enabled"] is False
    assert metadata["access_requests"][0]["status"] == "resolved"
    audit_actions = {
        row.action
        for row in ctx["db"].query(AuditLog).filter_by(tenant_id=1, category="support_access").all()
    }
    assert {
        "support_access_requested",
        "support_access_approved",
        "support_impersonate_issued",
        "support_session_resolved",
    }.issubset(audit_actions)

    targets = ctx["client"].get(
        "/admin/support-access/targets",
        headers=_admin_headers(ctx),
    )
    tenant_one = next(row for row in targets.json()["targets"] if row["tenant_id"] == 1)
    assert tenant_one["status"] == "REVOKED"
    assert tenant_one["can_request"] is True


def test_impersonation_resolves_legacy_env_admin_without_user_id(support_app):
    ctx = support_app
    created = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=_request_body(),
    )
    request_id = created.json()["request_id"]
    approved = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers={"Authorization": f"Bearer {_token(ctx['tenant_one_approver'])}"},
        json={"approve": True, "ttl_hours": 4},
    )
    assert approved.status_code == 200

    legacy_admin_token = create_token(
        email=ctx["admin"].email,
        role="admin",
        tenant_id=1,
    )
    entered = ctx["client"].post(
        "/admin/impersonate/1",
        headers={"Authorization": f"Bearer {legacy_admin_token}"},
    )

    assert entered.status_code == 200
    support_claims = decode_token(entered.json()["access_token"])
    assert support_claims is not None
    assert support_claims["role"] == "support_impersonation"
    assert support_claims["actor_user_id"] == ctx["admin"].id


def test_impersonation_fails_closed_when_legacy_actor_is_not_platform_admin(support_app):
    ctx = support_app
    created = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=_request_body(),
    )
    request_id = created.json()["request_id"]
    approved = ctx["client"].post(
        f"/merchant/access-requests/{request_id}/respond",
        headers={"Authorization": f"Bearer {_token(ctx['tenant_one_approver'])}"},
        json={"approve": True, "ttl_hours": 4},
    )
    assert approved.status_code == 200

    unresolved_admin_token = create_token(
        email="missing-platform-actor@example.com",
        role="admin",
        tenant_id=1,
    )
    entered = ctx["client"].post(
        "/admin/impersonate/1",
        headers={"Authorization": f"Bearer {unresolved_admin_token}"},
    )

    assert entered.status_code == 403
    assert entered.json()["detail"] == "platform_admin_actor_required"


def test_browser_cannot_supply_unknown_tenant_or_direct_grant_fields(support_app):
    ctx = support_app
    missing = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=_request_body(999),
    )
    assert missing.status_code == 404

    body = _request_body()
    body.update({"status": "approved", "enabled": True, "approval_user_id": ctx["admin"].id})
    response = ctx["client"].post(
        "/admin/support-access/requests",
        headers=_admin_headers(ctx),
        json=body,
    )
    assert response.status_code == 422
    settings = ctx["db"].query(TenantSettings).filter_by(tenant_id=1).one()
    metadata = dict(settings.extra_metadata or {})
    assert list(metadata.get("access_requests", [])) == []
    assert not dict(metadata.get("support_access") or {}).get("enabled", False)
