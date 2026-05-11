"""
tests/test_db_schema_health.py
──────────────────────────────
Locks the F15 db-schema diagnostic that surfaces missing migrations
on production. The merchant hit:

    psycopg2.errors.UndefinedColumn:
    column campaign_send_logs.delivered_at does not exist

because migration 0054 was never applied to Railway. The endpoint
``GET /admin/debug/db-schema-health`` converts that runtime crash
into an explicit verdict — "migration 0054 has not been applied,
columns added by it are missing" — and points at the repair
endpoint ``POST /admin/debug/run-migrations``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class TestLatestRevisionInCodebase:
    def test_returns_highest_revision_from_versions_dir(self):
        """The endpoint compares the codebase head against the
        deployed head. The codebase head is read by parsing every
        ``revision = "...."`` line in
        ``database/migrations/versions/`` — this test pins the
        parser against the actual repo state."""
        from routers.admin_debug import _latest_revision_in_codebase

        head = _latest_revision_in_codebase()
        # Right now the latest revision in the repo is 0054. The
        # exact value will move forward as new migrations land, but
        # whatever it is must be:
        #   * non-None (the parser found at least one revision)
        #   * a 4-digit string (the zero-padded format we use)
        #   * lexicographically >= "0054" (the dispatcher columns
        #     migration cannot be lost)
        assert head is not None, (
            "codebase head should be readable from "
            "database/migrations/versions/"
        )
        assert len(head) == 4
        assert head >= "0054", (
            "0054 must remain in the migration history — it adds "
            "delivered_at/read_at/failed_at that the dispatcher "
            "depends on"
        )

    def test_critical_columns_list_includes_delivered_at(self):
        """The ``_CRITICAL_COLUMNS`` list is the contract between
        the diagnostic endpoint and the production debug surface.
        It MUST include ``campaign_send_logs.delivered_at`` —
        that's the column whose absence caused the production
        outage."""
        from routers.admin_debug import _CRITICAL_COLUMNS

        names = {
            (c["table"], c["column"]) for c in _CRITICAL_COLUMNS
        }
        assert ("campaign_send_logs", "delivered_at") in names
        assert ("campaign_send_logs", "read_at")      in names
        assert ("campaign_send_logs", "failed_at")    in names
        # Each entry must record which migration introduced it,
        # so the verdict can name the missing revision in plain
        # text.
        for spec in _CRITICAL_COLUMNS:
            assert spec["added_by"], (
                f"missing 'added_by' for {spec!r} — operator won't "
                f"know which migration to run"
            )


class TestSchemaHealthEndpoint:
    """End-to-end shape check against an in-memory SQLite DB. We
    cannot exercise ``information_schema.columns`` on SQLite
    directly, so we patch the SQL execution to simulate a missing
    column and a stale alembic head."""

    def test_endpoint_flags_missing_migration_and_columns(self, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        # Stub require_admin and get_db. We don't need a real
        # session — only that the dependency resolves so the
        # endpoint can run its SQL through our mock.
        from core import auth as auth_mod

        def _fake_require_admin():
            return {"user_id": 1, "role": "admin"}

        monkeypatch.setattr(auth_mod, "require_admin", _fake_require_admin)

        # Mock DB: stale alembic head, every critical column
        # absent. Simulates the exact production failure.
        class _MockResult:
            def __init__(self, value):
                self._value = value

            def first(self):
                return self._value

        class _MockDB:
            def execute(self, sql, params=None):
                sql_text = str(sql)
                if "alembic_version" in sql_text:
                    return _MockResult(("0050",))     # stale
                if "information_schema.columns" in sql_text:
                    return _MockResult(None)          # column missing
                return _MockResult(None)

            def rollback(self):
                return None

        def _mock_get_db():
            yield _MockDB()

        from core import database as db_mod
        monkeypatch.setattr(db_mod, "get_db", _mock_get_db)

        import importlib
        from routers import admin_debug
        importlib.reload(admin_debug)

        app = FastAPI()
        # The router's own dependency on get_db is what we want
        # to replace. FastAPI's dependency_overrides is the
        # cleanest hook.
        from core.database import get_db as _real_get_db
        app.dependency_overrides[_real_get_db] = _mock_get_db
        app.include_router(admin_debug.router)
        client = TestClient(app)

        resp = client.get("/admin/debug/db-schema-health")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # Shape contract
        assert "deployed_alembic_head" in body
        assert "codebase_alembic_head" in body
        assert "behind_by"             in body
        assert "missing_migrations"    in body
        assert "critical_columns"      in body
        assert "issues"                in body
        assert "hints"                 in body
        assert "ok"                    in body

        # Verdict
        assert body["deployed_alembic_head"] == "0050"
        assert body["codebase_alembic_head"] >= "0054"
        assert body["behind_by"] is True
        assert "0054" in body["missing_migrations"]
        # Every critical column should be reported as missing.
        assert all(
            not col["present"] for col in body["critical_columns"]
        )
        # ok=False because we have issues.
        assert body["ok"] is False
        # Issues must mention the specific missing migration so the
        # merchant doesn't have to guess.
        assert any("0054" in i for i in body["issues"])

    def test_endpoint_reports_ok_when_schema_matches(self, monkeypatch):
        """Happy path: deployed head == codebase head AND every
        critical column is present. ``ok=True`` and ``issues=[]``."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from core import auth as auth_mod
        from routers.admin_debug import _latest_revision_in_codebase

        head_in_codebase = _latest_revision_in_codebase()

        def _fake_require_admin():
            return {"user_id": 1, "role": "admin"}

        monkeypatch.setattr(auth_mod, "require_admin", _fake_require_admin)

        class _MockResult:
            def __init__(self, v):
                self._v = v

            def first(self):
                return self._v

        class _MockDB:
            def execute(self, sql, params=None):
                sql_text = str(sql)
                if "alembic_version" in sql_text:
                    return _MockResult((head_in_codebase,))  # in sync
                if "information_schema.columns" in sql_text:
                    return _MockResult((1,))                  # column present
                return _MockResult(None)

            def rollback(self):
                return None

        def _mock_get_db():
            yield _MockDB()

        import importlib
        from routers import admin_debug
        importlib.reload(admin_debug)

        app = FastAPI()
        from core.database import get_db as _real_get_db
        app.dependency_overrides[_real_get_db] = _mock_get_db
        app.include_router(admin_debug.router)
        client = TestClient(app)

        resp = client.get("/admin/debug/db-schema-health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["issues"] == []
        assert body["behind_by"] is False
        assert body["missing_migrations"] == []

    def test_endpoint_surfaces_skip_bootstrap_env(self, monkeypatch):
        """If ``NAHLA_SKIP_DB_BOOTSTRAP=1`` is set, alembic is
        never invoked at startup — the merchant must be told
        about it explicitly, even when the schema happens to be
        in sync (because the next migration won't auto-apply)."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from core import auth as auth_mod
        from routers.admin_debug import _latest_revision_in_codebase

        head_in_codebase = _latest_revision_in_codebase()

        def _fake_require_admin():
            return {"user_id": 1, "role": "admin"}

        monkeypatch.setattr(auth_mod, "require_admin", _fake_require_admin)
        monkeypatch.setenv("NAHLA_SKIP_DB_BOOTSTRAP", "1")

        class _MockResult:
            def __init__(self, v):
                self._v = v

            def first(self):
                return self._v

        class _MockDB:
            def execute(self, sql, params=None):
                if "alembic_version" in str(sql):
                    return _MockResult((head_in_codebase,))
                return _MockResult((1,))

            def rollback(self):
                return None

        def _mock_get_db():
            yield _MockDB()

        import importlib
        from routers import admin_debug
        importlib.reload(admin_debug)

        app = FastAPI()
        from core.database import get_db as _real_get_db
        app.dependency_overrides[_real_get_db] = _mock_get_db
        app.include_router(admin_debug.router)
        client = TestClient(app)

        resp = client.get("/admin/debug/db-schema-health")
        body = resp.json()
        assert body["skip_bootstrap_env_set"] is True
        assert any("NAHLA_SKIP_DB_BOOTSTRAP" in i for i in body["issues"])
        assert body["ok"] is False
