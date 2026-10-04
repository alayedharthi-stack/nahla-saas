"""Invitation / verification / reset links follow the configured dashboard URL
(``DASHBOARD_URL``) so a review or staging deploy never hands out a production
link; the production default stays ``https://app.nahlah.ai``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)


def test_admin_invite_link_uses_configured_dashboard_url():
    src = (_REPO / "backend" / "routers" / "admin.py").read_text(encoding="utf-8")
    assert 'f"https://app.nahlah.ai/register?invite=' not in src
    assert "from core.config import DASHBOARD_URL, INVITE_EXPIRE_H" in src
    assert re.search(r'invite_url = f"\{str\(DASHBOARD_URL or .https://app\.nahlah\.ai.\)\.rstrip\(./.\)\}/register\?invite=\{token\}"', src)


def test_default_dashboard_url_is_production_when_unset(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    import importlib
    import core.config as cfg

    importlib.reload(cfg)
    assert cfg.DASHBOARD_URL == "https://app.nahlah.ai"


def test_review_dashboard_url_overrides_default(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://catalog-review.nahlah.ai")
    import importlib
    import core.config as cfg

    importlib.reload(cfg)
    assert cfg.DASHBOARD_URL == "https://catalog-review.nahlah.ai"
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    importlib.reload(cfg)


def test_auth_links_already_follow_dashboard_url():
    src = (_REPO / "backend" / "routers" / "auth.py").read_text(encoding="utf-8")
    assert 'f"{DASHBOARD_URL}/verify-email?token=' in src
    assert 'f"{DASHBOARD_URL}/reset-password?token=' in src
    assert "https://app.nahlah.ai/verify-email" not in src and "https://app.nahlah.ai/reset-password" not in src
