"""Catalog-only Meta consent against REAL PostgreSQL.

Proves, on throw-away databases cloned from a template migrated to 0118 (the
parent of 0120) on the admin DSN in ``NAHLA_RELIABILITY_PG_ADMIN_DSN``:

Template: ``0093`` → ``0111`` → ``0113`` → ``0118`` (the review provisioning
path, so every ORM column the runtime reads exists).

* migration 0120 creates ``meta_catalog_authorizations``, adopts an identical
  ``create_all`` table unchanged, refuses a drifted one without changing
  anything, and downgrades by dropping only that table;
* a pre-0120 database with the feature disabled keeps the existing catalog
  claim and connection paths working, with no aborted transaction;
* consent enabled on a pre-0120 database fails closed before any Graph call;
* the catalog-consent nonce is consumed exactly once under concurrency, and a
  WhatsApp embedded/coexistence consume can never take it;
* two tenants persisting the same catalog concurrently: exactly one wins;
* the full callback against PostgreSQL stores one encrypted row, and
  concurrent replays of one state produce exactly one success and one token
  exchange.

Graph is an in-process fake; every credential is generated at runtime.
Without the variable the module is skipped (reported as skipped, never as
passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no variable it fails.
Inventoried in the required PostgreSQL proofs.
"""
from __future__ import annotations

import ast
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_REPO = Path(__file__).resolve().parents[2]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")

TABLE = "meta_catalog_authorizations"
_SCRIPT = next((_REPO / "database" / "migrations" / "versions").glob(f"*_{TABLE}.py"))


def _declared(name: str) -> str:
    for node in ast.parse(_SCRIPT.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return node.value.value
    raise AssertionError(f"{name} not declared in {_SCRIPT.name}")


REVISION = _declared("revision")
PARENT = _declared("down_revision")

API_HOST = "api.catalog-review.example.test"
CALLBACK_PATH = "/merchant/catalog/meta-consent/callback"
REDIRECT = f"https://{API_HOST}{CALLBACK_PATH}"
CATALOG = "880000000000001"
BUSINESS = "770000000000001"
OTHER_CATALOG = "880000000000002"
APP_ID = "600000000000001"
USER_ID = "500000000000001"


def _admin(*statements: str) -> None:
    eng = create_engine(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        eng.dispose()


def _url(db: str) -> str:
    return make_url(ADMIN_URL).set(drivername="postgresql+psycopg2", database=db).render_as_string(hide_password=False)


def _alembic(db: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": _url(db)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(_REPO / "database"), env=env,
                          capture_output=True, text=True, timeout=600)


def _versions(db: str) -> set:
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        with eng.connect() as conn:
            return set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    finally:
        eng.dispose()


def _exec(db: str, *statements: str) -> None:
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            for stmt in statements:
                conn.execute(text(stmt))
    finally:
        eng.dispose()


def _shape(db: str):
    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        insp = inspect(eng)
        if TABLE not in insp.get_table_names():
            return None
        cols = sorted((c["name"], str(c["type"]), c["nullable"]) for c in insp.get_columns(TABLE))
        uniques = sorted(tuple(u["column_names"]) for u in insp.get_unique_constraints(TABLE))
        fks = sorted((tuple(f["constrained_columns"]), f["referred_table"]) for f in insp.get_foreign_keys(TABLE))
        return cols, uniques, fks
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def template():
    name = f"mcc_tmpl_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name}")
    try:
        # The review provisioning path: application heads, then the catalog branch.
        for target in ("0093", "0111", "0113", PARENT):
            proc = _alembic(name, "upgrade", target)
            assert proc.returncode == 0, proc.stderr[-3000:]
        assert PARENT in _versions(name)
        yield name
    finally:
        _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture()
def db(template):
    name = f"mcc_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name} TEMPLATE {template}")
    try:
        yield name
    finally:
        _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _create_with_model(db: str) -> None:
    from database.models import MetaCatalogAuthorization  # the startup create_all definition

    eng = create_engine(_url(db), poolclass=NullPool, future=True)
    try:
        MetaCatalogAuthorization.__table__.create(eng)
    finally:
        eng.dispose()


# ── migration 0120 ───────────────────────────────────────────────────────────

def test_fresh_database_gets_the_table_and_downgrade_removes_only_it(db):
    before = _versions(db)
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _versions(db) == (before - {PARENT}) | {REVISION}
    cols, uniques, fks = _shape(db)
    assert uniques == [("catalog_id",), ("tenant_id",)]
    assert fks == [(("tenant_id",), "tenants")]
    assert ("access_token_enc", "TEXT", False) in cols
    proc = _alembic(db, "downgrade", f"{REVISION}@-1")
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _shape(db) is None and _versions(db) == before


def test_create_all_table_is_adopted_unchanged(db):
    _create_with_model(db)
    shape = _shape(db)
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _shape(db) == shape and REVISION in _versions(db)


@pytest.mark.parametrize("drift", [
    f"ALTER TABLE {TABLE} ADD COLUMN extra_note VARCHAR(10)",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT uq_meta_catalog_authorizations_catalog",
    f"ALTER TABLE {TABLE} ALTER COLUMN access_token_enc DROP NOT NULL",
    f"ALTER TABLE {TABLE} ADD CONSTRAINT ck_status CHECK (status <> '')",
], ids=["extra_column", "catalog_not_unique", "token_nullable", "extra_check"])
def test_drifted_table_fails_closed_and_changes_nothing(db, drift):
    _create_with_model(db)
    _exec(db, drift)
    shape, versions = _shape(db), _versions(db)
    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode != 0 and "different definition" in (proc.stderr + proc.stdout)
    assert _shape(db) == shape and _versions(db) == versions


# ── runtime against PostgreSQL ───────────────────────────────────────────────

@pytest.fixture()
def runtime(db, monkeypatch):
    """Schema at 0118 (no consent table) with two tenants; consent env configured."""
    from cryptography.fernet import Fernet

    import core.config as core_config
    import core.review_environment as review_env_mod
    import core.whatsapp_oauth_nonce as nonce_mod
    from services import meta_catalog_consent as consent

    engine = create_engine(_url(db), poolclass=NullPool, future=True)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO tenants (id, name, is_active) VALUES (1, 'متجر تجريبي عام', true), "
                          "(2, 'متجر تجريبي آخر', true)"))
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    env = {
        "NAHLA_CATALOG_REVIEW_ENV": "1",
        "RAILWAY_PROJECT_NAME": "desirable-growth",
        "RAILWAY_ENVIRONMENT_NAME": "staging",
        "ENVIRONMENT": "staging",
        "DATABASE_URL": "postgresql+psycopg2://review_user:pw@postgres-catalog-review.railway.internal:5432/railway",
        "DASHBOARD_URL": "https://catalog-review.example.test",
        "NAHLA_META_CATALOG_CONSENT_ENABLED": "1",
        "META_CATALOG_CONSENT_CONFIG_ID": "400000000000001",
        "META_CATALOG_CONSENT_REDIRECT_URI": REDIRECT,
        "META_CATALOG_CONSENT_APPROVED_ASSETS": f"1:{CATALOG}:{BUSINESS},2:{OTHER_CATALOG}:{BUSINESS}",
        "META_APP_ID": APP_ID,
        "META_APP_SECRET": secrets.token_urlsafe(32),
        "WA_TOKEN_ENC_KEY": Fernet.generate_key().decode(),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("NAHLA_WHATSAPP_CATALOG_SYNC_TENANT_IDS", raising=False)
    monkeypatch.setattr(core_config, "JWT_SECRET", secrets.token_urlsafe(48))
    monkeypatch.setattr(review_env_mod, "read_database_marker",
                        lambda _u: review_env_mod.MarkerReading("catalog-review", "catalog-review", "railway"))
    monkeypatch.setattr(nonce_mod, "_independent_session", factory)
    consent._TABLE_SEEN.clear()
    yield engine, factory, db
    consent._TABLE_SEEN.clear()
    engine.dispose()


def _migrate_consent(db: str) -> None:
    from services import meta_catalog_consent as consent

    proc = _alembic(db, "upgrade", REVISION)
    assert proc.returncode == 0, proc.stderr[-3000:]
    consent._TABLE_SEEN.clear()


def test_pre_0120_database_keeps_existing_catalog_paths_with_feature_disabled(runtime, monkeypatch):
    from models import WhatsAppConnection
    from services.meta_catalog_claim import CatalogClaimError, guard_catalog_claim
    from services.meta_catalog_push import _resolve_connection

    engine, factory, _db = runtime
    monkeypatch.setenv("NAHLA_META_CATALOG_CONSENT_ENABLED", "0")
    monkeypatch.delenv("NAHLA_CATALOG_REVIEW_ENV")
    assert TABLE not in inspect(engine).get_table_names()
    with factory() as s:
        s.add(WhatsAppConnection(tenant_id=1, meta_catalog_id=CATALOG, catalog_enabled=True))
        s.add(WhatsAppConnection(tenant_id=2, meta_catalog_id=OTHER_CATALOG))
        s.commit()
        assert isinstance(_resolve_connection(s, 1), WhatsAppConnection)
        guard_catalog_claim(s, 1, CATALOG)
        with pytest.raises(CatalogClaimError):
            guard_catalog_claim(s, 1, OTHER_CATALOG)
        s.rollback()
        # Same session, same transaction scope: nothing was aborted by a missing table.
        assert s.execute(text("SELECT count(*) FROM whatsapp_connections")).scalar() == 2
        guard_catalog_claim(s, 1, CATALOG)
        s.commit()


def test_consent_enabled_on_pre_0120_database_fails_closed(runtime):
    from routers.meta_catalog_consent import _entitlement_and_scope_code

    _engine, factory, _db = runtime
    with factory() as s:
        assert _entitlement_and_scope_code(s, 1) == "storage_unavailable"


def test_catalog_nonce_is_consumed_exactly_once_under_concurrency(runtime):
    import core.whatsapp_oauth_nonce as nonce_mod

    _engine, factory, _db = runtime
    nonce = secrets.token_urlsafe(24)
    with factory() as s:
        nonce_mod.persist_catalog_consent_nonce(
            s, nonce=nonce, tenant_id=1, redirect_uri=REDIRECT, catalog_id=CATALOG, business_id=BUSINESS,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        s.commit()
    for mode in ("embedded", "coexistence"):
        with pytest.raises(nonce_mod.NonceRejected):
            nonce_mod.consume_oauth_nonce(nonce=nonce, tenant_id=1, connection_mode=mode, redirect_uri=REDIRECT)
    barrier = threading.Barrier(8)
    outcomes = []

    def consume():
        barrier.wait()
        try:
            nonce_mod.consume_catalog_consent_nonce(nonce=nonce, tenant_id=1, redirect_uri=REDIRECT,
                                                    catalog_id=CATALOG, business_id=BUSINESS)
            outcomes.append("ok")
        except nonce_mod.NonceRejected:
            outcomes.append("rejected")

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes) == ["ok"] + ["rejected"] * 7


def test_two_tenants_persisting_one_catalog_concurrently_one_wins(runtime):
    from core.meta_catalog_consent_config import CatalogApproval
    from services import meta_catalog_consent as consent

    _engine, factory, db = runtime
    _migrate_consent(db)
    barrier = threading.Barrier(2)
    outcomes = {}

    def persist(tenant_id):
        verified = consent.VerifiedConsent(access_token="EAA" + secrets.token_hex(24), meta_user_id=USER_ID,
                                           scopes=("business_management", "catalog_management"),
                                           token_expires_at=None, data_access_expires_at=None)
        with factory() as s:
            barrier.wait()
            try:
                consent.persist_authorization(s, approval=CatalogApproval(tenant_id, CATALOG, BUSINESS),
                                              verified=verified, app_id=APP_ID)
                outcomes[tenant_id] = "ok"
            except consent.ConsentError as exc:
                outcomes[tenant_id] = exc.code

    threads = [threading.Thread(target=persist, args=(tid,)) for tid in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(outcomes.values()) == ["catalog_claimed_by_other_tenant", "ok"]
    with factory() as s:
        rows = s.execute(text(f"SELECT tenant_id, catalog_id, access_token_enc FROM {TABLE}")).fetchall()
    assert len(rows) == 1 and rows[0][1] == CATALOG and rows[0][2].startswith("enc1:")


def _client(factory):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from routers import meta_catalog_consent as router_mod

    app = FastAPI()

    @app.middleware("http")
    async def _fake_jwt(request: Request, call_next):
        raw = request.headers.get("x-test-jwt")
        if raw:
            request.state.jwt_payload = json.loads(raw)
        return await call_next(request)

    app.include_router(router_mod.router)

    def _db():
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[router_mod.get_db] = _db
    return TestClient(app)


def test_full_callback_on_postgres_and_concurrent_replay(runtime, monkeypatch):
    import core.plan_entitlements as ent_mod
    from services import meta_catalog_consent as consent

    _engine, factory, db = runtime
    _migrate_consent(db)

    class _Ent:
        def has_feature(self, key):
            return key == "meta_catalog_sync"

    monkeypatch.setattr(ent_mod, "get_entitlements", lambda d, t, strict_lookup=True: _Ent())
    token = "EAA" + secrets.token_hex(24)
    exchanges = []
    lock = threading.Lock()

    async def graph(path, params, *, token_arg=None, **kw):
        now = int(time.time())
        if path == "oauth/access_token":
            with lock:
                exchanges.append(params.get("grant_type", "code"))
            return 200, {"access_token": token, "expires_in": 5184000}
        if path == "debug_token":
            return 200, {"data": {"app_id": APP_ID, "type": "USER", "user_id": USER_ID, "is_valid": True,
                                  "expires_at": now + 5_000_000, "data_access_expires_at": now + 7_000_000,
                                  "scopes": ["catalog_management", "business_management"]}}
        if path == "me":
            return 200, {"id": USER_ID}
        if path == "me/permissions":
            return 200, {"data": [{"permission": "catalog_management", "status": "granted"},
                                  {"permission": "business_management", "status": "granted"}]}
        if path == CATALOG:
            return 200, {"id": CATALOG, "business": {"id": BUSINESS}}
        if path == f"{BUSINESS}/owned_product_catalogs":
            return 200, {"data": [{"id": CATALOG}]}
        if path == "me/business_users":
            return 200, {"data": [{"id": "9001", "business": {"id": BUSINESS}, "role": "ADMIN"}]}
        return 404, {"error": {"code": 803}}

    monkeypatch.setattr(consent, "_graph_get", graph)
    client = _client(factory)
    started = client.post("/merchant/catalog/meta-consent/start",
                          headers={"x-test-jwt": json.dumps({"tenant_id": 1, "role": "merchant"})})
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["authorize_url"]).query)["state"][0]
    barrier = threading.Barrier(4)
    results = []

    def hit():
        barrier.wait()
        resp = client.get(CALLBACK_PATH, params={"code": secrets.token_urlsafe(32), "state": state},
                          follow_redirects=False)
        results.append(parse_qs(urlsplit(resp.headers["location"]).fragment)["meta_catalog_consent"][0])

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(results) == ["connected", "replayed", "replayed", "replayed"]
    assert exchanges.count("code") == 1
    with factory() as s:
        rows = s.execute(text(f"SELECT tenant_id, catalog_id, status, access_token_enc FROM {TABLE}")).fetchall()
        assert s.execute(text("SELECT count(*) FROM whatsapp_connections")).scalar() == 0
    assert len(rows) == 1 and rows[0][:3] == (1, CATALOG, "active")
    assert rows[0][3].startswith("enc1:") and token not in rows[0][3]
