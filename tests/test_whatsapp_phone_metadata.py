"""
tests/test_whatsapp_phone_metadata.py
─────────────────────────────────────
Locks down the half-bootstrapped fix for WhatsApp connections:

  1. `commit_connection()` MUST never persist `status='connected'` while
     `phone_number` / `business_display_name` are still NULL — when the
     caller does not supply them, the service falls back to
     `fetch_phone_metadata()` (a Graph lookup) and persists whatever
     Meta returns. If Meta also fails, the row is still written but a
     warning is logged.

  2. The backfill CLI's `_run()` repairs already-bootstrapped rows
     idempotently and never touches `status`, `sending_enabled`,
     tokens, or webhook flags.

We mock both the Graph lookup (`fetch_phone_metadata`) and the
side-effecting steps inside `commit_connection` (Meta validate +
register + webhook) — those are tested separately. Here we only care
that the display fields land in the right place.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Tuple

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker


REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

from models import Base, Tenant, WhatsAppConnection  # noqa: E402
from services import whatsapp_connection_service as wa_svc  # noqa: E402


def _make_db() -> Tuple[Any, Any]:
    engine = create_engine("sqlite:///:memory:")
    _saved: list[tuple] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig_type in _saved:
        col.type = orig_type
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed_tenant(db) -> Tenant:
    t = Tenant(name="T", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    return t


def _stub_side_effects(monkeypatch) -> None:
    """Neutralise the network/registration/webhook side effects so we can
    drive `commit_connection` end-to-end against an in-memory database."""
    monkeypatch.setattr(wa_svc, "validate_phone_waba_match",
                        lambda *_a, **_kw: (True, None, None))
    monkeypatch.setattr(wa_svc, "evict_phone_id_from_other_tenants",
                        lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "evict_waba_id_from_other_tenants",
                        lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_phone_id_not_claimed",
                        lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(wa_svc, "assert_waba_id_not_claimed",
                        lambda *_a, **_kw: None, raising=False)
    # Phone register + webhook subscribe live as private helpers; we make
    # any HTTP call a no-op by stubbing httpx.post to a 200 echo.
    class _FakeResp:
        status_code = 200
        content = b"{}"
        def json(self) -> dict:  # noqa: D401
            return {"success": True}
    monkeypatch.setattr(wa_svc.httpx, "post", lambda *_a, **_kw: _FakeResp(), raising=False)


# ── 1. commit_connection always persists display fields ────────────────────

class TestCommitConnectionDisplayFields:
    def test_caller_supplied_values_are_kept(self, monkeypatch) -> None:
        _stub_side_effects(monkeypatch)
        # The Graph fallback should NOT be needed — assert it isn't called
        # to prove caller-supplied values short-circuit the fetch.
        called = {"n": 0}
        def _spy(*_a, **_kw):
            called["n"] += 1
            return {"display_phone_number": "X", "verified_name": "X"}
        monkeypatch.setattr(wa_svc, "fetch_phone_metadata", _spy)

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            wa_svc.commit_connection(
                db,
                tenant_id       = t.id,
                phone_number_id = "PHONE-A",
                waba_id         = "WABA-A",
                access_token    = "tok",
                connection_type = "cloud_api",
                phone_number    = "+966500000000",
                display_name    = "Test Store",
            )
            row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
            assert row is not None
            assert row.status                == "connected"
            assert row.phone_number          == "+966500000000"
            assert row.business_display_name == "Test Store"
            assert called["n"] == 0  # ← no Graph fallback when caller supplied
        finally:
            db.close(); engine.dispose()

    def test_missing_values_are_backfilled_from_meta(self, monkeypatch) -> None:
        _stub_side_effects(monkeypatch)
        monkeypatch.setattr(
            wa_svc, "fetch_phone_metadata",
            lambda *_a, **_kw: {
                "display_phone_number":         "+966500000001",
                "verified_name":                "Auto Store",
                "whatsapp_business_account_id": "WABA-A",
            },
        )

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            wa_svc.commit_connection(
                db,
                tenant_id       = t.id,
                phone_number_id = "PHONE-B",
                waba_id         = "WABA-A",
                access_token    = "tok",
                connection_type = "cloud_api",
                # phone_number / display_name intentionally omitted
            )
            row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
            assert row.status                == "connected"
            assert row.phone_number          == "+966500000001"
            assert row.business_display_name == "Auto Store"
        finally:
            db.close(); engine.dispose()

    def test_meta_failure_does_not_block_write(self, monkeypatch) -> None:
        """If Meta is unavailable / lacks scope, the row still persists.
        We document the half-bootstrapped state by leaving the columns
        NULL — the backfill script will repair them later."""
        _stub_side_effects(monkeypatch)
        monkeypatch.setattr(
            wa_svc, "fetch_phone_metadata",
            lambda *_a, **_kw: {
                "display_phone_number":         None,
                "verified_name":                None,
                "whatsapp_business_account_id": None,
            },
        )

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            res = wa_svc.commit_connection(
                db,
                tenant_id       = t.id,
                phone_number_id = "PHONE-C",
                waba_id         = "WABA-C",
                access_token    = "tok",
                connection_type = "cloud_api",
            )
            assert res.credentials_saved is True
            row = db.query(WhatsAppConnection).filter_by(tenant_id=t.id).first()
            assert row.status                == "connected"
            assert row.phone_number          is None
            assert row.business_display_name is None
        finally:
            db.close(); engine.dispose()


# ── 2. Backfill CLI repairs half-bootstrapped rows ──────────────────────────

class TestBackfillCli:
    def _half_bootstrapped(self, db, tenant_id: int) -> WhatsAppConnection:
        conn = WhatsAppConnection(
            tenant_id              = tenant_id,
            status                 = "connected",
            phone_number_id        = "PHONE-Z",
            access_token           = "tok",
            connection_type        = "cloud_api",
            provider               = "meta",
            sending_enabled        = True,
            webhook_verified       = True,
        )
        db.add(conn); db.commit(); db.refresh(conn)
        return conn

    def test_dry_run_does_not_persist(self, monkeypatch) -> None:
        from scripts import backfill_whatsapp_phone_metadata as cli
        monkeypatch.setattr(
            cli, "fetch_phone_metadata",
            lambda *_a, **_kw: {"display_phone_number": "+966599", "verified_name": "X"},
        )
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            conn = self._half_bootstrapped(db, t.id)

            class _SessionFactory:
                def __call__(self):
                    return db
            monkeypatch.setattr(cli, "SessionLocal", _SessionFactory())
            # CLI must not close the session — wrap it in a no-op close.
            db.close = lambda: None  # type: ignore[assignment]

            scanned, updated, failed = cli._run(tenant_id=t.id, commit=False, refresh=False)
            assert (scanned, updated, failed) == (1, 1, 0)
            db.refresh(conn)
            assert conn.phone_number          is None
            assert conn.business_display_name is None
        finally:
            engine.dispose()

    def test_commit_persists_changes(self, monkeypatch) -> None:
        from scripts import backfill_whatsapp_phone_metadata as cli
        monkeypatch.setattr(
            cli, "fetch_phone_metadata",
            lambda *_a, **_kw: {"display_phone_number": "+966599", "verified_name": "Repaired"},
        )
        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            conn = self._half_bootstrapped(db, t.id)

            class _SessionFactory:
                def __call__(self):
                    return db
            monkeypatch.setattr(cli, "SessionLocal", _SessionFactory())
            db.close = lambda: None  # type: ignore[assignment]

            scanned, updated, failed = cli._run(tenant_id=t.id, commit=True, refresh=False)
            assert (scanned, updated, failed) == (1, 1, 0)
            db.refresh(conn)
            assert conn.phone_number          == "+966599"
            assert conn.business_display_name == "Repaired"
            # Nothing else should have changed.
            assert conn.status           == "connected"
            assert conn.sending_enabled  is True
            assert conn.webhook_verified is True
        finally:
            engine.dispose()

    def test_already_populated_rows_are_skipped(self, monkeypatch) -> None:
        from scripts import backfill_whatsapp_phone_metadata as cli
        called = {"n": 0}
        def _spy(*_a, **_kw):
            called["n"] += 1
            return {"display_phone_number": "X", "verified_name": "X"}
        monkeypatch.setattr(cli, "fetch_phone_metadata", _spy)

        db, engine = _make_db()
        try:
            t = _seed_tenant(db)
            conn = WhatsAppConnection(
                tenant_id              = t.id,
                status                 = "connected",
                phone_number_id        = "PHONE-OK",
                access_token           = "tok",
                phone_number           = "+966500000000",
                business_display_name  = "Already Set",
                connection_type        = "cloud_api",
                provider               = "meta",
                sending_enabled        = True,
            )
            db.add(conn); db.commit()

            class _SessionFactory:
                def __call__(self):
                    return db
            monkeypatch.setattr(cli, "SessionLocal", _SessionFactory())
            db.close = lambda: None  # type: ignore[assignment]

            scanned, updated, failed = cli._run(tenant_id=t.id, commit=True, refresh=False)
            assert (scanned, updated, failed) == (0, 0, 0)
            assert called["n"] == 0
        finally:
            engine.dispose()
