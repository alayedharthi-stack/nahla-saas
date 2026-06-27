"""Tests for Salla onboarding email helpers."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (REPO_ROOT, BACKEND_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.salla_onboarding_email import (  # noqa: E402
    is_deliverable_merchant_email,
    onboarding_email_already_sent,
    queue_salla_onboarding_email,
)


def test_deliverable_real_email():
    assert is_deliverable_merchant_email("owner@example.com") is True


def test_skip_derived_salla_email():
    assert is_deliverable_merchant_email("store-123@salla-merchant.nahlah.ai") is False


def test_skip_empty_email():
    assert is_deliverable_merchant_email("") is False
    assert is_deliverable_merchant_email("not-an-email") is False


def test_onboarding_flag_detected():
    assert onboarding_email_already_sent({"onboarding_email_sent_at": "2026-01-01T00:00:00+00:00"})
    assert not onboarding_email_already_sent({})
    assert not onboarding_email_already_sent(None)


def test_send_new_user_email_marks_integration(monkeypatch):
    import asyncio
    from sqlalchemy import create_engine, JSON
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.orm import sessionmaker

    from models import Base, Integration, Tenant  # noqa: E402

    sent = {}

    async def fake_send_email(*, to, subject, html):
        sent["to"] = to
        sent["subject"] = subject
        sent["html"] = html
        return True

    monkeypatch.setattr("core.notifications.send_email", fake_send_email)
    monkeypatch.setattr("core.audit.audit", lambda *a, **k: None)

    engine = create_engine("sqlite:///:memory:")
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    tenant = Tenant(name="Shop-1")
    db.add(tenant)
    db.flush()
    integration = Integration(
        tenant_id=tenant.id,
        provider="salla",
        external_store_id="99",
        config={"store_id": "99"},
        enabled=True,
    )
    db.add(integration)
    db.commit()

    monkeypatch.setattr("core.database.SessionLocal", lambda: Session())

    from core.salla_onboarding_email import _send_and_mark

    asyncio.run(_send_and_mark(
        email="owner@example.com",
        store_name="Shop",
        dashboard_url="https://app.nahlah.ai",
        set_password_url="https://app.nahlah.ai/set-password?token=abc",
        integration_id=integration.id,
        tenant_id=tenant.id,
        user_id=1,
    ))

    assert sent["to"] == "owner@example.com"
    assert "تم ربط متجرك" in sent["subject"]
    assert "set-password" in sent["html"]

    db.expire_all()
    refreshed = db.query(Integration).filter_by(id=integration.id).one()
    assert refreshed.config.get("onboarding_email_sent_at")
    db.close()


def test_existing_user_email_without_password_link(monkeypatch):
    import asyncio

    captured = {}

    async def fake_send_email(*, to, subject, html):
        captured["html"] = html
        return True

    class _DummySession:
        def close(self):
            pass

    monkeypatch.setattr("core.notifications.send_email", fake_send_email)
    monkeypatch.setattr("core.audit.audit", lambda *a, **k: None)
    monkeypatch.setattr("core.database.SessionLocal", _DummySession)

    from core.salla_onboarding_email import _send_and_mark

    asyncio.run(_send_and_mark(
        email="owner@example.com",
        store_name="Shop",
        dashboard_url="https://app.nahlah.ai",
        set_password_url=None,
        integration_id=None,
        tenant_id=1,
        user_id=2,
    ))

    assert "set-password" not in captured["html"]
    assert "بيانات دخولك الحالية" in captured["html"]


def test_queue_skips_derived_email(monkeypatch):
    called = {"n": 0}

    def fake_ensure_future(coro):
        called["n"] += 1

    monkeypatch.setattr("core.salla_onboarding_email.asyncio.ensure_future", fake_ensure_future)

    queue_salla_onboarding_email(
        email="x@salla-merchant.nahlah.ai",
        store_name="Shop",
        dashboard_url="https://app.nahlah.ai",
        set_password_url="https://app.nahlah.ai/set-password?token=x",
        integration_id=1,
        tenant_id=1,
        user_id=1,
    )
    assert called["n"] == 0
