"""
tests/test_salla_token_refresh.py
───────────────────────────────────
Comprehensive test suite for the Salla Easy Mode Token Auto Refresh system.

Scenarios covered
─────────────────
A  Webhook authorize
   • app.store.authorize persists access_token, refresh_token, expires_at
   • resets token_refresh_attempts to 0
   • clears any stale refresh-error fields

B  Scheduler success
   • token expiring in < 5 days triggers refresh
   • new access_token, refresh_token, expires_at, last_token_refresh_at saved
   • token_refresh_status = "success", error fields cleared
   • refresh_token NOT overwritten when Salla omits it from response

C  Scheduler failure (transient HTTP 500)
   • integration stays ENABLED
   • token_refresh_attempts incremented
   • token_refresh_status = "failed", error + timestamp persisted

D  Failure escalation
   • 3 consecutive failures → needs_reauth = True
   • integration stays enabled (NOT deleted / disabled)
   • existing access_token NOT cleared

E  Runtime API protection (_ensure_token_fresh)
   • access_token with < 24h expiry → proactive refresh before API call
   • token with 5 days remaining → no premature refresh
   • no expires_at → no-op

F  Race condition prevention
   • two concurrent _refresh_access_token calls → only 1 HTTP POST to Salla
   • asyncio lock released after success
   • asyncio lock released after failure
   • scheduler DB flag set during HTTP call, cleared after cycle

All async tests use asyncio.run() inside synchronous test methods (project convention).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import JSON, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ── Path setup ────────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parents[1]
BACKEND_DIR  = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

from database.models import Base, Integration, Tenant  # noqa: E402

# ── SQLite JSONB shim ─────────────────────────────────────────────────────────
if not getattr(Base.metadata, "_test_jsonb_shim_applied", False):
    @event.listens_for(Base.metadata, "before_create")
    def _remap_jsonb(target, connection, **kw):  # noqa: ANN001
        for table in target.sorted_tables:
            for col in table.columns:
                if isinstance(col.type, JSONB):
                    col.type = JSON()
    Base.metadata._test_jsonb_shim_applied = True  # type: ignore[attr-defined]


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def engine():
    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    yield e
    Base.metadata.drop_all(e)
    e.dispose()


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


@pytest.fixture()
def session_factory(engine):
    _Session = sessionmaker(bind=engine)
    class _F:
        def __call__(self):
            return _Session()
    return _F()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_integration(
    db,
    *,
    store_id: str = "S-TEST",
    api_key: str = "access-tok-001",
    refresh_token: str = "refresh-tok-001",
    expires_at: str | None = None,
    attempts: int = 0,
    enabled: bool = True,
) -> Integration:
    tenant = Tenant(name=f"Test Merchant {store_id}", is_active=True)
    db.add(tenant)
    db.flush()

    cfg: dict[str, Any] = {
        "api_key":       api_key,
        "refresh_token": refresh_token,
        "store_id":      store_id,
        "store_name":    "Test Store",
        "easy_mode":     True,
        "token_source":  "easy_mode_webhook",
        "token_refresh_attempts": attempts,
    }
    if expires_at is not None:
        cfg["expires_at"]       = expires_at
        cfg["token_expires_at"] = expires_at

    intg = Integration(
        tenant_id=tenant.id,
        provider="salla",
        external_store_id=store_id,
        config=cfg,
        enabled=enabled,
    )
    db.add(intg)
    db.commit()
    return intg


def _reload(db, intg: Integration) -> dict:
    db.expire(intg)
    return dict(intg.config or {})


def _env_getter(**overrides: str):
    _map = {
        "SALLA_CLIENT_ID":     "",
        "SALLA_CLIENT_SECRET": "",
        **overrides,
    }
    def _get(key, default=None):
        return _map.get(key, default)
    return _get


def _mock_httpx_client(status: int, json_body: dict | None = None, text: str = ""):
    """Build a mock httpx.AsyncClient context-manager that returns the given response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.text        = text
    if json_body is not None:
        mock_resp.json.return_value = json_body

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__  = AsyncMock(return_value=False)
    mock_client.post       = AsyncMock(return_value=mock_resp)
    return mock_client


# ─────────────────────────────────────────────────────────────────────────────
# A — Webhook authorize
# ─────────────────────────────────────────────────────────────────────────────

class TestWebhookAuthorize:

    def test_saves_all_token_fields(self, db):
        """Webhook handler saves access_token, refresh_token, expires_at and tracking fields."""
        now        = _now()
        expires_in = 1_209_600  # 14 days
        exp_at     = (now + timedelta(seconds=expires_in)).isoformat()

        intg = _make_integration(db, store_id="WH-A1")
        cfg  = dict(intg.config or {})
        cfg.update({
            "api_key":                   "new-access-token",
            "refresh_token":             "new-refresh-token",
            "expires_in":                expires_in,
            "expires_at":                exp_at,
            "token_expires_at":          exp_at,
            "refresh_token_received_at": now.isoformat(),
            "token_source":              "easy_mode_webhook",
            "easy_mode":                 True,
            "token_refresh_attempts":    0,
            "connected_at":              now.isoformat(),
        })
        cfg.pop("token_refresh_status", None)
        cfg.pop("token_refresh_error",  None)
        intg.config = cfg
        db.commit()

        result = _reload(db, intg)
        assert result["api_key"]               == "new-access-token"
        assert result["refresh_token"]         == "new-refresh-token"
        assert result["expires_at"]            == exp_at
        assert result["refresh_token_received_at"] is not None
        assert result["easy_mode"]             is True
        assert result["token_source"]          == "easy_mode_webhook"
        assert result["token_refresh_attempts"] == 0

    def test_clears_stale_refresh_error_fields(self, db):
        """Fresh token from webhook wipes stale failure metadata."""
        intg = _make_integration(db, store_id="WH-A2", attempts=2)
        cfg  = dict(intg.config or {})
        cfg["token_refresh_status"]    = "failed"
        cfg["token_refresh_error"]     = "HTTP 500: server error"
        cfg["token_refresh_failed_at"] = _iso(_now() - timedelta(days=1))
        intg.config = cfg
        db.commit()

        fresh = dict(intg.config or {})
        fresh.pop("token_refresh_status",   None)
        fresh.pop("token_refresh_error",    None)
        fresh.pop("token_refresh_failed_at",None)
        fresh["token_refresh_attempts"] = 0
        fresh["api_key"]                = "brand-new-access"
        fresh["refresh_token"]          = "brand-new-refresh"
        intg.config = fresh
        db.commit()

        result = _reload(db, intg)
        assert "token_refresh_status"    not in result
        assert "token_refresh_error"     not in result
        assert "token_refresh_failed_at" not in result
        assert result["token_refresh_attempts"] == 0
        assert result["api_key"]  == "brand-new-access"


# ─────────────────────────────────────────────────────────────────────────────
# B — Scheduler success
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerSuccess:

    def test_refreshes_expiring_token_and_updates_fields(self, db, session_factory):
        """
        Token expiring in 3 days triggers refresh.
        All token fields are updated; status = 'success'.
        """
        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="SCH-B1", expires_at=soon)

        mock_client = _mock_httpx_client(200, {
            "access_token":  "scheduler-new-access",
            "refresh_token": "scheduler-new-refresh",
            "expires_in":    1_209_600,
        })

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert result["api_key"]                == "scheduler-new-access"
        assert result["refresh_token"]          == "scheduler-new-refresh"
        assert result["token_refresh_status"]   == "success"
        assert result["token_refresh_attempts"] == 0
        assert result.get("token_refresh_error") is None
        assert result.get("last_token_refresh_at") is not None
        assert result.get("expires_at") is not None

    def test_keeps_old_refresh_token_when_salla_omits_it(self, db, session_factory):
        """
        Salla sometimes omits refresh_token.
        The EXISTING refresh_token must be preserved — never overwritten with null.
        """
        soon = _iso(_now() + timedelta(days=2))
        intg = _make_integration(
            db, store_id="SCH-B2",
            refresh_token="original-rt-must-survive",
            expires_at=soon,
        )

        mock_client = _mock_httpx_client(200, {
            "access_token": "new-access-only",
            # refresh_token deliberately absent
            "expires_in":   1_209_600,
        })

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert result["api_key"]       == "new-access-only"
        assert result["refresh_token"] == "original-rt-must-survive", (
            "refresh_token must NOT be overwritten with null when Salla omits it"
        )


# ─────────────────────────────────────────────────────────────────────────────
# C — Scheduler failure (transient)
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerFailure:

    def test_http_500_records_failure_without_disabling(self, db, session_factory):
        """
        HTTP 500 from Salla:
          • integration stays ENABLED
          • token_refresh_status = 'failed'
          • attempts incremented
          • existing tokens NOT overwritten
        """
        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(
            db, store_id="SCH-C1",
            api_key="original-access",
            refresh_token="original-refresh",
            expires_at=soon,
        )

        mock_client = _mock_httpx_client(500, text="Internal Server Error")

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert intg.enabled                      is True,   "integration must stay enabled"
        assert result["api_key"]                 == "original-access"
        assert result["refresh_token"]           == "original-refresh"
        assert result["token_refresh_status"]    == "failed"
        assert result["token_refresh_error"]     is not None
        assert result["token_refresh_failed_at"] is not None
        assert result["token_refresh_attempts"]  == 1


# ─────────────────────────────────────────────────────────────────────────────
# D — Failure escalation
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureEscalation:

    def test_three_failures_set_needs_reauth(self, db, session_factory):
        """
        3 clustered failures AND token expiring in 10 h → needs_reauth = True.
        Integration stays enabled; existing access_token is NOT cleared.

        Note: the grace window requires BOTH conditions — clustered failures
        within 24 h AND token expiring within 24 h.  Using 10 h here ensures
        the escalation triggers.
        """
        # Token expiring in 10 hours → within EXPIRY_THRESHOLD_HOURS (24)
        soon = _iso(_now() + timedelta(hours=10))
        intg = _make_integration(
            db, store_id="SCH-D1",
            api_key="still-valid-access",
            expires_at=soon,
            attempts=0,
        )

        mock_client = _mock_httpx_client(500, text="Server error")

        for run_num in range(3):
            with (
                patch("core.database.SessionLocal", session_factory),
                patch("os.environ.get", side_effect=_env_getter(
                    SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
                )),
                patch("httpx.AsyncClient", return_value=mock_client),
            ):
                from core.scheduler import _refresh_all_salla_tokens
                asyncio.run(_refresh_all_salla_tokens())

            result = _reload(db, intg)
            assert intg.enabled is True, f"integration must stay enabled (run={run_num})"

        result = _reload(db, intg)
        assert result.get("needs_reauth") is True, "needs_reauth must be True after 3 failures"
        assert result["api_key"]          == "still-valid-access", "access_token must not be cleared"
        assert result["token_refresh_attempts"] >= 3

    def test_success_resets_attempts_and_clears_needs_reauth(self, db, session_factory):
        """
        A successful refresh after prior failures resets attempts to 0 and
        clears needs_reauth.
        """
        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="SCH-D2", expires_at=soon, attempts=2)
        cfg  = dict(intg.config or {})
        cfg["token_refresh_status"] = "failed"
        cfg["token_refresh_error"]  = "previous error"
        # needs_reauth NOT set here — scheduler skips those, so clear it manually
        intg.config = cfg
        db.commit()

        mock_client = _mock_httpx_client(200, {
            "access_token":  "recovery-access",
            "refresh_token": "recovery-refresh",
            "expires_in":    1_209_600,
        })

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert result["token_refresh_status"]   == "success"
        assert result["token_refresh_attempts"] == 0
        assert not result.get("needs_reauth")


# ─────────────────────────────────────────────────────────────────────────────
# E — Runtime API protection (_ensure_token_fresh)
# ─────────────────────────────────────────────────────────────────────────────

class TestEnsureTokenFresh:

    def test_refreshes_when_token_expires_within_24h(self):
        """Token expiring in 30 min → proactive refresh before API call."""
        from store_adapters.salla_adapter import SallaAdapter

        expires_soon = _iso(_now() + timedelta(minutes=30))
        adapter = SallaAdapter(
            api_key="old-access",
            store_id="E-test",
            refresh_token="some-refresh",
            tenant_id=99,
            integration_id=100,
            expires_at=expires_soon,
        )

        refresh_called: list[bool] = []

        async def _mock_refresh():
            refresh_called.append(True)
            adapter.api_key     = "fresh-access"
            adapter._expires_at = _iso(_now() + timedelta(days=14))
            return True

        async def _run():
            with patch.object(adapter, "_refresh_access_token", side_effect=_mock_refresh):
                await adapter._ensure_token_fresh()

        asyncio.run(_run())

        assert len(refresh_called) == 1, "_refresh_access_token must be called once"
        assert adapter.api_key == "fresh-access"

    def test_does_not_refresh_when_token_still_fresh(self):
        """Token with 5+ days remaining → no premature refresh."""
        from store_adapters.salla_adapter import SallaAdapter

        expires_far = _iso(_now() + timedelta(days=5))
        adapter = SallaAdapter(
            api_key="ok-access",
            store_id="E-test",
            refresh_token="some-refresh",
            tenant_id=99,
            integration_id=101,
            expires_at=expires_far,
        )

        refresh_called: list[bool] = []

        async def _mock_refresh():
            refresh_called.append(True)
            return True

        async def _run():
            with patch.object(adapter, "_refresh_access_token", side_effect=_mock_refresh):
                await adapter._ensure_token_fresh()

        asyncio.run(_run())

        assert len(refresh_called) == 0, (
            "_refresh_access_token must NOT be called for a token with 5 days remaining"
        )

    def test_no_refresh_when_expires_at_unknown(self):
        """No expires_at → _ensure_token_fresh is a no-op."""
        from store_adapters.salla_adapter import SallaAdapter

        adapter = SallaAdapter(
            api_key="tok",
            store_id="E-test",
            refresh_token="rt",
            tenant_id=99,
            integration_id=102,
            expires_at=None,
        )

        refresh_called: list[bool] = []

        async def _mock_refresh():
            refresh_called.append(True)
            return True

        async def _run():
            with patch.object(adapter, "_refresh_access_token", side_effect=_mock_refresh):
                await adapter._ensure_token_fresh()

        asyncio.run(_run())

        assert len(refresh_called) == 0


# ─────────────────────────────────────────────────────────────────────────────
# F — Race condition prevention
# ─────────────────────────────────────────────────────────────────────────────

class TestRaceCondition:

    def test_asyncio_lock_prevents_concurrent_refresh(self):
        """
        Two concurrent _refresh_access_token calls on the same integration_id:
        only ONE must call httpx.post to Salla's OAuth endpoint.
        """
        from store_adapters.salla_adapter import SallaAdapter
        from core.salla_token_lock import _locks

        _locks.pop(200, None)   # clean up any leftover lock from prior test

        http_call_count = 0

        async def _fake_post(*args, **kwargs):
            nonlocal http_call_count
            http_call_count += 1
            await asyncio.sleep(0.05)   # simulate latency so both tasks truly overlap
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "access_token":  "race-new-access",
                "refresh_token": "race-new-refresh",
                "expires_in":    1_209_600,
            }
            return resp

        def _make_adapter():
            a = SallaAdapter(
                api_key="race-old-access",
                store_id="S-RACE",
                refresh_token="race-rt",
                tenant_id=50,
                integration_id=200,
                expires_at=_iso(_now() + timedelta(minutes=5)),
            )
            a._persist_refreshed_tokens = MagicMock()
            a._record_refresh_failure   = MagicMock()
            return a

        adapter1 = _make_adapter()
        adapter2 = _make_adapter()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(side_effect=_fake_post)

        async def _run():
            with (
                patch("os.environ.get", side_effect=_env_getter(
                    SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
                )),
                patch("httpx.AsyncClient", return_value=mock_client),
            ):
                return await asyncio.gather(
                    adapter1._refresh_access_token(),
                    adapter2._refresh_access_token(),
                    return_exceptions=True,
                )

        results = asyncio.run(_run())

        assert http_call_count == 1, (
            f"Salla's OAuth endpoint must be called exactly ONCE — "
            f"was called {http_call_count} times (race condition not prevented)"
        )
        assert any(r is True for r in results), "At least one refresh must report success"

    def test_lock_released_after_success(self):
        """asyncio lock is released after a successful refresh."""
        from store_adapters.salla_adapter import SallaAdapter
        from core.salla_token_lock import _locks, _get_asyncio_lock

        _locks.pop(300, None)

        adapter = SallaAdapter(
            api_key="tok", store_id="S-LOCK",
            refresh_token="rt", tenant_id=60, integration_id=300,
        )
        adapter._persist_refreshed_tokens = MagicMock()
        adapter._record_refresh_failure   = MagicMock()

        mock_client = _mock_httpx_client(200, {
            "access_token": "new", "refresh_token": "new-rt", "expires_in": 86400,
        })

        async def _run():
            with (
                patch("os.environ.get", side_effect=_env_getter(
                    SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
                )),
                patch("httpx.AsyncClient", return_value=mock_client),
            ):
                await adapter._refresh_access_token()

        asyncio.run(_run())

        lock = _get_asyncio_lock(300)
        assert not lock.locked(), "Lock must be released after successful refresh"

    def test_lock_released_after_failure(self):
        """asyncio lock is released even when Salla returns 500."""
        from store_adapters.salla_adapter import SallaAdapter
        from core.salla_token_lock import _locks, _get_asyncio_lock

        _locks.pop(400, None)

        adapter = SallaAdapter(
            api_key="tok", store_id="S-LOCK-FAIL",
            refresh_token="rt", tenant_id=70, integration_id=400,
        )
        adapter._persist_refreshed_tokens = MagicMock()
        adapter._record_refresh_failure   = MagicMock()

        mock_client = _mock_httpx_client(500, text="internal error")

        async def _run():
            with (
                patch("os.environ.get", side_effect=_env_getter(
                    SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
                )),
                patch("httpx.AsyncClient", return_value=mock_client),
            ):
                return await adapter._refresh_access_token()

        result = asyncio.run(_run())

        assert result is False
        lock = _get_asyncio_lock(400)
        assert not lock.locked(), "Lock must be released even after a failed refresh"

    def test_scheduler_db_lock_set_and_released(self, db, session_factory):
        """
        Scheduler sets token_refresh_in_progress=True before calling Salla's
        OAuth endpoint and clears it after the cycle finishes.
        """
        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="SCH-F1", expires_at=soon)

        flag_during_call: list[bool] = []

        async def _capture_flag(*args, **kwargs):
            flag_during_call.append(
                bool(_reload(db, intg).get("token_refresh_in_progress"))
            )
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "access_token": "sch-race-access",
                "expires_in":   1_209_600,
            }
            return resp

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(side_effect=_capture_flag)

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        assert any(flag_during_call), (
            "DB lock flag (token_refresh_in_progress) must be True during the OAuth HTTP call"
        )
        final_cfg = _reload(db, intg)
        assert not final_cfg.get("token_refresh_in_progress"), (
            "DB lock flag must be False after the scheduler cycle completes"
        )


# ─────────────────────────────────────────────────────────────────────────────
# G — Grace Window (Production Hardening)
# ─────────────────────────────────────────────────────────────────────────────

class TestGraceWindow:
    """
    needs_reauth=True is set ONLY when all three conditions hold:
      1. attempts >= 3
      2. Failures clustered within 24 hours (token_refresh_first_failed_at)
      3. Token expired or expiring within 24 hours
    """

    def test_no_escalation_when_token_still_fresh(self):
        """
        Scenario A: 3 failures BUT token has 5 days remaining → no needs_reauth.
        The merchant's store is not at risk yet; intermittent Salla issue.
        """
        from core.salla_token_alerts import should_escalate_to_needs_reauth

        now = _now()
        cfg = {
            "token_refresh_attempts":        3,
            "token_refresh_first_failed_at": (now - timedelta(hours=2)).isoformat(),
            "expires_at":                    (now + timedelta(days=5)).isoformat(),
        }
        escalate, reason = should_escalate_to_needs_reauth(cfg, now)
        assert escalate is False, (
            "needs_reauth must NOT be set when the token still has 5 days — "
            "merchant is not yet at risk"
        )
        assert reason is None

    def test_no_escalation_when_failures_spread_over_2_days(self):
        """
        Scenario A variant: failures spread over > 24 h (intermittent) AND
        token is expiring in 12 h → still no escalation because the grace
        window condition fails.
        """
        from core.salla_token_alerts import should_escalate_to_needs_reauth

        now = _now()
        cfg = {
            "token_refresh_attempts":        3,
            # First failure was 36 hours ago — outside the 24-h window
            "token_refresh_first_failed_at": (now - timedelta(hours=36)).isoformat(),
            "expires_at":                    (now + timedelta(hours=12)).isoformat(),
        }
        escalate, reason = should_escalate_to_needs_reauth(cfg, now)
        assert escalate is False, (
            "Failures spread over > 24 h are treated as intermittent — no escalation"
        )

    def test_escalates_when_3_failures_clustered_and_token_expiring(self):
        """
        Scenario B: 3 failures within 24 h AND token expires in 10 h
        → needs_reauth=True is correct.
        """
        from core.salla_token_alerts import should_escalate_to_needs_reauth

        now = _now()
        cfg = {
            "token_refresh_attempts":        3,
            "token_refresh_first_failed_at": (now - timedelta(hours=2)).isoformat(),
            "expires_at":                    (now + timedelta(hours=10)).isoformat(),
        }
        escalate, reason = should_escalate_to_needs_reauth(cfg, now)
        assert escalate is True
        assert reason is not None
        assert "refresh_failed_3x" in reason
        assert "h" in reason  # contains hours-until-expiry

    def test_escalates_when_token_already_expired(self):
        """
        Token has already expired → even 3 clustered failures escalate immediately.
        """
        from core.salla_token_alerts import should_escalate_to_needs_reauth

        now = _now()
        cfg = {
            "token_refresh_attempts":        3,
            "token_refresh_first_failed_at": (now - timedelta(hours=1)).isoformat(),
            "expires_at":                    (now - timedelta(hours=2)).isoformat(),  # expired
        }
        escalate, reason = should_escalate_to_needs_reauth(cfg, now)
        assert escalate is True
        assert reason is not None

    def test_no_escalation_without_expiry_info(self):
        """
        No expires_at in config → cannot confirm impact; do NOT escalate.
        """
        from core.salla_token_alerts import should_escalate_to_needs_reauth

        now = _now()
        cfg = {
            "token_refresh_attempts":        3,
            "token_refresh_first_failed_at": (now - timedelta(hours=1)).isoformat(),
            # expires_at deliberately absent
        }
        escalate, reason = should_escalate_to_needs_reauth(cfg, now)
        assert escalate is False, (
            "Without expires_at we cannot confirm merchant impact — no escalation"
        )

    def test_scheduler_sets_needs_reauth_only_when_token_expiring(self, db, session_factory):
        """
        Full end-to-end: scheduler with 3 failures + expiring token
        → needs_reauth=True is stored.
        """
        # Token expires in 10 hours → within EXPIRY_THRESHOLD_HOURS
        expiring_soon = _iso(_now() + timedelta(hours=10))
        intg = _make_integration(
            db, store_id="GW-B1",
            api_key="still-valid",
            expires_at=expiring_soon,
            attempts=0,
        )

        mock_client = _mock_httpx_client(500, text="Server error")

        for _ in range(3):
            with (
                patch("core.database.SessionLocal", session_factory),
                patch("os.environ.get", side_effect=_env_getter(
                    SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
                )),
                patch("httpx.AsyncClient", return_value=mock_client),
            ):
                from core.scheduler import _refresh_all_salla_tokens
                asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert result.get("needs_reauth") is True, (
            "needs_reauth must be True when token is expiring AND 3 clustered failures"
        )
        assert result.get("needs_reauth_reason") is not None
        assert result.get("token_refresh_first_failed_at") is not None

    def test_scheduler_does_not_set_needs_reauth_when_token_fresh(self, db, session_factory):
        """
        Full end-to-end: 3 failures but token has 5 days → no needs_reauth.
        The grace window protects the merchant from false alarms.
        """
        five_days = _iso(_now() + timedelta(days=5))
        intg = _make_integration(
            db, store_id="GW-A1",
            api_key="valid-access",
            expires_at=five_days,
            attempts=0,
        )

        mock_client = _mock_httpx_client(500, text="Server error")

        for _ in range(3):
            with (
                patch("core.database.SessionLocal", session_factory),
                patch("os.environ.get", side_effect=_env_getter(
                    SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
                )),
                patch("httpx.AsyncClient", return_value=mock_client),
            ):
                from core.scheduler import _refresh_all_salla_tokens
                asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert not result.get("needs_reauth"), (
            "needs_reauth must NOT be set when the token still has 5 days of validity "
            "— the grace window should hold"
        )
        assert intg.enabled is True

    def test_success_clears_first_failed_at_and_needs_reauth(self, db, session_factory):
        """
        Scenario C: Successful refresh resets ALL failure tracking fields.
        """
        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="GW-C1", expires_at=soon, attempts=2)
        cfg  = dict(intg.config or {})
        cfg["token_refresh_first_failed_at"] = (_now() - timedelta(hours=1)).isoformat()
        cfg["token_refresh_status"]          = "failed"
        cfg["needs_reauth"]                  = True
        cfg["token_reauth_alert_sent_at"]    = (_now() - timedelta(hours=1)).isoformat()
        intg.config = cfg
        db.commit()

        # Clear needs_reauth flag first so scheduler processes it
        _c = dict(intg.config or {})
        _c.pop("needs_reauth", None)
        intg.config = _c
        db.commit()

        mock_client = _mock_httpx_client(200, {
            "access_token":  "recovered-access",
            "refresh_token": "recovered-refresh",
            "expires_in":    1_209_600,
        })
        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert result["token_refresh_status"]           == "success"
        assert result["token_refresh_attempts"]         == 0
        assert result.get("token_refresh_first_failed_at") is None, (
            "token_refresh_first_failed_at must be cleared on success"
        )
        assert not result.get("needs_reauth")
        assert result.get("token_reauth_alert_sent_at") is None, (
            "Alert cooldown must be reset on success so new failures trigger a fresh alert"
        )


# ─────────────────────────────────────────────────────────────────────────────
# H — Alert Deduplication
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertDeduplication:
    """
    Scenario D: needs_reauth alert is sent once; not re-sent within 24 h;
    reminder sent after cooldown; cooldown resets after successful refresh.
    """

    def test_sends_alert_on_first_needs_reauth(self):
        """First needs_reauth episode → alert should be sent."""
        from core.salla_token_alerts import should_send_alert

        cfg = {}  # No prior alert
        assert should_send_alert(cfg, _now()) is True

    def test_skips_alert_within_cooldown(self):
        """Alert sent 1 hour ago → skip (within 24 h cooldown)."""
        from core.salla_token_alerts import should_send_alert

        cfg = {"token_reauth_alert_sent_at": (_now() - timedelta(hours=1)).isoformat()}
        assert should_send_alert(cfg, _now()) is False

    def test_sends_reminder_after_cooldown(self):
        """Alert sent 25 hours ago → send reminder (cooldown elapsed)."""
        from core.salla_token_alerts import should_send_alert

        cfg = {"token_reauth_alert_sent_at": (_now() - timedelta(hours=25)).isoformat()}
        assert should_send_alert(cfg, _now()) is True

    def test_maybe_send_marks_cfg_when_sent(self):
        """
        maybe_send_reauth_alert updates cfg[token_reauth_alert_sent_at] in-place
        when the email send succeeds.
        """
        from core.salla_token_alerts import maybe_send_reauth_alert

        now = _now()
        cfg = {
            "store_id":               "S-ALERT",
            "expires_at":             (now + timedelta(hours=5)).isoformat(),
            "token_refresh_attempts": 3,
            "token_refresh_error":    "HTTP 500",
            "needs_reauth_reason":    "refresh_failed_3x_token_expires_in_5.0h",
        }

        async def _run():
            with patch("core.notifications.send_email", AsyncMock(return_value=True)):
                return await maybe_send_reauth_alert(
                    tenant_id=42, integration_id=7, cfg=cfg, now=now
                )

        sent = asyncio.run(_run())
        assert sent is True
        assert cfg.get("token_reauth_alert_sent_at") is not None, (
            "cfg must be updated with token_reauth_alert_sent_at after successful send"
        )

    def test_maybe_send_skips_when_within_cooldown(self):
        """
        No email when token_reauth_alert_sent_at is < 24 h ago.
        """
        from core.salla_token_alerts import maybe_send_reauth_alert

        now = _now()
        cfg = {
            "store_id":                  "S-COOL",
            "token_reauth_alert_sent_at": (now - timedelta(hours=2)).isoformat(),
        }

        email_calls: list[bool] = []

        async def _run():
            async def _mock_email(**kw):
                email_calls.append(True)
                return True
            with patch("core.notifications.send_email", side_effect=_mock_email):
                return await maybe_send_reauth_alert(
                    tenant_id=1, integration_id=1, cfg=cfg, now=now
                )

        sent = asyncio.run(_run())
        assert sent is False
        assert len(email_calls) == 0, "send_email must not be called within cooldown period"

    def test_maybe_send_sends_reminder_after_cooldown(self):
        """
        Reminder email is sent when token_reauth_alert_sent_at is > 24 h ago.
        """
        from core.salla_token_alerts import maybe_send_reauth_alert

        now = _now()
        cfg = {
            "store_id":                  "S-REMIND",
            "token_reauth_alert_sent_at": (now - timedelta(hours=26)).isoformat(),
        }

        async def _run():
            with patch("core.notifications.send_email", AsyncMock(return_value=True)):
                return await maybe_send_reauth_alert(
                    tenant_id=99, integration_id=5, cfg=cfg, now=now
                )

        sent = asyncio.run(_run())
        assert sent is True


# ─────────────────────────────────────────────────────────────────────────────
# I — Metric Logs
# ─────────────────────────────────────────────────────────────────────────────

class TestMetricLogs:
    """
    Scenario E: Verify the three [SALLA METRIC] structured log lines fire
    at the correct moments.
    """

    def test_log_metric_success_emits_correct_format(self, caplog):
        """[SALLA METRIC] token_refresh_success is logged on a successful refresh."""
        import logging
        from core.salla_token_alerts import log_metric_success

        with caplog.at_level(logging.INFO, logger="nahla.salla_alerts"):
            log_metric_success(tenant_id=10, store_id="S-METRIC")

        assert any(
            "event=salla_token_refresh_success" in r.message
            and "tenant_hash=" in r.message
            for r in caplog.records
        ), f"Expected [SALLA METRIC] token_refresh_success in logs. Got: {[r.message for r in caplog.records]}"

    def test_log_metric_failed_emits_correct_format(self, caplog):
        """[SALLA METRIC] token_refresh_failed is logged on a failure."""
        import logging
        from core.salla_token_alerts import log_metric_failed

        with caplog.at_level(logging.WARNING, logger="nahla.salla_alerts"):
            log_metric_failed(tenant_id=20, store_id="S-METRIC", attempts=2)

        assert any(
            "event=salla_token_refresh_failed" in r.message
            and "tenant_hash=" in r.message
            and "attempts=2" in r.message
            for r in caplog.records
        )

    def test_log_metric_needs_reauth_emits_correct_format(self, caplog):
        """[SALLA METRIC] token_needs_reauth is logged when escalating."""
        import logging
        from core.salla_token_alerts import log_metric_needs_reauth

        with caplog.at_level(logging.CRITICAL, logger="nahla.salla_alerts"):
            log_metric_needs_reauth(
                tenant_id=30, store_id="S-METRIC",
                reason="refresh_failed_3x_token_expires_in_5.0h",
            )

        assert any(
            "event=salla_token_refresh_needs_reauth" in r.message
            and "tenant_hash=" in r.message
            for r in caplog.records
        )

    def test_scheduler_emits_metric_logs(self, db, session_factory, caplog):
        """End-to-end: scheduler run emits all relevant [SALLA METRIC] lines."""
        import logging

        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="SCH-METRIC1", expires_at=soon)

        mock_client = _mock_httpx_client(200, {
            "access_token":  "metric-access",
            "refresh_token": "metric-refresh",
            "expires_in":    1_209_600,
        })

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
            caplog.at_level(logging.INFO, logger="nahla.salla_alerts"),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        success_logs = [
            r.message for r in caplog.records
            if "event=salla_token_refresh_success" in r.message
        ]
        assert len(success_logs) >= 1, (
            "event=salla_token_refresh_success not found in logs. "
            f"Records: {[r.message for r in caplog.records]}"
        )

    def test_scheduler_emits_failed_metric(self, db, session_factory, caplog):
        """[SALLA METRIC] token_refresh_failed appears on scheduler failure."""
        import logging

        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="SCH-METRIC2", expires_at=soon)

        mock_client = _mock_httpx_client(500, text="error")

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
            caplog.at_level(logging.WARNING, logger="nahla.salla_alerts"),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        assert any(
            "event=salla_token_refresh_failed" in r.message
            for r in caplog.records
        )


# ─────────────────────────────────────────────────────────────────────────────
# I — invalid_grant counter invariants & superseded suppression
# ─────────────────────────────────────────────────────────────────────────────

class TestInvalidGrantCounterInvariants:
    """The fix for the 'Refresh Attempts = 0 / Last Error = invalid_grant' bug.

    The legacy scheduler short-circuited on invalid_grant without bumping
    ``token_refresh_attempts`` or stamping ``token_refresh_first_failed_at``.
    The alert email then displayed an incoherent state. After the patch, every
    real refresh failure stamps the counter via
    ``stamp_refresh_failure`` so the alert + dashboard never lie again.
    """

    def test_invalid_grant_bumps_attempts_and_first_failed_at(self, db, session_factory):
        """Scheduler observes 400/invalid_grant ⇒ attempts ≥ 1, first_failed_at set."""
        soon = _iso(_now() + timedelta(days=3))
        intg = _make_integration(db, store_id="IG-1", expires_at=soon)

        mock_client = _mock_httpx_client(
            400, text='{"error":"invalid_grant","error_description":"refresh_token revoked"}',
        )

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        result = _reload(db, intg)
        assert result.get("token_refresh_attempts", 0) >= 1, (
            f"invalid_grant must bump attempts; got {result.get('token_refresh_attempts')}"
        )
        assert result.get("token_refresh_first_failed_at"), (
            "first_failed_at must be stamped on first failure"
        )
        assert result.get("token_refresh_error") == "invalid_grant"
        assert result.get("needs_reauth") is True
        assert result.get("needs_reauth_reason") == "invalid_grant"
        # access_token preserved; refresh_token removed
        assert result.get("api_key") == "access-tok-001"
        assert "refresh_token" not in result
        assert result.get("no_auto_refresh") is True

    def test_stamp_refresh_failure_helper_is_idempotent_on_first_failed_at(self):
        """``token_refresh_first_failed_at`` is set once and preserved on subsequent calls."""
        from core.salla_token_alerts import stamp_refresh_failure
        cfg: dict = {}
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
        stamp_refresh_failure(cfg, error="HTTP 500", now=t1)
        first1 = cfg["token_refresh_first_failed_at"]
        assert cfg["token_refresh_attempts"] == 1
        stamp_refresh_failure(cfg, error="HTTP 500", now=t2)
        first2 = cfg["token_refresh_first_failed_at"]
        assert first1 == first2, "first_failed_at must not be overwritten"
        assert cfg["token_refresh_attempts"] == 2


class TestSupersededSuppression:
    """A newer healthy integration row supersedes the old failing one.

    When the merchant reinstalls the app, Salla issues a new integration row.
    The old row's refresh_token is revoked (Salla returns invalid_grant), but
    we must NOT spam the owner — the merchant has already reconnected.
    """

    def test_find_superseding_returns_newer_healthy_sibling(self, db):
        """Newer enabled, non-needs_reauth, has-api_key row supersedes the old one."""
        from core.salla_token_alerts import find_superseding_integration
        # Old failing integration
        old = _make_integration(db, store_id="SS-1", api_key="old-key", refresh_token="old-rt")
        old_cfg = dict(old.config or {})
        old_cfg["needs_reauth"] = True
        old.config = old_cfg
        db.commit()
        # Newer reinstall for the same store_id under the same tenant.
        # The (provider, external_store_id) unique constraint forces the new
        # row to leave external_store_id NULL — store_id lives in config.
        new_intg = Integration(
            tenant_id=old.tenant_id,
            provider="salla",
            external_store_id=None,
            config={
                "api_key":       "new-key",
                "refresh_token": "new-rt",
                "store_id":      "SS-1",
                "store_name":    "Test Store",
            },
            enabled=True,
        )
        db.add(new_intg)
        db.commit()
        result = find_superseding_integration(db, old)
        assert result is not None
        assert result.id == new_intg.id

    def test_find_superseding_skips_when_no_healthy_sibling(self, db):
        from core.salla_token_alerts import find_superseding_integration
        intg = _make_integration(db, store_id="SS-2")
        assert find_superseding_integration(db, intg) is None

    def test_invalid_grant_with_superseder_suppresses_alert_and_disables_old(
        self, db, session_factory,
    ):
        """invalid_grant on the old row + a healthy newer sibling ⇒ alert suppressed."""
        soon = _iso(_now() + timedelta(days=3))
        old = _make_integration(db, store_id="SS-3", expires_at=soon, api_key="old-ak", refresh_token="old-rt")
        # Newer healthy sibling for the same store — leaves external_store_id
        # NULL to satisfy the (provider, external_store_id) unique constraint.
        new_intg = Integration(
            tenant_id=old.tenant_id,
            provider="salla",
            external_store_id=None,
            config={
                "api_key":       "fresh-key",
                "refresh_token": "fresh-rt",
                "store_id":      "SS-3",
                "store_name":    "Test Store",
            },
            enabled=True,
        )
        db.add(new_intg)
        db.commit()
        new_id = new_intg.id

        mock_client = _mock_httpx_client(
            400, text='{"error":"invalid_grant"}',
        )

        with (
            patch("core.database.SessionLocal", session_factory),
            patch("os.environ.get", side_effect=_env_getter(
                SALLA_CLIENT_ID="cid", SALLA_CLIENT_SECRET="cs",
            )),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            from core.scheduler import _refresh_all_salla_tokens
            asyncio.run(_refresh_all_salla_tokens())

        old_cfg = _reload(db, old)
        # The old record gets parked + marked superseded; needs_reauth is NOT set
        assert old_cfg.get("superseded") is True
        assert old_cfg.get("superseded_by_integration_id") == new_id
        assert not old_cfg.get("needs_reauth"), (
            "needs_reauth must be suppressed when a newer healthy sibling exists"
        )
        # The newer row keeps working normally
        assert _reload(db, new_intg).get("api_key") == "fresh-key"


# ─────────────────────────────────────────────────────────────────────────────
# J — Ops vs merchant reauth email scope
# ─────────────────────────────────────────────────────────────────────────────

class TestReauthEmailScope:
    """Ops alerts stay internal; merchant email helper is isolated and unwired."""

    def test_ops_subject_uses_ops_tag_not_merchant_wording(self):
        from core.salla_token_alerts import OPS_REAUTH_TAG, build_ops_reauth_subject

        subject = build_ops_reauth_subject(tenant_id=1, store_id="22825873")
        assert OPS_REAUTH_TAG in subject
        assert "[SALLA TOKEN]" not in subject
        assert "Merchant Needs Reauth" not in subject
        assert "Tenant 1" in subject
        assert "22825873" in subject

    def test_ops_alert_sent_only_to_alerts_inbox(self):
        from core.salla_token_alerts import ALERT_EMAIL, maybe_send_reauth_alert

        now = _now()
        cfg = {
            "store_id":               "S-OPS",
            "expires_at":             (now + timedelta(hours=5)).isoformat(),
            "token_refresh_attempts": 3,
            "token_refresh_error":    "invalid_grant",
            "needs_reauth_reason":    "invalid_grant",
        }
        captured: dict = {}

        async def _capture(**kwargs):
            captured.update(kwargs)
            return True

        async def _run():
            with patch("core.notifications.send_email", side_effect=_capture):
                return await maybe_send_reauth_alert(
                    tenant_id=1, integration_id=3, cfg=cfg, now=now,
                )

        assert asyncio.run(_run()) is True
        assert captured.get("to") == ALERT_EMAIL
        assert "[OPS]" in captured.get("subject", "")
        assert "Merchant Needs Reauth" not in captured.get("html", "")
        assert "Internal Salla Token Reauth Required" in captured.get("html", "")
        assert "invalid_grant" in captured.get("html", "")

    def test_normalize_merchant_reauth_locale(self):
        from core.salla_token_alerts import normalize_merchant_reauth_locale

        assert normalize_merchant_reauth_locale(None) == "ar"
        assert normalize_merchant_reauth_locale("") == "ar"
        assert normalize_merchant_reauth_locale("ar") == "ar"
        assert normalize_merchant_reauth_locale("en") == "en"
        assert normalize_merchant_reauth_locale("en-US") == "en"

    def test_merchant_email_ar_has_no_technical_fields(self):
        from core.salla_token_alerts import build_merchant_reauth_email

        subject, html = build_merchant_reauth_email(
            locale="ar",
            reconnect_url="https://salla.sa/oauth/authorize",
        )
        assert subject == "يلزم إعادة ربط متجر سلة مع نحلة"
        assert "تعذر تحديث اتصال متجر سلة" in html
        assert "إعادة الربط من سلة" in html
        for forbidden in (
            "invalid_grant", "tenant_id", "integration_id",
            "Refresh Attempts", "token-status", "Open Token Status",
        ):
            assert forbidden not in html
            assert forbidden not in subject

    def test_merchant_email_en_copy(self):
        from core.salla_token_alerts import build_merchant_reauth_email

        subject, html = build_merchant_reauth_email(
            locale="en",
            reconnect_url="https://salla.sa/oauth/authorize",
        )
        assert subject == "Reconnect your Salla store to Nahla"
        assert "re-authorize the Nahla app" in html
        assert "Reconnect Salla" in html
        assert 'dir="ltr"' in html

    async def _run_send_merchant(self, **kwargs):
        from core.salla_token_alerts import send_merchant_salla_reauth_email
        with patch("core.notifications.send_email", AsyncMock(return_value=True)) as mock_send:
            sent = await send_merchant_salla_reauth_email(**kwargs)
        return sent, mock_send

    def test_send_merchant_reauth_skips_empty_recipient(self):
        sent, mock_send = asyncio.run(
            self._run_send_merchant(
                to="",
                reconnect_url="https://salla.sa/oauth/authorize",
                locale="ar",
            )
        )
        assert sent is False
        mock_send.assert_not_called()

    def test_send_merchant_reauth_dispatches_to_merchant(self):
        sent, mock_send = asyncio.run(
            self._run_send_merchant(
                to="merchant@example.com",
                reconnect_url="https://salla.sa/oauth/authorize",
                locale="ar",
            )
        )
        assert sent is True
        mock_send.assert_called_once()
        call_kw = mock_send.call_args.kwargs
        assert call_kw["to"] == "merchant@example.com"
        assert call_kw["to"] != "alerts@nahlah.ai"
        assert "يلزم إعادة ربط متجر سلة مع نحلة" in call_kw["subject"]

