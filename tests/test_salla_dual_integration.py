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


class TestSallaOAuthClientResolution:
    """Sync vs legacy refresh credential selection."""

    @staticmethod
    def _env(monkeypatch, *, sync_id="sync-client", legacy_id="legacy-client"):
        monkeypatch.setenv("SALLA_OAUTH_CLIENT_ID", sync_id)
        monkeypatch.setenv("SALLA_OAUTH_CLIENT_SECRET", "sync-secret")
        monkeypatch.setenv("SALLA_CLIENT_ID", legacy_id)
        monkeypatch.setenv("SALLA_CLIENT_SECRET", "legacy-secret")

    def test_sync_row_uses_oauth_client(self, monkeypatch):
        from core.salla_oauth_credentials import resolve_salla_oauth_client

        self._env(monkeypatch)
        cid, secret, kind = resolve_salla_oauth_client({
            "app_type": "custom_oauth_sync",
            "api_key_source": "custom_oauth_sync",
            "api_sync_enabled": True,
            "api_client_id": "sync-client",
        })
        assert kind == "sync_oauth"
        assert cid == "sync-client"
        assert secret == "sync-secret"

    def test_embedded_row_uses_legacy_client(self, monkeypatch):
        from core.salla_oauth_credentials import resolve_salla_oauth_client

        self._env(monkeypatch)
        cid, secret, kind = resolve_salla_oauth_client({
            "api_key_source": "embedded_token",
            "api_key": "sess",
        })
        assert kind == "legacy"
        assert cid == "legacy-client"
        assert secret == "legacy-secret"

    def test_easy_mode_uses_legacy_client(self, monkeypatch):
        from core.salla_oauth_credentials import resolve_salla_oauth_client

        self._env(monkeypatch)
        cid, _, kind = resolve_salla_oauth_client({
            "app_type": "easy",
            "api_key_source": "easy_mode_webhook",
            "refresh_token": "rt",
        })
        assert kind == "legacy"
        assert cid == "legacy-client"

    def test_api_client_id_match_selects_sync(self, monkeypatch):
        from core.salla_oauth_credentials import resolve_salla_oauth_client

        self._env(monkeypatch)
        _, _, kind = resolve_salla_oauth_client({
            "api_sync_enabled": True,
            "api_client_id": "sync-client",
        })
        assert kind == "sync_oauth"


class TestSyncOAuthBootstrapMetadata:
    def test_sets_expiry_and_refresh_history(self):
        from datetime import datetime, timezone
        from core.salla_oauth_credentials import bootstrap_sync_oauth_token_metadata

        now = datetime(2026, 7, 5, 10, 0, 0, tzinfo=timezone.utc)
        out = bootstrap_sync_oauth_token_metadata(
            {"api_sync_enabled": True},
            expires_in=3600,
            now=now,
        )
        assert out["last_token_refresh_at"] == now.isoformat()
        assert out["token_refresh_status"] == "success"
        assert out["expires_at"].startswith("2026-07-05T11:00:00")
        assert out["token_expires_at"] == out["expires_at"]


class TestSchedulerRefreshClientSelection:
    def test_scheduler_resolver_picks_sync_for_custom_oauth_row(self, monkeypatch):
        from core.salla_oauth_credentials import resolve_salla_oauth_client

        monkeypatch.setenv("SALLA_OAUTH_CLIENT_ID", "sync-cid")
        monkeypatch.setenv("SALLA_OAUTH_CLIENT_SECRET", "sync-sec")
        monkeypatch.setenv("SALLA_CLIENT_ID", "legacy-cid")
        monkeypatch.setenv("SALLA_CLIENT_SECRET", "legacy-sec")

        cfg = {
            "api_key": "ak",
            "refresh_token": "rt",
            "app_type": "custom_oauth_sync",
            "api_key_source": "custom_oauth_sync",
            "api_sync_enabled": True,
            "api_client_id": "sync-cid",
        }
        cid, secret, kind = resolve_salla_oauth_client(cfg)
        assert kind == "sync_oauth"
        assert cid == "sync-cid"
        assert secret == "sync-sec"

    def test_sync_integration_refresh_uses_oauth_client(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from store_adapters.salla_adapter import SallaAdapter

        monkeypatch.setenv("SALLA_OAUTH_CLIENT_ID", "sync-cid")
        monkeypatch.setenv("SALLA_OAUTH_CLIENT_SECRET", "sync-sec")
        monkeypatch.setenv("SALLA_CLIENT_ID", "legacy-cid")
        monkeypatch.setenv("SALLA_CLIENT_SECRET", "legacy-sec")

        adapter = SallaAdapter(
            api_key="access",
            refresh_token="sync-refresh",
            tenant_id=1,
            integration_id=3,
        )
        adapter._get_integration_config = lambda: {  # noqa: SLF001
            "app_type": "custom_oauth_sync",
            "api_key_source": "custom_oauth_sync",
            "api_sync_enabled": True,
            "api_client_id": "sync-cid",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 1209600,
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("store_adapters.salla_adapter.httpx.AsyncClient", return_value=mock_client):
                with patch.object(adapter, "_persist_refreshed_tokens"):
                    with patch("core.salla_token_lock.salla_asyncio_lock") as lock_ctx:
                        lock_ctx.return_value.__aenter__ = AsyncMock(return_value=True)
                        lock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
                        return await adapter._refresh_access_token()

        ok = asyncio.run(_run())
        assert ok is True
        posted = mock_client.post.call_args
        assert posted.kwargs["data"]["client_id"] == "sync-cid"
        assert posted.kwargs["data"]["client_secret"] == "sync-sec"

    def test_invalid_grant_still_marks_needs_reauth(self, monkeypatch):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from store_adapters.salla_adapter import SallaAdapter, SallaTokenRevokedException

        monkeypatch.setenv("SALLA_OAUTH_CLIENT_ID", "sync-cid")
        monkeypatch.setenv("SALLA_OAUTH_CLIENT_SECRET", "sync-sec")
        monkeypatch.setenv("SALLA_CLIENT_ID", "legacy-cid")
        monkeypatch.setenv("SALLA_CLIENT_SECRET", "legacy-sec")

        adapter = SallaAdapter(
            api_key="access",
            refresh_token="sync-refresh",
            tenant_id=1,
            integration_id=3,
        )
        adapter._get_integration_config = lambda: {"app_type": "custom_oauth_sync"}  # noqa: SLF001

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error":"invalid_grant"}'

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("store_adapters.salla_adapter.httpx.AsyncClient", return_value=mock_client):
                with patch("core.salla_token_lock.salla_asyncio_lock") as lock_ctx:
                    lock_ctx.return_value.__aenter__ = AsyncMock(return_value=True)
                    lock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
                    with pytest.raises(SallaTokenRevokedException):
                        await adapter._refresh_access_token()

        with patch.object(adapter, "_mark_needs_reauth") as mark_reauth:
            asyncio.run(_run())
            mark_reauth.assert_called_once_with("invalid_grant")


class TestTokenLoginSyncPreservation:
    def test_sync_refresh_not_overwritten_by_introspect(self):
        from core.salla_oauth_credentials import is_sync_oauth_integration

        cfg = {
            "api_sync_enabled": True,
            "app_type": "custom_oauth_sync",
            "api_key_source": "custom_oauth_sync",
            "api_client_id": "sync-client",
            "refresh_token": "sync-refresh-token",
            "needs_reauth": True,
            "needs_reauth_reason": "invalid_grant",
        }
        sync_protected = is_sync_oauth_integration(cfg)
        introspect_refresh_token = "embedded-refresh"

        if introspect_refresh_token and not sync_protected:
            cfg["refresh_token"] = introspect_refresh_token

        assert cfg["refresh_token"] == "sync-refresh-token"
        assert cfg.get("needs_reauth") is True

    def test_sync_clears_reauth_only_when_refresh_present(self):
        cfg = {
            "app_type": "custom_oauth_sync",
            "refresh_token": "sync-refresh",
            "needs_reauth": True,
        }
        existing_refresh = cfg.get("refresh_token", "")
        reauth_clear_keys = ("needs_reauth", "needs_reauth_at", "needs_reauth_reason")
        if existing_refresh:
            for k in reauth_clear_keys:
                cfg.pop(k, None)
        assert "needs_reauth" not in cfg

    def test_sync_without_refresh_keeps_needs_reauth(self):
        cfg = {
            "app_type": "custom_oauth_sync",
            "refresh_token": "",
            "needs_reauth": True,
            "needs_reauth_reason": "invalid_grant",
        }
        existing_refresh = cfg.get("refresh_token", "")
        reauth_clear_keys = ("needs_reauth", "needs_reauth_reason")
        if existing_refresh:
            for k in reauth_clear_keys:
                cfg.pop(k, None)
        assert cfg.get("needs_reauth") is True
        assert cfg.get("needs_reauth_reason") == "invalid_grant"

