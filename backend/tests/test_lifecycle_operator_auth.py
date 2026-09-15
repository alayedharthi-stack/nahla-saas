"""Authentication isolation for the lifecycle operations M2M surface."""
from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from core.auth import create_token, require_admin
from core.database import get_db
from core.lifecycle_operator_auth import (
    LIFECYCLE_OPS_TOKEN_ENV,
    LIFECYCLE_OPS_TOKEN_HEADER,
    MIN_LIFECYCLE_OPS_TOKEN_LENGTH,
)
from core.middleware import jwt_enforcement_middleware
from routers import admin_lifecycle_operations as ops_router


OPS_TOKEN = "o" * MIN_LIFECYCLE_OPS_TOKEN_LENGTH


def _client(monkeypatch):
    app = FastAPI()
    app.middleware("http")(jwt_enforcement_middleware)
    app.include_router(ops_router.router)

    def _db():
        yield object()

    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(
        ops_router,
        "build_lifecycle_preflight",
        lambda _db: {"schema_ready": True},
    )
    monkeypatch.setattr(
        ops_router,
        "build_order_recovery_preflight",
        lambda _db, *, order_id: {"order_id": order_id, "order_exists": True},
    )
    retry = AsyncMock(return_value={"outcome": "duplicate", "eligibility": {}})
    monkeypatch.setattr(ops_router, "retry_post_cod_final_confirmation", retry)

    @app.get("/admin/unrelated")
    def unrelated(_admin=Depends(require_admin)):
        return {"ok": True}

    return TestClient(app), retry


def _ops_headers(token=OPS_TOKEN):
    return {LIFECYCLE_OPS_TOKEN_HEADER: token}


def test_missing_and_invalid_ops_token_are_rejected(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setenv(LIFECYCLE_OPS_TOKEN_ENV, OPS_TOKEN)
    assert client.get("/admin/operations/commerce-lifecycle/preflight").status_code == 401
    assert client.get(
        "/admin/operations/commerce-lifecycle/preflight",
        headers=_ops_headers("x" * MIN_LIFECYCLE_OPS_TOKEN_LENGTH),
    ).status_code == 403


def test_ops_token_allows_only_the_three_lifecycle_routes(monkeypatch):
    client, retry = _client(monkeypatch)
    monkeypatch.setenv(LIFECYCLE_OPS_TOKEN_ENV, OPS_TOKEN)

    global_response = client.get(
        "/admin/operations/commerce-lifecycle/preflight", headers=_ops_headers()
    )
    order_response = client.get(
        "/admin/operations/commerce-lifecycle/orders/160/preflight", headers=_ops_headers()
    )
    retry_response = client.post(
        "/admin/operations/orders/160/retry-final-confirmation", headers=_ops_headers()
    )

    assert global_response.status_code == 200
    assert global_response.json() == {"schema_ready": True}
    assert order_response.status_code == 200
    assert order_response.json() == {"order_id": 160, "order_exists": True}
    assert retry_response.status_code == 200
    retry.assert_awaited_once()
    assert retry.await_args.kwargs == {"order_id": 160}
    assert client.get("/admin/unrelated", headers=_ops_headers()).status_code == 401


def test_merchant_and_support_tokens_remain_rejected(monkeypatch):
    client, _ = _client(monkeypatch)
    merchant = create_token("merchant@example.test", "merchant", 1)
    support = create_token(
        "merchant@example.test",
        "support_impersonation",
        1,
        extra_claims={"impersonation": True, "actor_user_id": 999},
    )
    for token in (merchant, support):
        response = client.get(
            "/admin/operations/commerce-lifecycle/preflight",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403


def test_normal_platform_admin_jwt_remains_accepted(monkeypatch):
    client, _ = _client(monkeypatch)
    admin = create_token("admin@example.test", "admin", 1)
    response = client.get(
        "/admin/operations/commerce-lifecycle/preflight",
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert response.status_code == 200


def test_unset_or_short_configured_token_fails_closed(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.delenv(LIFECYCLE_OPS_TOKEN_ENV, raising=False)
    assert client.get(
        "/admin/operations/commerce-lifecycle/preflight", headers=_ops_headers()
    ).status_code == 401

    monkeypatch.setenv(LIFECYCLE_OPS_TOKEN_ENV, "short")
    assert client.get(
        "/admin/operations/commerce-lifecycle/preflight", headers=_ops_headers()
    ).status_code == 401


def test_ops_token_is_never_returned_or_added_to_audit(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setenv(LIFECYCLE_OPS_TOKEN_ENV, OPS_TOKEN)
    captured = []
    monkeypatch.setattr(ops_router, "audit", lambda event, **metadata: captured.append((event, metadata)))

    response = client.get(
        "/admin/operations/commerce-lifecycle/preflight", headers=_ops_headers()
    )

    assert response.status_code == 200
    assert OPS_TOKEN not in response.text
    assert OPS_TOKEN not in repr(captured)
    assert captured[0][1]["auth_method"] == "lifecycle_ops_token"
