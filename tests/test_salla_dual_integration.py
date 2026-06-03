"""
Dual Integration Architecture — registry scoring + token-login flags.

Verifies:
  1. pick_active_salla_integration prefers api_sync_enabled + refresh_token
     over Easy Mode and embedded-token rows.
  2. Easy Mode still beats plain embedded-token rows.
  3. Embedded-token Communication App sessions do NOT trigger needs_oauth.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from database.models import Base, Tenant, Integration  # noqa: E402


@event.listens_for(Base.metadata, "before_create")
def _remap_jsonb(target, connection, **kw):
    for table in target.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = __import__("sqlalchemy", fromlist=["JSON"]).JSON()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _tenant(db, tid: int = 1):
    t = Tenant(id=tid, name=f"Tenant {tid}")
    db.add(t)
    db.commit()
    return t


def _row(db, *, config: dict, enabled: bool = True, row_id: int | None = None):
    store_id = config.get("store_id") or f"STORE{row_id or 1}"
    config = {**config, "store_id": store_id}
    kwargs = dict(
        tenant_id=1,
        provider="salla",
        external_store_id=store_id,
        config=config,
        enabled=enabled,
    )
    if row_id is not None:
        kwargs["id"] = row_id
    i = Integration(**kwargs)
    db.add(i)
    db.commit()
    db.refresh(i)
    return i


class TestPickActiveSallaIntegration:
    def test_api_sync_beats_embedded_and_easy_mode(self, db):
        from store_integration.registry import pick_active_salla_integration

        _tenant(db)
        embedded = _row(
            db,
            config={
                "api_key": "embedded-session",
                "api_key_source": "embedded_token",
            },
            row_id=1,
        )
        easy = _row(
            db,
            config={
                "api_key": "easy-key",
                "refresh_token": "easy-refresh",
                "app_type": "easy",
                "api_key_source": "easy_mode_webhook",
            },
            row_id=2,
        )
        sync = _row(
            db,
            config={
                "api_key": "sync-access",
                "refresh_token": "sync-refresh",
                "api_sync_enabled": True,
                "api_canonical": True,
                "app_type": "custom_oauth_sync",
                "api_key_source": "custom_oauth_sync",
            },
            row_id=3,
        )

        winner = pick_active_salla_integration(db, 1)
        assert winner is not None
        assert winner.id == sync.id
        assert (winner.config or {}).get("api_sync_enabled") is True

        # Sanity: losers exist but were not picked
        assert embedded.id != winner.id
        assert easy.id != winner.id

    def test_easy_mode_beats_embedded_only(self, db):
        from store_integration.registry import pick_active_salla_integration

        _tenant(db)
        _row(
            db,
            config={
                "api_key": "embedded-session",
                "api_key_source": "embedded_token",
            },
            row_id=10,
        )
        easy = _row(
            db,
            config={
                "api_key": "easy-key",
                "refresh_token": "easy-refresh",
                "app_type": "easy",
                "api_key_source": "easy_mode_webhook",
            },
            row_id=11,
        )

        winner = pick_active_salla_integration(db, 1)
        assert winner.id == easy.id

    def test_disabled_api_sync_loses_to_enabled_easy_mode(self, db):
        from store_integration.registry import pick_active_salla_integration

        _tenant(db)
        _row(
            db,
            config={
                "api_key": "sync-access",
                "refresh_token": "sync-refresh",
                "api_sync_enabled": True,
            },
            enabled=False,
            row_id=20,
        )
        easy = _row(
            db,
            config={
                "api_key": "easy-key",
                "refresh_token": "easy-refresh",
                "app_type": "easy",
            },
            row_id=21,
        )

        winner = pick_active_salla_integration(db, 1)
        assert winner.id == easy.id


class TestScoreIntegrationTuple:
    def test_api_sync_outscores_easy_mode(self):
        from store_integration.registry import _score_integration

        api_sync = Integration(
            tenant_id=1,
            provider="salla",
            enabled=True,
            config={
                "api_sync_enabled": True,
                "refresh_token": "rt",
                "api_key": "ak",
            },
        )
        easy = Integration(
            tenant_id=1,
            provider="salla",
            enabled=True,
            config={
                "app_type": "easy",
                "refresh_token": "rt",
                "api_key": "ak",
            },
        )
        assert _score_integration(api_sync) > _score_integration(easy)

    def test_needs_reauth_always_loses(self):
        from store_integration.registry import _score_integration

        broken = Integration(
            tenant_id=1,
            provider="salla",
            enabled=True,
            config={
                "api_sync_enabled": True,
                "refresh_token": "rt",
                "needs_reauth": True,
            },
        )
        healthy_embedded = Integration(
            tenant_id=1,
            provider="salla",
            enabled=True,
            config={"api_key": "embedded", "api_key_source": "embedded_token"},
        )
        assert _score_integration(broken) < _score_integration(healthy_embedded)


class TestTokenLoginFlagsLogic:
    """Uses the same helper as /salla/token-login."""

    @staticmethod
    def _derive_flags(cfg: dict, *, enabled: bool = True) -> tuple[bool, bool]:
        from store_integration.salla_login_flags import (
            derive_salla_login_integration_flags,
        )
        return derive_salla_login_integration_flags(cfg, enabled=enabled)

    def test_embedded_token_needs_api_sync_not_oauth(self):
        needs_oauth, needs_api_sync = self._derive_flags(
            {"api_key": "sess", "api_key_source": "embedded_token"},
        )
        assert needs_oauth is False
        assert needs_api_sync is True

    def test_api_sync_done_clears_needs_api_sync(self):
        needs_oauth, needs_api_sync = self._derive_flags(
            {
                "api_key": "ak",
                "refresh_token": "rt",
                "api_sync_enabled": True,
                "api_key_source": "custom_oauth_sync",
            },
        )
        assert needs_oauth is False
        assert needs_api_sync is False

    def test_legacy_custom_without_refresh_still_needs_oauth(self):
        needs_oauth, needs_api_sync = self._derive_flags(
            {"api_key": "ak", "api_key_source": "manual"},
        )
        assert needs_oauth is True
        assert needs_api_sync is True
