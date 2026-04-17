"""
tests/test_login_no_snap_and_admin_user_tenant.py
─────────────────────────────────────────────────
Locks down the new safe contract for tenant assignment:

1. `/auth/login` MUST refuse (HTTP 409) when the authenticated user has
   `tenant_id=NULL`. The previous behaviour silently linked the user to
   whichever tenant happened to own a WhatsAppConnection — which in
   production ended up always being tenant_id=1 — and produced the
   "conversations sometimes appear, sometimes vanish" symptom because
   multiple accounts of the same physical owner landed on different
   tenants after their first login.

2. `/admin/users/{user_id}/assign-tenant` is the ONE blessed way to bind
   a user to a tenant: idempotent, refuses cross-tenant moves unless
   `move_existing_data=True` is passed as an explicit ack.

3. `/admin/whatsapp/{tenant_id}/set-token` writes a permanent System User
   token into the merchant's WhatsAppConnection and clears the
   `oauth_session_status=invalid` / `needs_reauth` flags so the
   dashboard banner disappears.

We exercise the router functions directly (no TestClient) so we don't
have to bootstrap the full FastAPI app and its middleware stack.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Tuple

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from models import Base, Tenant, User, WhatsAppConnection  # noqa: E402
from routers import admin as admin_router  # noqa: E402
from routers import auth as auth_router  # noqa: E402


# ─── Test infrastructure ──────────────────────────────────────────────────────

def _make_db(*, allow_user_null_tenant: bool = False) -> Tuple[Any, Any]:
    """
    Build an in-memory SQLite copy of the production schema. Two
    schema relaxations are needed for these tests:
      * JSONB columns get downgraded to JSON.
      * Optionally `users.tenant_id` becomes nullable so we can simulate
        legacy rows that pre-date the NOT NULL constraint and still
        appear in production via backfills / admin tools.
    """
    engine = create_engine("sqlite:///:memory:")
    saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append(("type", col, col.type))
                col.type = JSON()
    user_tid_col = User.__table__.c.tenant_id
    if allow_user_null_tenant:
        saved.append(("nullable", user_tid_col, user_tid_col.nullable))
        user_tid_col.nullable = True
    Base.metadata.create_all(engine)
    for kind, col, original in saved:
        if kind == "type":
            col.type = original
        elif kind == "nullable":
            col.nullable = original
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _fake_request() -> Request:
    """Minimal Starlette Request stub — only `.headers` and `.client` are read."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [(b"x-real-ip", b"127.0.0.1")],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    return Request(scope)


def _seed_tenant(db, *, tid: int = 1, name: str | None = None) -> Tenant:
    t = Tenant(id=tid, name=name or f"T{tid}", is_active=True,
               is_platform_tenant=False)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _seed_user(db, *, email: str, tenant_id: int | None,
               password_hash: str = "x") -> User:
    u = User(username=email, email=email, password_hash=password_hash,
             role="merchant", is_active=True, tenant_id=tenant_id)
    db.add(u); db.commit(); db.refresh(u)
    return u


# ─── 1. /auth/login refuses NULL tenant ───────────────────────────────────────

class TestLoginRefusesUnassignedTenant:
    def test_user_with_null_tenant_gets_409(self, monkeypatch):
        """The new contract: no auto-snap, no auto-create, hard refuse."""
        db, engine = _make_db(allow_user_null_tenant=True)
        try:
            _seed_tenant(db, tid=1)
            _seed_user(db, email="orphan@example.com", tenant_id=None)

            monkeypatch.setattr(auth_router, "JWT_AVAILABLE", True)
            monkeypatch.setattr(auth_router, "BCRYPT_AVAILABLE", True)
            monkeypatch.setattr(auth_router, "verify_password",
                                lambda raw, hashed: True)

            body = auth_router.LoginIn(email="orphan@example.com", password="pw")
            with pytest.raises(HTTPException) as ei:
                asyncio.run(auth_router.auth_login(body, _fake_request(), db))
            assert ei.value.status_code == 409, (
                "Login MUST be refused when user.tenant_id is NULL — "
                "we never silently snap to a default tenant anymore."
            )

            # And the user row must NOT have been mutated as a side-effect.
            db.refresh(db.query(User).first())
            assert db.query(User).first().tenant_id is None
        finally:
            db.close(); engine.dispose()

    def test_user_with_assigned_tenant_logs_in_normally(self, monkeypatch):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=42, name="happy-merchant")
            _seed_user(db, email="happy@example.com", tenant_id=42)

            monkeypatch.setattr(auth_router, "JWT_AVAILABLE", True)
            monkeypatch.setattr(auth_router, "BCRYPT_AVAILABLE", True)
            monkeypatch.setattr(auth_router, "verify_password",
                                lambda raw, hashed: True)
            captured = {}

            def fake_create_token(**kw):
                captured.update(kw)
                return "tok"

            monkeypatch.setattr(auth_router, "create_token", fake_create_token)

            body = auth_router.LoginIn(email="happy@example.com", password="pw")
            res = asyncio.run(auth_router.auth_login(body, _fake_request(), db))
            assert res["tenant_id"] == 42
            assert res["access_token"] == "tok"
            assert captured["tenant_id"] == 42, (
                "JWT must encode the user's existing tenant_id verbatim."
            )
        finally:
            db.close(); engine.dispose()


# ─── 2. /admin/users/{id}/assign-tenant ───────────────────────────────────────

class TestAssignTenantEndpoint:
    def test_first_assignment_succeeds(self):
        db, engine = _make_db(allow_user_null_tenant=True)
        try:
            _seed_tenant(db, tid=7, name="target")
            u = _seed_user(db, email="o@x.com", tenant_id=None)

            res = asyncio.run(admin_router.admin_users_assign_tenant(
                user_id=u.id,
                body=admin_router._AssignTenantBody(tenant_id=7),
                db=db,
            ))
            assert res["status"] == "assigned"
            assert res["tenant_id"] == 7
            assert res["previous_tenant_id"] is None

            db.refresh(u)
            assert u.tenant_id == 7
        finally:
            db.close(); engine.dispose()

    def test_reassign_blocked_without_explicit_ack(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            _seed_tenant(db, tid=2)
            u = _seed_user(db, email="bound@x.com", tenant_id=1)

            with pytest.raises(HTTPException) as ei:
                asyncio.run(admin_router.admin_users_assign_tenant(
                    user_id=u.id,
                    body=admin_router._AssignTenantBody(tenant_id=2),
                    db=db,
                ))
            assert ei.value.status_code == 409
            db.refresh(u)
            assert u.tenant_id == 1, "Binding must NOT change without ack."
        finally:
            db.close(); engine.dispose()

    def test_reassign_with_ack_overwrites(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=1)
            _seed_tenant(db, tid=2)
            u = _seed_user(db, email="bound@x.com", tenant_id=1)

            res = asyncio.run(admin_router.admin_users_assign_tenant(
                user_id=u.id,
                body=admin_router._AssignTenantBody(
                    tenant_id=2, move_existing_data=True,
                ),
                db=db,
            ))
            assert res["previous_tenant_id"] == 1
            assert res["tenant_id"] == 2
            db.refresh(u)
            assert u.tenant_id == 2
        finally:
            db.close(); engine.dispose()

    def test_idempotent_noop_when_target_equals_current(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=5)
            u = _seed_user(db, email="x@x.com", tenant_id=5)

            res = asyncio.run(admin_router.admin_users_assign_tenant(
                user_id=u.id,
                body=admin_router._AssignTenantBody(tenant_id=5),
                db=db,
            ))
            assert res["status"] == "noop"
        finally:
            db.close(); engine.dispose()


# ─── 3. /admin/whatsapp/{tenant_id}/set-token ─────────────────────────────────

class TestSetWhatsAppTokenEndpoint:
    def _seed_wa(self, db, tenant_id: int, *, with_invalid_oauth: bool = True):
        wa = WhatsAppConnection(
            tenant_id=tenant_id,
            status="connected",
            phone_number_id="100",
            whatsapp_business_account_id="WABA",
            access_token="OLD_TOKEN_ENDING_OLDEND",
            token_type="oauth_user",
            extra_metadata=({
                "oauth_session_status": "invalid",
                "oauth_session_needs_reauth": True,
                "token_status": "expiring_soon",
            } if with_invalid_oauth else {}),
        )
        db.add(wa); db.commit(); db.refresh(wa)
        return wa

    def test_token_replacement_clears_oauth_invalid_flag(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=11)
            wa = self._seed_wa(db, tenant_id=11)

            new_tok = "EAA_PERMANENT_SYSTEM_USER_TOKEN_LONG_OK"
            res = asyncio.run(admin_router.admin_whatsapp_set_token(
                tenant_id=11,
                body=admin_router._SetWaTokenBody(
                    access_token=new_tok,
                    token_type="permanent_system_user",
                    note="rotated by owner",
                ),
                db=db,
            ))
            assert res["status"] == "ok"
            assert res["token_tail"] == new_tok[-6:]
            assert res["previous_token_tail"] == "OLDEND"

            db.refresh(wa)
            assert wa.access_token == new_tok
            assert wa.token_expires_at is None, (
                "Permanent tokens have no expiry; we must wipe the old one."
            )
            assert wa.token_type == "permanent_system_user"
            meta = wa.extra_metadata or {}
            assert meta["token_status"] == "permanent"
            assert meta["oauth_session_needs_reauth"] is False
            assert meta["oauth_session_status"] == "replaced_with_permanent"
            assert meta["last_token_set_note"] == "rotated by owner"
        finally:
            db.close(); engine.dispose()

    def test_404_when_no_connection(self):
        db, engine = _make_db()
        try:
            _seed_tenant(db, tid=99)
            with pytest.raises(HTTPException) as ei:
                asyncio.run(admin_router.admin_whatsapp_set_token(
                    tenant_id=99,
                    body=admin_router._SetWaTokenBody(
                        access_token="x" * 30,
                    ),
                    db=db,
                ))
            assert ei.value.status_code == 404
        finally:
            db.close(); engine.dispose()
