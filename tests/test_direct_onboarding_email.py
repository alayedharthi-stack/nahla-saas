"""Direct merchant onboarding — register, verify, welcome email contract."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Tuple

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from core.auth import create_verify_token, hash_password  # noqa: E402
from core.direct_welcome_email import (  # noqa: E402
    _WELCOME_FLAG,
    welcome_email_already_sent,
)
from models import Base, Tenant, TenantSettings, User  # noqa: E402
from routers import auth as auth_router  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _fake_request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/register",
        "headers": [(b"x-real-ip", b"127.0.0.1")],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    return Request(scope)


def _run_async(coro):
    return asyncio.run(coro)


def _patch_register_email_capture(monkeypatch):
    sent: list[dict] = []
    pending: list = []

    async def fake_send_email(*, to, subject, html):
        sent.append({"to": to, "subject": subject, "html": html})
        return True

    def fake_ensure_future(coro):
        pending.append(coro)

    def flush_pending():
        if not pending:
            return
        loop = asyncio.new_event_loop()
        try:
            while pending:
                coro = pending.pop(0)
                loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr("routers.auth.send_email", fake_send_email)
    monkeypatch.setattr(auth_router.asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr(auth_router, "notify_welcome", lambda *a, **k: None)
    return {"items": sent, "flush": flush_pending}


def _patch_welcome_email_capture(monkeypatch, *, send_ok: bool = True):
    import core.direct_welcome_email as welcome_mod  # noqa: PLC0415

    sent: list[dict] = []
    pending: list = []

    async def fake_send_email(*, to, subject, html):
        sent.append({"to": to, "subject": subject, "html": html})
        return send_ok

    def fake_ensure_future(coro):
        pending.append(coro)

    def flush_pending():
        if not pending:
            return
        loop = asyncio.new_event_loop()
        try:
            while pending:
                coro = pending.pop(0)
                loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr("core.notifications.send_email", fake_send_email)
    monkeypatch.setattr(welcome_mod.asyncio, "ensure_future", fake_ensure_future)
    monkeypatch.setattr("core.database.SessionLocal", lambda: _current_test_db["session"]())
    return {"items": sent, "flush": flush_pending}


_current_test_db: dict = {}


def _tenant_settings(db, tenant_id: int) -> TenantSettings | None:
    return db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setattr(auth_router, "BCRYPT_AVAILABLE", True)
    monkeypatch.setattr(auth_router, "JWT_AVAILABLE", True)
    monkeypatch.setattr(auth_router, "REQUIRE_INVITE", False)
    monkeypatch.setattr(auth_router, "DASHBOARD_URL", "https://app.nahlah.ai")
    monkeypatch.setattr(auth_router, "audit", lambda *a, **k: None)
    monkeypatch.setattr("core.audit.audit", lambda *a, **k: None)


class TestDirectRegister:
    def test_register_sends_verification_not_welcome(self, monkeypatch, auth_env):
        db, engine = _make_db()
        try:
            sent = _patch_register_email_capture(monkeypatch)
            monkeypatch.setattr(auth_router, "get_db", lambda: iter([db]))

            body = auth_router.RegisterIn(
                email="new@example.com",
                password="secret123",
                store_name="My Shop",
            )
            result = _run_async(auth_router.auth_register(body, _fake_request(), db))
            sent["flush"]()

            assert result["email_verified"] is False
            assert len(sent["items"]) == 1
            assert sent["items"][0]["to"] == "new@example.com"
            assert "أكّد بريدك" in sent["items"][0]["subject"]
            assert "verify-email" in sent["items"][0]["html"]
            assert "مرحباً بك في نحلة الذكية" not in sent["items"][0]["html"]
        finally:
            db.close()
            engine.dispose()

    def test_register_duplicate_email_returns_409(self, monkeypatch, auth_env):
        db, engine = _make_db()
        try:
            tenant = Tenant(name="Existing Shop", is_active=True)
            db.add(tenant)
            db.flush()
            db.add(User(
                username="dup@example.com",
                email="dup@example.com",
                password_hash="x",
                role="merchant",
                is_active=True,
                tenant_id=tenant.id,
            ))
            db.commit()

            _patch_register_email_capture(monkeypatch)

            body = auth_router.RegisterIn(
                email="dup@example.com",
                password="secret123",
                store_name="Another Shop",
            )
            with pytest.raises(HTTPException) as exc:
                _run_async(auth_router.auth_register(body, _fake_request(), db))
            assert exc.value.status_code == 409
        finally:
            db.close()
            engine.dispose()

    def test_register_page_redirects_to_pending_verify(self):
        src = (REPO_ROOT / "dashboard" / "src" / "pages" / "Register.tsx").read_text(
            encoding="utf-8",
        )
        assert "navigate('/verify-email?status=pending'" in src


class TestDirectVerifyEmail:
    def _seed_user(
        self,
        db,
        *,
        email: str = "merchant@example.com",
        verified: bool = False,
        welcome_sent: bool = False,
    ):
        tenant = Tenant(name="Shop One", is_active=True)
        db.add(tenant)
        db.flush()
        user = User(
            username=email,
            email=email,
            password_hash=hash_password("secret123"),
            role="merchant",
            is_active=True,
            email_verified=verified,
            tenant_id=tenant.id,
        )
        db.add(user)
        if welcome_sent:
            db.add(TenantSettings(
                tenant_id=tenant.id,
                notification_settings={_WELCOME_FLAG: "2026-01-01T00:00:00+00:00"},
            ))
        db.commit()
        db.refresh(user)
        return user

    def test_first_verify_sends_welcome_once(self, monkeypatch, auth_env):
        db, engine = _make_db()
        _current_test_db["session"] = sessionmaker(bind=engine)
        try:
            email = "merchant@example.com"
            user = self._seed_user(db, email=email, verified=False)
            sent = _patch_welcome_email_capture(monkeypatch)

            token = create_verify_token(email)
            resp = _run_async(auth_router.verify_email(token, db))
            sent["flush"]()

            assert isinstance(resp, RedirectResponse)
            assert resp.headers["location"].endswith("status=success")
            assert len(sent["items"]) == 1
            assert sent["items"][0]["to"] == email
            assert "نحلة الذكية" in sent["items"][0]["subject"]
            assert "https://app.nahlah.ai" in sent["items"][0]["html"]
            assert email in sent["items"][0]["html"]
            assert "كلمة المرور التي اخترتها" in sent["items"][0]["html"]
            assert "سلة" not in sent["items"][0]["html"].lower()

            user = db.query(User).filter(User.email == email).one()
            assert user.email_verified is True
            ts = _tenant_settings(db, user.tenant_id)
            assert ts is not None
            assert welcome_email_already_sent(ts.notification_settings)
        finally:
            db.close()
            engine.dispose()
            _current_test_db.clear()

    def test_first_verify_welcome_failure_leaves_dedupe_unset(self, monkeypatch, auth_env):
        db, engine = _make_db()
        _current_test_db["session"] = sessionmaker(bind=engine)
        try:
            email = "fail@example.com"
            self._seed_user(db, email=email, verified=False)
            sent = _patch_welcome_email_capture(monkeypatch, send_ok=False)

            token = create_verify_token(email)
            resp = _run_async(auth_router.verify_email(token, db))
            sent["flush"]()

            assert isinstance(resp, RedirectResponse)
            assert resp.headers["location"].endswith("status=success")
            assert len(sent["items"]) == 1

            user = db.query(User).filter(User.email == email).one()
            assert user.email_verified is True
            ts = _tenant_settings(db, user.tenant_id)
            assert ts is None or not welcome_email_already_sent(ts.notification_settings)
        finally:
            db.close()
            engine.dispose()
            _current_test_db.clear()

    def test_repeat_verify_retries_welcome_after_failure(self, monkeypatch, auth_env):
        db, engine = _make_db()
        _current_test_db["session"] = sessionmaker(bind=engine)
        try:
            email = "retry@example.com"
            self._seed_user(db, email=email, verified=False)

            fail_sent = _patch_welcome_email_capture(monkeypatch, send_ok=False)
            token = create_verify_token(email)
            _run_async(auth_router.verify_email(token, db))
            fail_sent["flush"]()

            ok_sent = _patch_welcome_email_capture(monkeypatch, send_ok=True)
            _run_async(auth_router.verify_email(token, db))
            ok_sent["flush"]()

            assert len(fail_sent["items"]) == 1
            assert len(ok_sent["items"]) == 1

            user = db.query(User).filter(User.email == email).one()
            ts = _tenant_settings(db, user.tenant_id)
            assert ts is not None
            assert welcome_email_already_sent(ts.notification_settings)
        finally:
            db.close()
            engine.dispose()
            _current_test_db.clear()

    def test_repeat_verify_skips_welcome_after_success(self, monkeypatch, auth_env):
        db, engine = _make_db()
        _current_test_db["session"] = sessionmaker(bind=engine)
        try:
            email = "verified@example.com"
            self._seed_user(db, email=email, verified=True, welcome_sent=True)
            sent = _patch_welcome_email_capture(monkeypatch)

            token = create_verify_token(email)
            resp = _run_async(auth_router.verify_email(token, db))
            sent["flush"]()

            assert isinstance(resp, RedirectResponse)
            assert resp.headers["location"].endswith("status=success")
            assert sent["items"] == []

            user = db.query(User).filter(User.email == email).one()
            assert user.email_verified is True
        finally:
            db.close()
            engine.dispose()
            _current_test_db.clear()


class TestSallaIsolation:
    def test_verify_email_does_not_use_salla_onboarding(self, monkeypatch, auth_env):
        db, engine = _make_db()
        _current_test_db["session"] = sessionmaker(bind=engine)
        try:
            tenant = Tenant(name="Salla Shop", is_active=True)
            db.add(tenant)
            db.flush()
            email = "salla-owner@example.com"
            db.add(User(
                username=email,
                email=email,
                password_hash=hash_password("secret123"),
                role="merchant",
                is_active=True,
                email_verified=False,
                tenant_id=tenant.id,
            ))
            db.commit()

            sent = _patch_welcome_email_capture(monkeypatch)
            salla_calls = {"n": 0}

            def fake_queue_salla_onboarding_email(**kwargs):
                salla_calls["n"] += 1

            monkeypatch.setattr(
                "core.salla_onboarding_email.queue_salla_onboarding_email",
                fake_queue_salla_onboarding_email,
            )

            token = create_verify_token(email)
            _run_async(auth_router.verify_email(token, db))
            sent["flush"]()

            assert salla_calls["n"] == 0
            assert len(sent["items"]) == 1
            assert "تم ربط متجرك" not in sent["items"][0]["subject"]
            assert "set-password" not in sent["items"][0]["html"]
        finally:
            db.close()
            engine.dispose()
            _current_test_db.clear()

    def test_salla_onboarding_module_unchanged_by_direct_welcome(self):
        from core.notifications import email_welcome  # noqa: PLC0415

        html = email_welcome("Shop", "https://app.nahlah.ai", "owner@example.com")
        assert "سلة" not in html.lower()
        assert "password" not in html.lower()
