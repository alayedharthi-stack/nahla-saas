"""Invitation / verification / reset links follow the configured dashboard URL
(``DASHBOARD_URL``) so a review or staging deploy never hands out a production
link; the production default stays ``https://app.nahlah.ai``.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from routers import admin as admin_router  # noqa: E402


def _invite(monkeypatch, dashboard_url: str) -> dict:
    monkeypatch.setattr(admin_router, "DASHBOARD_URL", dashboard_url)
    monkeypatch.setattr(admin_router, "JWT_AVAILABLE", True)
    monkeypatch.setattr(admin_router, "create_invite_token", lambda email: "tok-123")
    monkeypatch.setattr(admin_router, "audit", lambda *a, **k: None)
    body = SimpleNamespace(email="Merchant@Example.com ")
    return asyncio.run(admin_router.create_invitation(body, request=None, _admin={"sub": "admin@test"}))


def test_invite_link_uses_review_dashboard_url(monkeypatch):
    out = _invite(monkeypatch, "https://catalog-review.nahlah.ai")
    assert out["invite_url"] == "https://catalog-review.nahlah.ai/register?invite=tok-123"
    assert out["invited_email"] == "merchant@example.com"


def test_invite_link_strips_trailing_slash(monkeypatch):
    out = _invite(monkeypatch, "https://catalog-review.nahlah.ai/")
    assert out["invite_url"] == "https://catalog-review.nahlah.ai/register?invite=tok-123"


def test_invite_link_production_default_unchanged(monkeypatch):
    out = _invite(monkeypatch, "https://app.nahlah.ai")
    assert out["invite_url"] == "https://app.nahlah.ai/register?invite=tok-123"
    assert "https://app.nahlah.ai/register?invite=" not in (_REPO / "backend" / "routers" / "admin.py").read_text(encoding="utf-8").replace("'https://app.nahlah.ai'", "")


def test_config_default_dashboard_url_is_production():
    src = (_REPO / "backend" / "core" / "config.py").read_text(encoding="utf-8")
    assert 'DASHBOARD_URL  = os.environ.get("DASHBOARD_URL", "https://app.nahlah.ai")' in src


def test_auth_links_already_follow_dashboard_url():
    src = (_REPO / "backend" / "routers" / "auth.py").read_text(encoding="utf-8")
    assert 'f"{DASHBOARD_URL}/verify-email?token=' in src
    assert 'f"{DASHBOARD_URL}/reset-password?token=' in src
    assert "https://app.nahlah.ai/verify-email" not in src and "https://app.nahlah.ai/reset-password" not in src
