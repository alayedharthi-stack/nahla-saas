"""Env-admin revalidation keeps the absolute access-token lifetime.

All identities/keys/passwords are synthetic. DB reads are controlled mocks and
revocation uses its real in-process fallback; no external services are called.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from core import auth as auth_core
from core import token_revocation
from core.database import get_db
from routers import auth as auth_routes

ADMIN = "admin@example.test"
KEY = "synthetic-session-regression-signing-key-0001"
PASSWORD = "Synthetic-login-password-0001"


@pytest.fixture
def session_env(monkeypatch):
    monkeypatch.setattr(auth_core, "JWT_SECRET", KEY)
    monkeypatch.setattr(auth_core, "ADMIN_EMAIL", ADMIN)
    monkeypatch.setattr(auth_core, "JWT_EXPIRE_H", 1)
    monkeypatch.setattr(auth_core, "JWT_REFRESH_GRACE_DAYS", 30)
    monkeypatch.setattr(auth_routes, "ADMIN_EMAIL", ADMIN)
    monkeypatch.setattr(auth_routes, "ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(auth_routes, "JWT_SECRET", KEY)
    monkeypatch.setattr(token_revocation, "get_redis", lambda: None)
    monkeypatch.setattr(token_revocation, "_LOCAL_REVOKED", OrderedDict())
    # Login rate limiting/2FA enrollment are outside this session regression.
    monkeypatch.setattr(auth_routes, "_enforce_login_rate_limits", lambda *args: None)
    monkeypatch.setattr(auth_routes, "_user_has_2fa_enabled", lambda *args: False)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client, db


def _claims(**overrides):
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "sub": ADMIN, "role": "admin", "tenant_id": 1,
        "iat": now, "exp": now + 3600, "jti": "synthetic-env-admin-jti",
        **overrides,
    }


def _sign(claims, key=KEY, algorithm="HS256"):
    return jwt.encode(claims, key, algorithm=algorithm)


def _refresh(client, token):
    return client.post("/auth/session/refresh", headers={"Authorization": f"Bearer {token}"})


def _no_mint(monkeypatch):
    mint = Mock(side_effect=AssertionError("env-admin must not mint a token"))
    monkeypatch.setattr(auth_routes, "create_token", mint)
    return mint


def _database_user(db, *, email="merchant@example.test", role="merchant", active=True,
                   tenant_id=7, tenant_exists=True):
    user = SimpleNamespace(id=3, email=email, role=role, is_active=active, tenant_id=tenant_id)
    tenant = SimpleNamespace(id=tenant_id) if tenant_exists else None
    db.query.return_value.filter.return_value.first.side_effect = [user, tenant]
    return user


@pytest.mark.parametrize("transport", ["json", "form"])
def test_login_then_refresh_accepts_one_hour_env_admin_without_db_rows(session_env, monkeypatch, transport):
    client, db = session_env
    if transport == "json":
        login = client.post("/auth/login", json={"email": ADMIN, "password": PASSWORD})
    else:
        login = client.post("/auth/login-form", data={"email": ADMIN, "password": PASSWORD})
    assert login.status_code == 200
    token = login.json()["access_token"]
    claims = auth_core.decode_token(token)
    assert claims["exp"] - claims["iat"] == 3600
    assert "user_id" not in claims
    db.query.reset_mock()
    mint = _no_mint(monkeypatch)

    # Reopening/focusing repeatedly cannot extend exp or rotate jti.
    for _ in range(2):
        response = _refresh(client, token)
        assert response.status_code == 200
        assert response.json() == {
            "access_token": token, "role": "admin", "tenant_id": 1, "email": ADMIN,
        }
        assert auth_core.decode_token(response.json()["access_token"]) == claims
    db.query.assert_not_called()
    mint.assert_not_called()
    db.add.assert_not_called()
    db.flush.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize("expired_seconds", [1, 3600, 29 * 86400, 31 * 86400])
def test_expired_env_admin_never_renews_even_if_db_user_exists(session_env, monkeypatch, expired_seconds):
    client, db = session_env
    _database_user(db, email=ADMIN, role="admin", tenant_id=1)
    mint = _no_mint(monkeypatch)
    token = _sign(_claims(exp=int(datetime.now(timezone.utc).timestamp()) - expired_seconds))
    assert _refresh(client, token).status_code == 401
    db.query.assert_not_called()
    mint.assert_not_called()


def test_env_admin_expiry_boundary_is_closed(session_env, monkeypatch):
    client, db = session_env
    claims = _claims()
    class AtExpiry:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(claims["exp"], tz=tz)
    monkeypatch.setattr(auth_routes, "datetime", AtExpiry)
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(claims)).status_code == 401
    db.query.assert_not_called()


def test_revoked_env_admin_fails_closed(session_env, monkeypatch):
    client, db = session_env
    claims = _claims()
    token_revocation.revoke_jti(claims["jti"], claims["exp"])
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(claims)).status_code == 401
    db.query.assert_not_called()


@pytest.mark.parametrize("token", ["malformed", "", "bad-signature", "wrong-algorithm"])
def test_invalid_env_admin_token_fails_closed(session_env, monkeypatch, token):
    client, db = session_env
    if token == "bad-signature":
        token = _sign(_claims(), key="different-synthetic-key")
    elif token == "wrong-algorithm":
        token = _sign(_claims(), algorithm="HS384")
    _no_mint(monkeypatch)
    assert _refresh(client, token).status_code == 401
    db.query.assert_not_called()


@pytest.mark.parametrize("exp", [None, "tomorrow", "9999999999", True, False, float("nan"),
                                  float("inf"), float("-inf"), {}, []])
def test_invalid_exp_fails_closed_before_db_or_mint(session_env, monkeypatch, exp):
    client, db = session_env
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(_claims(exp=exp))).status_code == 401
    db.query.assert_not_called()


def test_missing_exp_fails_closed(session_env, monkeypatch):
    client, db = session_env
    claims = _claims()
    del claims["exp"]
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(claims)).status_code == 401
    db.query.assert_not_called()


@pytest.mark.parametrize("purpose", ["password_reset", "verify_email", "invite",
                                     "2fa_challenge", "2fa_setup", "", None])
@pytest.mark.parametrize("db_backed", [False, True])
def test_purpose_tokens_cannot_be_revalidated_or_promoted(session_env, monkeypatch, purpose, db_backed):
    client, db = session_env
    claims = _claims(type=purpose)
    if db_backed:
        claims["user_id"] = 3
        _database_user(db, email=ADMIN, role="admin", tenant_id=1)
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(claims)).status_code == 401
    db.query.assert_not_called()


@pytest.mark.parametrize("overrides", [
    {"sub": "other@example.test"}, {"role": "owner"}, {"role": "merchant"},
    {"tenant_id": 7}, {"tenant_id": "1"}, {"tenant_id": True}, {"tenant_id": 1.0},
    {"jti": None}, {"jti": ""}, {"user_id": None}, {"user_id": 0},
    {"impersonation": True}, {"impersonation": False},
    {"actor_sub": ADMIN}, {"actor_user_id": 0}, {"session_version": 1},
    {"role": "support_impersonation", "impersonation": True, "user_id": 3},
])
def test_other_identities_cannot_select_env_admin_path(session_env, monkeypatch, overrides):
    client, _ = session_env
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(_claims(**overrides))).status_code == 401


def test_cached_admin_role_cannot_turn_merchant_token_into_env_admin(session_env, monkeypatch):
    client, _ = session_env
    _no_mint(monkeypatch)
    token = _sign(_claims(sub="merchant@example.test", role="merchant", tenant_id=7, user_id=3))
    response = client.post("/auth/session/refresh", headers={
        "Authorization": f"Bearer {token}", "X-Cached-Role": "admin",
    })
    assert response.status_code == 401


@pytest.mark.parametrize("expired", [False, True])
def test_merchant_refresh_still_rolls_valid_and_recently_expired_sessions(session_env, expired):
    client, db = session_env
    user = _database_user(db)
    now = int(datetime.now(timezone.utc).timestamp())
    claims = _claims(sub=user.email, role="merchant", tenant_id=7, user_id=3,
                     exp=now - 3600 if expired else now + 3600)
    token = _sign(claims)
    response = _refresh(client, token)
    assert response.status_code == 200
    fresh = auth_core.decode_token(response.json()["access_token"])
    assert response.json()["access_token"] != token
    assert fresh["jti"] != claims["jti"]
    assert fresh["exp"] - fresh["iat"] == 3600
    assert fresh["exp"] > now
    assert (fresh["sub"], fresh["role"], fresh["tenant_id"], fresh["user_id"]) == (
        user.email, "merchant", 7, 3,
    )


@pytest.mark.parametrize("reason", ["revoked", "beyond-grace"])
def test_invalid_merchant_session_fails_closed(session_env, monkeypatch, reason):
    client, db = session_env
    claims = _claims(sub="merchant@example.test", role="merchant", tenant_id=7, user_id=3)
    if reason == "revoked":
        token_revocation.revoke_jti(claims["jti"], claims["exp"])
    else:
        claims["exp"] = int(datetime.now(timezone.utc).timestamp()) - 31 * 86400
    _no_mint(monkeypatch)
    assert _refresh(client, _sign(claims)).status_code == 401
    db.query.assert_not_called()


@pytest.mark.parametrize("reason", ["missing-user", "inactive-user", "scope-mismatch", "missing-tenant"])
def test_merchant_account_and_tenant_checks_remain_enforced(session_env, monkeypatch, reason):
    client, db = session_env
    if reason != "missing-user":
        _database_user(db, active=reason != "inactive-user",
                       tenant_id=8 if reason == "scope-mismatch" else 7,
                       tenant_exists=reason != "missing-tenant")
    _no_mint(monkeypatch)
    token = _sign(_claims(sub="merchant@example.test", role="merchant", tenant_id=7, user_id=3))
    assert _refresh(client, token).status_code == 401


def test_db_admin_with_user_id_keeps_db_account_checks(session_env, monkeypatch):
    client, db = session_env
    _database_user(db, email=ADMIN, role="admin", active=False, tenant_id=1)
    _no_mint(monkeypatch)
    token = _sign(_claims(user_id=3))
    assert _refresh(client, token).status_code == 401
    db.query.assert_called_once()


@pytest.mark.parametrize("purpose", ["password_reset", "verify_email", "invite"])
def test_dedicated_purpose_token_decoder_remains_available(session_env, purpose):
    # Refresh hardening does not alter the decoder used by dedicated routes.
    token = _sign(_claims(type=purpose))
    assert auth_core.decode_token(token)["type"] == purpose
    assert auth_core.decode_token_for_refresh(token) is None
