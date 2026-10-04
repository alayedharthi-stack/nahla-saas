"""Review-environment-only connection setup operator: stops without proven
isolation or confirmation, never prints the token or DSN, encrypts the token
through the existing path, refuses a catalog id another tenant carries, and
dry-run writes nothing.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from models import Base, Tenant, WhatsAppConnection  # noqa: E402
from scripts.operators import catalog_review_env_connection_setup as op  # noqa: E402

REVIEW_DSN = "postgresql+psycopg2://review_user:s3cret-dsn-password@postgres-catalog-review.railway.internal:5432/railway"
TOKEN = "EAAG-system-user-token-never-printed-0123456789"


def review_env(**overrides):
    env = {
        "NAHLA_CATALOG_REVIEW_ENV": "1",
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": REVIEW_DSN,
        "DASHBOARD_URL": "https://catalog-review.nahlah.ai",
        "NAHLA_CATALOG_REVIEW_WA_TOKEN": TOKEN,
        "WA_TOKEN_ENC_KEY": "u1RZzcBzs0r5OVljAb8vZ8ZgAJE9Z0jU6Yl9Jf1g3Fk=",
    }
    env.update(overrides)
    return env


def _remap_jsonb(target, connection, **kw):  # SQLite cannot CREATE JSONB
    for table in target.tables.values():
        for col in table.columns:
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest.fixture()
def session_factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    from sqlalchemy import event

    event.listen(Base.metadata, "before_create", _remap_jsonb)
    try:
        Base.metadata.create_all(engine, tables=[Tenant.__table__, WhatsAppConnection.__table__])
    finally:
        event.remove(Base.metadata, "before_create", _remap_jsonb)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    s = factory()
    s.add(Tenant(id=1, name="متجر تجريبي عام"))
    s.add(Tenant(id=2, name="متجر آخر"))
    s.commit()
    s.close()
    monkeypatch.setenv("WA_TOKEN_ENC_KEY", "u1RZzcBzs0r5OVljAb8vZ8ZgAJE9Z0jU6Yl9Jf1g3Fk=")
    return factory


def run(argv, env, session_factory=None, marker="catalog-review", stdin=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = op.run(argv, env=env, session_factory=session_factory, marker_reader=lambda u: marker, stdin_reader=stdin)
    lines = [json.loads(l) for l in buf.getvalue().strip().splitlines() if l.strip()]
    return rc, lines[-1] if lines else {}, buf.getvalue()


BASE_ARGS = ["--tenant-id", "1", "--catalog-id", "999000111", "--business-id", "248365378024448"]


def test_stops_when_review_flag_unset(session_factory):
    env = review_env(); env.pop("NAHLA_CATALOG_REVIEW_ENV")
    rc, out, text = run(BASE_ARGS, env, session_factory)
    assert rc == 2 and out["stage"] == "isolation" and out["error"] == "review_env_flag_unset"
    assert TOKEN not in text and "s3cret-dsn-password" not in text


@pytest.mark.parametrize("bad", [
    {"DATABASE_URL": "postgresql://u:pw@postgres-staging.railway.internal:5432/nahla"},
    {"DATABASE_URL": "postgresql://u:pw@postgres.railway.internal:5432/railway"},
    {"DASHBOARD_URL": "https://app.nahlah.ai"},
    {"RAILWAY_ENVIRONMENT_NAME": "production"},
])
def test_stops_when_isolation_not_proven(session_factory, bad):
    rc, out, text = run(BASE_ARGS, review_env(**bad), session_factory)
    assert rc == 2 and out["status"] == "refused" and out["stage"] == "isolation"
    assert TOKEN not in text and "pw@" not in text


def test_stops_when_database_marker_missing(session_factory):
    rc, out, _ = run(BASE_ARGS, review_env(), session_factory, marker=None)
    assert rc == 2 and "database_marker_missing" in out["error"]


def test_dry_run_default_writes_nothing_and_hides_token(session_factory):
    rc, out, text = run(BASE_ARGS, review_env(), session_factory)
    assert rc == 0 and out["status"] == "dry_run"
    assert out["plan"]["action"] == "insert" and out["plan"]["meta_catalog_id"] == "999000111"
    assert out["token_provided"] is True
    assert TOKEN not in text and "s3cret-dsn-password" not in text
    s = session_factory()
    assert s.query(WhatsAppConnection).count() == 0
    s.close()


def test_write_requires_confirmation(session_factory):
    rc, out, _ = run(BASE_ARGS + ["--write"], review_env(), session_factory)
    assert rc == 3 and out["error"] == "dangerous_action_not_confirmed"
    s = session_factory()
    assert s.query(WhatsAppConnection).count() == 0
    s.close()


def test_token_missing_is_refused_without_touching_db(session_factory):
    env = review_env(); env.pop("NAHLA_CATALOG_REVIEW_WA_TOKEN")
    rc, out, _ = run(BASE_ARGS, env, session_factory)
    assert rc == 4 and out["error"] == "token_missing"


def test_write_encrypts_token_and_configures_connection(session_factory):
    env = review_env(**{op.CONFIRMATION_ENV: op.CONFIRMATION_TOKEN})
    rc, out, text = run(BASE_ARGS + ["--write", "--waba-id", "111222333"], env, session_factory)
    assert rc == 0 and out["status"] == "written", out
    assert out["result"]["token_encrypted"] is True and out["result"]["token_stored_prefix"] == "enc1:"
    assert TOKEN not in text
    s = session_factory()
    conn = s.query(WhatsAppConnection).filter_by(tenant_id=1).one()
    assert conn.meta_catalog_id == "999000111" and conn.catalog_enabled is True
    assert conn.provider == "meta" and conn.status == "connected" and conn.connection_type == "embedded"
    assert conn.business_manager_id == "248365378024448" and conn.whatsapp_business_account_id == "111222333"
    assert conn.access_token.startswith("enc1:") and TOKEN not in conn.access_token
    from services.whatsapp_platform.wa_connection_secrets import read_access_token

    assert read_access_token(conn) == TOKEN
    s.close()


def test_token_from_stdin_is_accepted_and_not_echoed(session_factory):
    env = review_env(**{op.CONFIRMATION_ENV: op.CONFIRMATION_TOKEN}); env.pop("NAHLA_CATALOG_REVIEW_WA_TOKEN")
    rc, out, text = run(BASE_ARGS + ["--write", "--token-stdin"], env, session_factory, stdin=lambda: TOKEN + "\n")
    assert rc == 0 and out["status"] == "written"
    assert TOKEN not in text


def test_refuses_catalog_id_held_by_another_tenant(session_factory):
    s = session_factory()
    s.add(WhatsAppConnection(tenant_id=2, provider="meta", status="connected", meta_catalog_id="999000111", catalog_enabled=True))
    s.commit(); s.close()
    rc, out, _ = run(BASE_ARGS, review_env(), session_factory)
    assert rc == 5 and out["error"] == "catalog_claimed_by_other_tenant"


def test_refuses_unknown_tenant_and_bad_catalog_id(session_factory):
    rc, out, _ = run(["--tenant-id", "77", "--catalog-id", "999000111"], review_env(), session_factory)
    assert rc == 5 and out["error"] == "tenant_not_found"
    rc, out, _ = run(["--tenant-id", "1", "--catalog-id", "not-a-catalog"], review_env(), session_factory)
    assert rc == 5 and out["error"] == "catalog_id_rejected"


def test_update_path_keeps_single_row(session_factory):
    env = review_env(**{op.CONFIRMATION_ENV: op.CONFIRMATION_TOKEN})
    assert run(BASE_ARGS + ["--write"], env, session_factory)[0] == 0
    rc, out, _ = run(["--tenant-id", "1", "--catalog-id", "999000222", "--write"], env, session_factory)
    assert rc == 0 and out["result"]["meta_catalog_id"] == "999000222"
    s = session_factory()
    assert s.query(WhatsAppConnection).count() == 1
    s.close()
