"""Dormant Shopify connection foundation against REAL PostgreSQL.

Throw-away databases are cloned from a template that holds ``tenants`` and
``users`` (from ``models.Base``) and the Shopify tables created by revision
0121 itself. Shopify is an in-process, thread-safe fake with barrier hooks:
no network, no merchant, every credential generated at runtime. The fake
models Shopify's documented behaviour that matters here: one current
expiring offline token pair per app and store (a new exchange or refresh
retires the previous refresh token), uninstall revokes every credential, and
a refused credential answers 401.

Without ``NAHLA_RELIABILITY_PG_ADMIN_DSN`` the module is skipped (reported as
skipped, never as passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no
variable it fails. Inventoried in the required PostgreSQL proofs.

Neutral generic merchants and shops only (platform-wide behaviour).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import IntegrityError
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

from services.shopify_connection import crypto, lifecycle, recovery  # noqa: E402
from services.shopify_connection.actor import ActorRejected, revalidate_actor  # noqa: E402
from services.shopify_connection.config import evaluate_availability  # noqa: E402
from services.shopify_connection.models import (  # noqa: E402
    SHOPIFY_TABLE_NAMES,
    ShopifyBase,
    create_shopify_tables,
)
from services.shopify_connection.oauth import (  # noqa: E402
    ShopIdentity,
    ShopifyApiError,
    TokenGrant,
    VerifiedCallback,
    compute_query_hmac,
)
from services.shopify_connection.webhooks import SignedShopIdentity  # noqa: E402

_MIGRATION_PATH = _REPO / "database" / "migrations" / "versions" / "0121_shopify_connection_foundation.py"

SECRET = "shopify-test-secret-" + secrets.token_hex(16)
ENV = {
    "NAHLA_SHOPIFY_CONNECTION_ENABLED": "1",
    "SHOPIFY_CLIENT_ID": "test-client-" + secrets.token_hex(4),
    "SHOPIFY_CLIENT_SECRET": SECRET,
    "SHOPIFY_OAUTH_REDIRECT_URI": "https://api.shop-review.example.test/merchant/integrations/shopify/callback",
    "DASHBOARD_URL": "https://app.shop-review.example.test",
    "SHOPIFY_TOKEN_ENC_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
}
CFG = evaluate_availability(ENV).config
CIPHER = crypto.TokenCipher(CFG.encryption_key) if CFG else None

SHOP_SHOES = "generic-shoes-store.myshopify.com"
SHOP_SHIRTS = "cotton-shirts-demo.myshopify.com"
SHOP_PERFUME = "rose-perfume-demo.myshopify.com"
GIDS = {
    SHOP_SHOES: "gid://shopify/Shop/7100000001",
    SHOP_SHIRTS: "gid://shopify/Shop/7100000002",
    SHOP_PERFUME: "gid://shopify/Shop/7100000003",
}


# ── Database fixtures ─────────────────────────────────────────────────────────

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


def _load_migration():
    spec = importlib.util.spec_from_file_location("shopify_migration_0121", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(conn, direction: str = "upgrade") -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _load_migration()
    with Operations.context(MigrationContext.configure(conn)):
        getattr(module, direction)()


def _base_tables(conn) -> None:
    from models import Base, Tenant, User

    Base.metadata.create_all(conn, tables=[Tenant.__table__, User.__table__])


def _fresh_db(prefix: str, *, template: str | None = None) -> str:
    name = f"{prefix}_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name}" + (f" TEMPLATE {template}" if template else ""))
    return name


def _drop(name: str) -> None:
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(scope="module")
def template_db():
    name = _fresh_db("shp_tmpl")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _run_migration(conn)
    finally:
        eng.dispose()
    try:
        yield name
    finally:
        _drop(name)


@pytest.fixture()
def pg(template_db, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    name = _fresh_db("shp", template=template_db)
    eng = create_engine(_url(name), pool_size=12, max_overflow=12, future=True)
    factory = sessionmaker(bind=eng, expire_on_commit=False, future=True)
    try:
        yield SimpleNamespace(name=name, engine=eng, Session=factory)
    finally:
        eng.dispose()
        _drop(name)


def _sql(pg, statement: str, **params):
    with pg.engine.begin() as conn:
        result = conn.execute(text(statement), params)
        try:
            return result.mappings().all()
        except Exception:  # noqa: BLE001 — statement without rows
            return []


class Merchant:
    def __init__(self, pg, label: str):
        self.tenant_id = _sql(pg, "INSERT INTO tenants (name, is_active, is_platform_tenant) "
                                  "VALUES (:n, true, false) RETURNING id", n=f"متجر تجريبي عام {label}")[0]["id"]
        self.user_id = _sql(pg, "INSERT INTO users (username, email, role, is_active, email_verified, tenant_id) "
                                "VALUES (:u, :e, 'merchant', true, true, :t) RETURNING id",
                            u=f"merchant-{label}-{secrets.token_hex(3)}", e=f"{label}-{secrets.token_hex(3)}@example.test",
                            t=self.tenant_id)[0]["id"]
        self.jti = secrets.token_hex(16)

    def payload(self, **overrides):
        base = {"tenant_id": self.tenant_id, "user_id": self.user_id, "role": "merchant", "jti": self.jti}
        base.update(overrides)
        return base

    def actor(self, pg, **overrides):
        with pg.Session() as db:
            return revalidate_actor(db, self.payload(**overrides))


# ── Fake Shopify ──────────────────────────────────────────────────────────────

class FakeShopify:
    """Thread-safe; one current token pair per shop; uninstall revokes everything."""

    def __init__(self):
        self.lock = threading.Lock()
        self.codes = {}
        self.valid_access = {}      # access token -> shop
        self.valid_refresh = {}     # refresh token -> shop
        self.current = {}           # shop -> (access, refresh)
        self.calls = []
        self.exchange_hook = None
        self.refresh_hook = None
        self.identity_hook = None
        self.identity_error = None
        self.refresh_error = None
        self.exchange_delay = 0.0

    def count(self, kind):
        with self.lock:
            return sum(1 for k, _ in self.calls if k == kind)

    def issue_code(self, shop):
        code = secrets.token_hex(16)
        with self.lock:
            self.codes[code] = shop
        return code

    def _issue(self, shop):
        access, refresh = f"shpat_{secrets.token_hex(16)}", f"shprt_{secrets.token_hex(16)}"
        old = self.current.get(shop)
        if old:
            self.valid_refresh.pop(old[1], None)   # the previous refresh token is retired
        self.valid_access[access] = shop            # retired access tokens stay usable until expiry
        self.valid_refresh[refresh] = shop
        self.current[shop] = (access, refresh)
        return TokenGrant(access, refresh, 3600, 7776000, frozenset({"read_products"}))

    def uninstall(self, shop):
        with self.lock:
            for table in (self.valid_access, self.valid_refresh):
                for token in [t for t, s in table.items() if s == shop]:
                    table.pop(token)
            self.current.pop(shop, None)

    async def exchange_code(self, *, shop_domain, code):
        with self.lock:
            self.calls.append(("exchange", shop_domain))
        if self.exchange_hook:
            self.exchange_hook(shop_domain)
        if self.exchange_delay:
            await asyncio.sleep(self.exchange_delay)   # cancellable: no grant if abandoned first
        with self.lock:
            if self.codes.pop(code, None) != shop_domain:
                raise ShopifyApiError("permanent", "grant_rejected")
            return self._issue(shop_domain)

    async def refresh(self, *, shop_domain, refresh_token):
        with self.lock:
            self.calls.append(("refresh", shop_domain))
        if self.refresh_error:
            raise self.refresh_error
        with self.lock:
            if self.valid_refresh.get(refresh_token) != shop_domain:
                raise ShopifyApiError("permanent", "grant_rejected")
            grant = self._issue(shop_domain)
        if self.refresh_hook:
            self.refresh_hook(shop_domain)
        return grant

    async def shop_identity(self, *, shop_domain, access_token):
        with self.lock:
            self.calls.append(("identity", shop_domain))
        if self.identity_hook:
            self.identity_hook(shop_domain, access_token)
        if self.identity_error:
            raise self.identity_error
        with self.lock:
            if self.valid_access.get(access_token) != shop_domain:
                raise ShopifyApiError("rejected", "credentials_rejected")
        return ShopIdentity(shop_gid=GIDS[shop_domain], shop_domain=shop_domain)


def run(coro):
    return asyncio.run(coro)


def start(pg, merchant, shop, **kw):
    actor = merchant.actor(pg)
    with pg.Session() as db:
        url, _ = lifecycle.begin_authorization(db, actor=actor, shop_domain=shop, config=CFG, **kw)
    return parse_qs(urlsplit(url).query)["state"][0]


def callback(pg, shop, state, code, **kw):
    with pg.Session() as db:
        return lifecycle.accept_callback(db, callback=VerifiedCallback(shop, state, code), config=CFG,
                                         cipher=CIPHER, **kw)


def complete(pg, merchant, handle, fake, actor=None):
    actor = actor or merchant.actor(pg)
    with pg.Session() as db:
        return run(lifecycle.complete_authorization(db, actor=actor, handle=handle, config=CFG, cipher=CIPHER,
                                                    api=fake))


def ready(pg, merchant, shop, fake):
    """Start + callback: returns the completion handle."""
    state = start(pg, merchant, shop)
    return callback(pg, shop, state, fake.issue_code(shop))


def connect(pg, merchant, shop, fake):
    return complete(pg, merchant, ready(pg, merchant, shop, fake), fake)


def row(pg, shop):
    rows = _sql(pg, "SELECT * FROM shopify_connections WHERE shop_domain = :s", s=shop)
    return dict(rows[0]) if rows else None


def state_row(pg, handle):
    return dict(_sql(pg, "SELECT * FROM shopify_oauth_states WHERE completion_handle_hash = :h",
                     h=lifecycle.completion_handle_hash(handle))[0])


def stored_tokens(pg, shop):
    r = row(pg, shop)
    ctx = dict(tenant_id=r["tenant_id"], shop_domain=shop, generation=r["generation"])
    return (CIPHER.decrypt(r["access_token_enc"], crypto.token_context(crypto.PURPOSE_ACCESS_TOKEN, **ctx)),
            CIPHER.decrypt(r["refresh_token_enc"], crypto.token_context(crypto.PURPOSE_REFRESH_TOKEN, **ctx)))


def refused(fn, *args, **kwargs) -> str:
    with pytest.raises(lifecycle.ConnectionRefused) as info:
        fn(*args, **kwargs)
    return info.value.code


def uninstall_event(pg, shop, *, webhook_id, digest, triggered_at=None, shop_id=None):
    identity = SignedShopIdentity(shop_id=shop_id or int(GIDS[shop].rsplit("/", 1)[1]), shop_domain=shop)
    with pg.Session() as db:
        return lifecycle.record_uninstall(db, identity=identity, webhook_id=webhook_id, body_sha256=digest,
                                          triggered_at_header=triggered_at)


def reconcile(pg, shop, fake, now=None):
    with pg.Session() as db:
        return run(lifecycle.reconcile(db, connection_id=row(pg, shop)["id"], api=fake, cipher=CIPHER, now=now))


def in_thread(fn, *args, **kwargs):
    box = {}

    def target():
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — surfaced to the test
            box["error"] = exc

    th = threading.Thread(target=target)
    th.start()
    return th, box


def barrier_hook():
    entered, release = threading.Event(), threading.Event()

    def hook(*_args):
        entered.set()
        assert release.wait(20), "barrier not released"

    return hook, entered, release


# ── Migration ─────────────────────────────────────────────────────────────────

def _schema(eng):
    insp = inspect(eng)
    out = {}
    for t in SHOPIFY_TABLE_NAMES:
        out[t] = {
            "columns": sorted((c["name"], str(c["type"]), bool(c["nullable"])) for c in insp.get_columns(t)),
            "pk": insp.get_pk_constraint(t).get("constrained_columns"),
            "uniques": sorted((u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(t)),
            "checks": sorted((c["name"], " ".join(c["sqltext"].split())) for c in insp.get_check_constraints(t)),
            "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]),
                           (f.get("options") or {}).get("ondelete")) for f in insp.get_foreign_keys(t)),
            "indexes": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in insp.get_indexes(t)),
        }
    return out


def test_migration_schema_equals_model_metadata():
    by_migration, by_model = _fresh_db("shp_mig"), _fresh_db("shp_mod")
    try:
        e1 = create_engine(_url(by_migration), poolclass=NullPool, future=True)
        e2 = create_engine(_url(by_model), poolclass=NullPool, future=True)
        with e1.begin() as conn:
            _base_tables(conn)
            _run_migration(conn)
        with e2.begin() as conn:
            _base_tables(conn)
            create_shopify_tables(conn)
        a, b = _schema(e1), _schema(e2)
        e1.dispose()
        e2.dispose()
        assert a == b
        conn_checks = dict(a["shopify_connections"]["checks"])
        assert "ck_shopify_connections_credentials" in conn_checks
        assert ("uq_shopify_connections_shop_domain", ("shop_domain",)) in a["shopify_connections"]["uniques"]
        assert ("uq_shopify_connections_shop_gid", ("shop_gid",)) in a["shopify_connections"]["uniques"]
    finally:
        _drop(by_migration)
        _drop(by_model)


@pytest.mark.parametrize("shape", ["full_model_tables", "partial_one_table", "malformed_table"])
def test_migration_refuses_any_preexisting_shopify_table_and_changes_nothing(shape):
    name = _fresh_db("shp_pre")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            if shape == "full_model_tables":
                create_shopify_tables(conn)
            elif shape == "partial_one_table":
                ShopifyBase.metadata.tables["shopify_shop_leases"].create(conn)
            else:
                conn.execute(text("CREATE TABLE shopify_connections (id integer, status text CHECK (true))"))
        before = _schema_or_names(eng)
        with pytest.raises(RuntimeError, match="already exist"):
            with eng.begin() as conn:
                _run_migration(conn)
        assert _schema_or_names(eng) == before
    finally:
        eng.dispose()
        _drop(name)


def _schema_or_names(eng):
    insp = inspect(eng)
    names = sorted(insp.get_table_names())
    cols = {t: sorted((c["name"], str(c["type"])) for c in insp.get_columns(t)) for t in names}
    return names, cols


def test_downgrade_drops_only_the_shopify_tables():
    name = _fresh_db("shp_down")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _run_migration(conn)
        assert set(SHOPIFY_TABLE_NAMES) <= set(inspect(eng).get_table_names())
        with eng.begin() as conn:
            _run_migration(conn, "downgrade")
        remaining = set(inspect(eng).get_table_names())
        assert not (set(SHOPIFY_TABLE_NAMES) & remaining)
        assert {"tenants", "users"} <= remaining
    finally:
        eng.dispose()
        _drop(name)


def test_alembic_chain_upgrades_0120_to_0121_and_back():
    name = _fresh_db("shp_chain")
    env = {**os.environ, "DATABASE_URL": _url(name)}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(_REPO / "database"), env=env,
                              capture_output=True, text=True, timeout=600)

    try:
        for target in ("0093", "0111", "0113", "0120", "0121"):
            proc = alembic("upgrade", target)
            assert proc.returncode == 0, proc.stderr[-3000:]
        eng = create_engine(_url(name), poolclass=NullPool, future=True)
        with eng.connect() as conn:
            versions = set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
        assert "0121" in versions and "0120" not in versions
        assert set(SHOPIFY_TABLE_NAMES) <= set(inspect(eng).get_table_names())
        proc = alembic("downgrade", "0121@-1")
        assert proc.returncode == 0, proc.stderr[-3000:]
        with eng.connect() as conn:
            versions = set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
        assert "0120" in versions and "0121" not in versions
        assert not (set(SHOPIFY_TABLE_NAMES) & set(inspect(eng).get_table_names()))
        assert "meta_catalog_authorizations" in inspect(eng).get_table_names()
        eng.dispose()
    finally:
        _drop(name)


# ── Ownership, isolation, no squatting ────────────────────────────────────────

def test_connect_two_tenants_isolated(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    summary = connect(pg, a, SHOP_SHOES, fake)
    assert summary["status"] == "active" and summary["scopes"] == ["read_products"]
    with pg.Session() as db:
        assert [c["shop_domain"] for c in lifecycle.connection_summaries(db, a.tenant_id)] == [SHOP_SHOES]
        assert lifecycle.connection_summaries(db, b.tenant_id) == []
    assert refused(start, pg, b, SHOP_SHOES) == lifecycle.C_SHOP_UNAVAILABLE
    with pg.Session() as db:
        assert refused(lifecycle.disconnect, db, actor=b.actor(pg), shop_domain=SHOP_SHOES) == lifecycle.C_NOT_FOUND
    conn_id = row(pg, SHOP_SHOES)["id"]
    with pg.Session() as db:
        code = refused(run, lifecycle.active_access_token(db, tenant_id=b.tenant_id, connection_id=conn_id,
                                                          api=fake, cipher=CIPHER))
    assert code == lifecycle.C_NOT_FOUND
    with pg.Session() as db:
        token, generation = run(lifecycle.active_access_token(db, tenant_id=a.tenant_id, connection_id=conn_id,
                                                              api=fake, cipher=CIPHER))
    assert token == fake.current[SHOP_SHOES][0] and generation == 1
    assert row(pg, SHOP_SHOES)["tenant_id"] == a.tenant_id


def test_pending_or_expired_authorization_never_reserves_the_shop(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    state_a = start(pg, a, SHOP_SHIRTS)
    assert row(pg, SHOP_SHIRTS) is None
    # Tenant B may start and finish while A's authorization is pending.
    state_b = start(pg, b, SHOP_SHIRTS)
    assert row(pg, SHOP_SHIRTS) is None
    # A's state expires unused: refused and burned, no row.
    late = lifecycle.utcnow() + timedelta(seconds=lifecycle.STATE_TTL_SECONDS + 5)
    assert refused(callback, pg, SHOP_SHIRTS, state_a, fake.issue_code(SHOP_SHIRTS), now=late) == lifecycle.C_EXPIRED
    assert row(pg, SHOP_SHIRTS) is None
    handle_b = callback(pg, SHOP_SHIRTS, state_b, fake.issue_code(SHOP_SHIRTS))
    complete(pg, b, handle_b, fake)
    assert row(pg, SHOP_SHIRTS)["tenant_id"] == b.tenant_id
    assert refused(start, pg, a, SHOP_SHIRTS) == lifecycle.C_SHOP_UNAVAILABLE


def test_failed_exchange_never_reserves_the_shop(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    state = start(pg, a, SHOP_PERFUME)
    handle = callback(pg, SHOP_PERFUME, state, "code-shopify-will-refuse")
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_EXCHANGE_FAILED
    assert row(pg, SHOP_PERFUME) is None
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0
    assert state_row(pg, handle)["status"] == "failed" and state_row(pg, handle)["code_enc"] is None
    connect(pg, b, SHOP_PERFUME, fake)
    assert row(pg, SHOP_PERFUME)["tenant_id"] == b.tenant_id


def test_verified_ownership_survives_disconnect_and_blocks_other_tenant(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    state_b = start(pg, b, SHOP_SHOES)          # B started before A owned the shop
    state_b2 = start(pg, b, SHOP_SHOES)
    connect(pg, a, SHOP_SHOES, fake)
    # B's earlier authorization cannot finish against a shop A now owns.
    assert refused(callback, pg, SHOP_SHOES, state_b, fake.issue_code(SHOP_SHOES)) == lifecycle.C_SHOP_UNAVAILABLE
    with pg.Session() as db:
        lifecycle.disconnect(db, actor=a.actor(pg), shop_domain=SHOP_SHOES)
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "disconnected" and r["tenant_id"] == a.tenant_id
    assert r["access_token_enc"] is None and r["refresh_token_enc"] is None
    with pg.Session() as db:   # idempotent
        assert lifecycle.disconnect(db, actor=a.actor(pg), shop_domain=SHOP_SHOES)["status"] == "disconnected"
    # Disconnect fails every in-flight authorization for the shop, any tenant's.
    assert refused(callback, pg, SHOP_SHOES, state_b2, fake.issue_code(SHOP_SHOES)) == lifecycle.C_REPLAYED
    # The tombstone keeps ownership: B cannot start, A may reconnect.
    assert refused(start, pg, b, SHOP_SHOES) == lifecycle.C_SHOP_UNAVAILABLE
    assert fake.count("exchange") == 1
    connect(pg, a, SHOP_SHOES, fake)
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["tenant_id"] == a.tenant_id and r["generation"] == 3
    # The database itself refuses a second row for the shop, tombstone or not.
    with pytest.raises(IntegrityError):
        _sql(pg, "INSERT INTO shopify_connections (tenant_id, shop_domain, shop_gid, status, generation, "
                 "credential_version, reconcile_attempts) VALUES (:t, :s, 'gid://shopify/Shop/1', "
                 "'disconnected', 1, 1, 0)", t=b.tenant_id, s=SHOP_SHOES)


# ── State, callback and completion binding ────────────────────────────────────

def test_callback_refusals(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    assert refused(callback, pg, SHOP_SHOES, "forged-" + secrets.token_urlsafe(24), "c") == lifecycle.C_INVALID_STATE
    state = start(pg, a, SHOP_SHOES)
    assert refused(callback, pg, SHOP_SHIRTS, state, "c") == lifecycle.C_SHOP_MISMATCH
    # The mismatch burned the state: the genuine shop cannot use it afterwards.
    assert refused(callback, pg, SHOP_SHOES, state, fake.issue_code(SHOP_SHOES)) == lifecycle.C_REPLAYED
    state2 = start(pg, a, SHOP_SHOES)
    handle = callback(pg, SHOP_SHOES, state2, fake.issue_code(SHOP_SHOES))
    assert handle
    assert refused(callback, pg, SHOP_SHOES, state2, fake.issue_code(SHOP_SHOES)) == lifecycle.C_REPLAYED
    raw = state_row(pg, handle)
    assert raw["code_enc"].startswith(crypto.PREFIX) and raw["status"] == "callback_received"


def test_concurrent_callbacks_consume_a_state_once(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    state = start(pg, a, SHOP_SHOES)
    threads = [in_thread(callback, pg, SHOP_SHOES, state, fake.issue_code(SHOP_SHOES)) for _ in range(8)]
    for th, _ in threads:
        th.join(30)
    handles = [box["value"] for _, box in threads if "value" in box]
    errors = [box["error"].code for _, box in threads if "error" in box]
    assert len(handles) == 1 and errors == [lifecycle.C_REPLAYED] * 7


def test_concurrent_completions_exchange_once(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    handle = ready(pg, a, SHOP_SHOES, fake)
    actor = a.actor(pg)
    threads = [in_thread(complete, pg, a, handle, fake, actor) for _ in range(6)]
    for th, _ in threads:
        th.join(30)
    ok = [box["value"] for _, box in threads if "value" in box]
    errors = sorted(box["error"].code for _, box in threads if "error" in box)
    assert len(ok) == 1
    assert set(errors) <= {lifecycle.C_REPLAYED, lifecycle.C_EXCHANGE_IN_PROGRESS} and len(errors) == 5
    assert fake.count("exchange") == 1
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_connections")[0]["n"] == 1


def test_completion_requires_same_actor_and_session(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    handle = ready(pg, a, SHOP_SHOES, fake)
    # Another tenant presenting A's handle: refused and the authorization burned.
    assert refused(complete, pg, b, handle, fake) == lifecycle.C_SESSION_MISMATCH
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_REPLAYED
    # Same user, new session (logout + login): a new jti does not inherit intent.
    handle2 = ready(pg, a, SHOP_SHOES, fake)
    new_session = a.actor(pg, jti=secrets.token_hex(16))
    assert refused(complete, pg, a, handle2, fake, new_session) == lifecycle.C_SESSION_MISMATCH
    assert fake.count("exchange") == 0 and row(pg, SHOP_SHOES) is None


def test_callback_refuses_a_deactivated_initiator(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    state = start(pg, a, SHOP_SHOES)
    _sql(pg, "UPDATE users SET is_active = false WHERE id = :u", u=a.user_id)
    assert refused(callback, pg, SHOP_SHOES, state, fake.issue_code(SHOP_SHOES)) == lifecycle.C_ACTOR_REVOKED


@pytest.mark.parametrize("change", ["support_impersonation", "platform_admin", "staff_role", "missing_jti"])
def test_actor_claims_refused_before_any_database_write(pg, change):
    a = Merchant(pg, "a")
    overrides = {
        "support_impersonation": {"impersonation": True, "role": "support_impersonation"},
        "platform_admin": {"role": "admin"},
        "staff_role": {"role": "staff"},
        "missing_jti": {"jti": ""},
    }[change]
    with pytest.raises(ActorRejected):
        a.actor(pg, **overrides)


def test_actor_db_revalidation(pg):
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    _sql(pg, "UPDATE users SET role = 'staff' WHERE id = :u", u=a.user_id)
    with pytest.raises(ActorRejected, match="role_not_permitted"):
        a.actor(pg)
    _sql(pg, "UPDATE users SET role = 'merchant', tenant_id = :t WHERE id = :u", t=b.tenant_id, u=a.user_id)
    with pytest.raises(ActorRejected, match="actor_tenant_mismatch"):
        a.actor(pg)
    _sql(pg, "UPDATE users SET tenant_id = :t WHERE id = :u", t=a.tenant_id, u=a.user_id)
    _sql(pg, "UPDATE tenants SET is_active = false WHERE id = :t", t=a.tenant_id)
    with pytest.raises(ActorRejected, match="tenant_inactive"):
        a.actor(pg)


@pytest.mark.parametrize("change", ["deactivated", "reassigned", "demoted", "session_revoked"])
def test_claim_rechecks_the_actor_after_the_network_window(pg, change):
    from core import token_revocation

    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    handle = ready(pg, a, SHOP_SHOES, fake)
    actor = a.actor(pg)

    def during_exchange(_shop):
        if change == "deactivated":
            _sql(pg, "UPDATE users SET is_active = false WHERE id = :u", u=a.user_id)
        elif change == "reassigned":
            _sql(pg, "UPDATE users SET tenant_id = :t WHERE id = :u", t=b.tenant_id, u=a.user_id)
        elif change == "demoted":
            _sql(pg, "UPDATE users SET role = 'staff' WHERE id = :u", u=a.user_id)
        else:
            # Inherited best-effort denylist (in-process fallback without Redis).
            token_revocation.revoke_jti(a.jti, int(time.time()) + 3600)

    fake.exchange_hook = during_exchange
    try:
        assert refused(complete, pg, a, handle, fake, actor) == lifecycle.C_ACTOR_REVOKED
    finally:
        token_revocation._LOCAL_REVOKED.pop(a.jti, None)
    assert row(pg, SHOP_SHOES) is None
    assert state_row(pg, handle)["status"] == "failed"
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0


# ── Per-shop exchange lease ───────────────────────────────────────────────────

def test_competing_tenants_exchange_once_and_the_winner_keeps_working_credentials(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    handle_a, handle_b = ready(pg, a, SHOP_SHOES, fake), ready(pg, b, SHOP_SHOES, fake)
    hook, entered, release = barrier_hook()
    fake.exchange_hook = hook
    th, box = in_thread(complete, pg, a, handle_a, fake, a.actor(pg))
    assert entered.wait(20)
    fake.exchange_hook = None
    # B passes every ownership check, but cannot exchange while A's is in flight.
    assert refused(complete, pg, b, handle_b, fake) == lifecycle.C_EXCHANGE_IN_PROGRESS
    assert state_row(pg, handle_b)["status"] == "callback_received"   # nothing spent
    release.set()
    th.join(30)
    assert box.get("value", {}).get("status") == "active", box
    # B's retry is refused under the lease, before any call to Shopify.
    assert refused(complete, pg, b, handle_b, fake) == lifecycle.C_SHOP_UNAVAILABLE
    assert fake.count("exchange") == 1
    assert row(pg, SHOP_SHOES)["tenant_id"] == a.tenant_id
    # The winner's stored refresh token was never retired by a losing exchange.
    with pg.Session() as db:
        out = run(lifecycle.refresh_credentials(db, connection_id=row(pg, SHOP_SHOES)["id"], api=fake, cipher=CIPHER))
    assert out.status == "refreshed"
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]


def test_stale_lease_is_taken_over(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    handle = ready(pg, a, SHOP_SHOES, fake)
    _sql(pg, "INSERT INTO shopify_shop_leases (shop_domain, lease_id, purpose, tenant_id, acquired_at, expires_at) "
             "VALUES (:s, 'crashed-worker', 'exchange', :t, now() - interval '10 minutes', "
             "now() - interval '9 minutes')", s=SHOP_SHOES, t=a.tenant_id)
    assert complete(pg, a, handle, fake)["status"] == "active"
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0


def test_lost_lease_discards_the_exchange_result(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    handle = ready(pg, a, SHOP_SHOES, fake)
    fake.exchange_hook = lambda shop: _sql(pg, "UPDATE shopify_shop_leases SET lease_id = 'taken-over' "
                                               "WHERE shop_domain = :s", s=shop)
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_SUPERSEDED
    assert row(pg, SHOP_SHOES) is None
    assert _sql(pg, "SELECT lease_id FROM shopify_shop_leases")[0]["lease_id"] == "taken-over"


def test_reinstall_waits_for_an_in_flight_refresh(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    conn_id = row(pg, SHOP_SHOES)["id"]
    handle = ready(pg, a, SHOP_SHOES, fake)
    hook, entered, release = barrier_hook()
    fake.refresh_hook = hook

    def do_refresh():
        with pg.Session() as db:
            return run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER))

    th, box = in_thread(do_refresh)
    assert entered.wait(20)
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_EXCHANGE_IN_PROGRESS
    release.set()
    th.join(30)
    fake.refresh_hook = None
    assert box["value"].status == "refreshed"
    assert complete(pg, a, handle, fake)["status"] == "active"
    r = row(pg, SHOP_SHOES)
    assert r["generation"] == 2 and fake.count("exchange") == 2
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]
    with pg.Session() as db:   # the stored pair is Shopify's current one: it refreshes
        assert run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER)).status == "refreshed"


def test_refresh_waits_for_an_in_flight_reinstall(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    conn_id = row(pg, SHOP_SHOES)["id"]
    handle = ready(pg, a, SHOP_SHOES, fake)
    hook, entered, release = barrier_hook()
    fake.exchange_hook = hook
    th, box = in_thread(complete, pg, a, handle, fake, a.actor(pg))
    assert entered.wait(20)
    with pg.Session() as db:
        assert run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER)).status == "in_progress"
    assert fake.count("refresh") == 0
    release.set()
    th.join(30)
    assert box["value"]["status"] == "active"
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]


# ── Refresh rotation and races ────────────────────────────────────────────────

def test_refresh_rotation_and_concurrent_refresh(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    conn_id = row(pg, SHOP_SHOES)["id"]
    hook, entered, release = barrier_hook()
    fake.refresh_hook = hook

    def do_refresh():
        with pg.Session() as db:
            return run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER))

    th, box = in_thread(do_refresh)
    assert entered.wait(20)
    assert do_refresh().status == "in_progress"
    release.set()
    th.join(30)
    assert box["value"].status == "refreshed" and box["value"].credential_version == 2
    assert fake.count("refresh") == 1
    r = row(pg, SHOP_SHOES)
    assert r["credential_version"] == 2 and r["generation"] == 1
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]


def test_refresh_racing_disconnect_never_revives(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    conn_id = row(pg, SHOP_SHOES)["id"]
    actor = a.actor(pg)

    def disconnect_now(_shop):
        with pg.Session() as db:
            lifecycle.disconnect(db, actor=actor, shop_domain=SHOP_SHOES)

    fake.refresh_hook = disconnect_now
    with pg.Session() as db:
        assert run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER)).status == "superseded"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "disconnected" and r["access_token_enc"] is None and r["refresh_token_enc"] is None
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0


@pytest.mark.parametrize("failure", ["permanent", "transient", "expired_refresh_token"])
def test_refresh_failures(pg, failure):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    conn_id = row(pg, SHOP_SHOES)["id"]
    before = stored_tokens(pg, SHOP_SHOES)
    if failure == "permanent":
        fake.refresh_error = ShopifyApiError("permanent", "grant_rejected")
    elif failure == "transient":
        fake.refresh_error = ShopifyApiError("transient", "upstream_unavailable")
    else:
        _sql(pg, "UPDATE shopify_connections SET refresh_token_expires_at = now() - interval '1 minute'")
    with pg.Session() as db:
        out = run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER))
    r = row(pg, SHOP_SHOES)
    if failure == "transient":
        assert out.status == "deferred" and r["status"] == "active" and stored_tokens(pg, SHOP_SHOES) == before
    else:
        assert out.status == "rejected" and r["status"] == "reauth_required"
        assert r["access_token_enc"] is None and r["refresh_token_enc"] is None and r["generation"] == 2
    if failure == "expired_refresh_token":
        assert fake.count("refresh") == 0
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0


# ── Uninstall: quarantine, reconcile, replay, generations ────────────────────

def test_genuine_uninstall_is_confirmed_and_keeps_the_tombstone(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    connect(pg, a, SHOP_SHOES, fake)
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest="d" * 64)[0] == "quarantined"
    assert row(pg, SHOP_SHOES)["status"] == "quarantined"
    with pg.Session() as db:   # a quarantined connection never yields a credential
        assert refused(run, lifecycle.active_access_token(db, tenant_id=a.tenant_id,
                                                           connection_id=row(pg, SHOP_SHOES)["id"],
                                                           api=fake, cipher=CIPHER)) == lifecycle.C_UNAVAILABLE
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "uninstalled" and r["access_token_enc"] is None and r["generation"] == 2
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest="d" * 64) == ("duplicate", None)
    assert refused(start, pg, b, SHOP_SHOES) == lifecycle.C_SHOP_UNAVAILABLE
    connect(pg, a, SHOP_SHOES, fake)
    assert row(pg, SHOP_SHOES)["status"] == "active"


def test_unknown_shop_changes_nothing(pg):
    assert uninstall_event(pg, SHOP_PERFUME, webhook_id="w-x", digest="e" * 64) == ("unknown_shop", None)
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_connections")[0]["n"] == 0


def test_delayed_old_uninstall_after_same_tenant_reinstall_is_retained(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    fake.uninstall(SHOP_SHOES)            # genuine uninstall at Shopify; its webhook is delayed
    connect(pg, a, SHOP_SHOES, fake)       # merchant reinstalls through Nahlah
    assert row(pg, SHOP_SHOES)["generation"] == 2
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-late", digest="f" * 64,
                           triggered_at="2020-01-01T00:00:00Z")[0] == "quarantined"
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["generation"] == 2


def test_old_event_with_expired_access_token_refreshes_before_concluding(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-old", digest="1" * 64)
    _sql(pg, "UPDATE shopify_connections SET access_token_expires_at = now() - interval '5 minutes'")
    # Shopify also stops accepting the expired access token itself.
    expired_access = stored_tokens(pg, SHOP_SHOES)[0]
    fake.valid_access.pop(expired_access)
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["credential_version"] == 2 and r["generation"] == 1
    assert fake.count("refresh") == 1
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]


def test_expired_access_token_and_revoked_refresh_confirms_uninstall(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-real", digest="2" * 64)
    _sql(pg, "UPDATE shopify_connections SET access_token_expires_at = now() - interval '5 minutes'")
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"
    assert row(pg, SHOP_SHOES)["status"] == "uninstalled"


def test_rejected_old_credential_never_erases_a_newer_pair(pg):
    """Barrier: the probe with the old pair is refused only after a refresh
    has committed a newer pair in the same generation."""
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    conn_id = row(pg, SHOP_SHOES)["id"]
    _sql(pg, "UPDATE shopify_connections SET revalidation_requested_at = now() WHERE id = :i", i=conn_id)
    old_access = stored_tokens(pg, SHOP_SHOES)[0]

    def do_refresh():
        with pg.Session() as db:
            return run(lifecycle.refresh_credentials(db, connection_id=conn_id, api=fake, cipher=CIPHER))

    def refresh_then_refuse_old(_shop, token):
        if token != old_access:
            return
        th, box = in_thread(do_refresh)          # another worker, its own loop and session
        th.join(30)
        assert box["value"].status == "refreshed"
        fake.valid_access.pop(old_access, None)   # the old access token is now refused

    fake.identity_hook = refresh_then_refuse_old
    assert reconcile(pg, SHOP_SHOES, fake) == "superseded"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["credential_version"] == 2 and r["access_token_enc"] is not None
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]
    fake.identity_hook = None
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"


def test_identical_signed_body_across_generations_is_never_suppressed(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    body = hashlib.sha256(b'{"id":7100000001,"myshopify_domain":"generic-shoes-store.myshopify.com"}').hexdigest()
    connect(pg, a, SHOP_SHOES, fake)                                      # generation 1
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest=body)[0] == "quarantined"
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"              # generation 2 (tombstone)
    connect(pg, a, SHOP_SHOES, fake)                                      # generation 3
    # Replays of the old body with altered unsigned headers: probed, retained.
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-replay-1", digest=body,
                           triggered_at="2099-01-01T00:00:00Z")[0] == "quarantined"
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-replay-2", digest=body,
                           triggered_at="1999-01-01T00:00:00Z")[0] == "revalidate_only"
    assert row(pg, SHOP_SHOES)["status"] == "active"                     # no suspension for a known-stale body
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    # A later genuine uninstall whose signed body is byte-identical.
    fake.uninstall(SHOP_SHOES)
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-genuine", digest=body)[0] == "revalidate_only"
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"
    assert row(pg, SHOP_SHOES)["status"] == "uninstalled"


def test_same_delivery_id_and_body_across_reinstall_requests_a_probe(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest="3" * 64)
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"
    connect(pg, a, SHOP_SHOES, fake)
    outcome, conn_id = uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest="3" * 64)
    assert outcome == "duplicate" and conn_id == row(pg, SHOP_SHOES)["id"]
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["revalidation_requested_at"] is not None
    fake.uninstall(SHOP_SHOES)       # the current install really was removed meanwhile
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"


def test_same_delivery_id_after_a_retained_probe_still_probes(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-9", digest="4" * 64)
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-9", digest="4" * 64)[0] == "duplicate"
    assert row(pg, SHOP_SHOES)["revalidation_requested_at"] is not None
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-9", digest="4" * 64)[0] == "duplicate"
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"


def test_reused_delivery_id_with_another_body_is_a_new_event(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-5", digest="5" * 64)
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-5", digest="6" * 64)[0] == "quarantined"
    events = _sql(pg, "SELECT webhook_id, body_sha256 FROM shopify_webhook_events ORDER BY id")
    assert [(e["webhook_id"], e["body_sha256"][0]) for e in events] == [("w-5", "5"), (None, "6")]


def test_signed_identity_mismatch_quarantines_and_probe_decides(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-m", digest="7" * 64, shop_id=9999999)[0] == "quarantined"
    assert row(pg, SHOP_SHOES)["quarantine_reason"] == "uninstall_identity_mismatch"
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"


def test_uninstall_arriving_during_a_probe_keeps_the_quarantine(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-a", digest="8" * 64)
    fake.identity_hook = lambda shop, _t: uninstall_event(pg, shop, webhook_id="w-b", digest="9" * 64)
    assert reconcile(pg, SHOP_SHOES, fake) == "deferred"
    assert row(pg, SHOP_SHOES)["status"] == "quarantined"
    fake.identity_hook = None
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"


def test_in_flight_authorization_after_confirmed_uninstall_must_restart(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-u", digest="a" * 64)
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_REPLAYED
    assert row(pg, SHOP_SHOES)["status"] == "uninstalled"
    connect(pg, a, SHOP_SHOES, fake)        # a fresh authenticated authorization
    assert row(pg, SHOP_SHOES)["status"] == "active"


def test_fresh_completion_resolves_a_quarantine(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-q", digest="b" * 64)
    assert complete(pg, a, handle, fake)["status"] == "active"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["quarantined_at"] is None and r["reconcile_next_at"] is None
    assert _sql(pg, "SELECT resolution FROM shopify_webhook_events")[0]["resolution"] == "superseded"


def test_disconnect_during_exchange_supersedes_the_claim(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    actor = a.actor(pg)

    def disconnect_now(_shop):
        with pg.Session() as db:
            lifecycle.disconnect(db, actor=actor, shop_domain=SHOP_SHOES)

    fake.exchange_hook = disconnect_now
    assert refused(complete, pg, a, handle, fake, actor) == lifecycle.C_SUPERSEDED
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "disconnected" and r["access_token_enc"] is None
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0


def test_webhook_waits_at_most_the_lock_timeout(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    holder = pg.engine.connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM shopify_connections WHERE shop_domain = :s FOR UPDATE"), {"s": SHOP_SHOES})
    try:
        began = time.monotonic()
        from sqlalchemy.exc import DBAPIError
        with pytest.raises(DBAPIError):
            uninstall_event(pg, SHOP_SHOES, webhook_id="w-locked", digest="c" * 64)
        assert time.monotonic() - began < 4.5
    finally:
        tx.rollback()
        holder.close()
    # Nothing was acknowledged or stored: the redelivery is processed normally.
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_webhook_events")[0]["n"] == 0
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-locked", digest="c" * 64)[0] == "quarantined"


# ── Durable recovery runner ───────────────────────────────────────────────────

def test_runner_is_dormant_without_flag_and_config():
    def explode():
        raise AssertionError("the database must not be touched")

    for env in ({}, {**ENV, "NAHLA_SHOPIFY_CONNECTION_ENABLED": "0"}, {**ENV, "SHOPIFY_TOKEN_ENC_KEY": ""}):
        assert "skipped" in run(recovery.run_recovery_tick(session_factory=explode, env=env))


def test_runner_recovers_after_a_crash_following_the_ack(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-crash", digest="d1" * 32)   # acked; background never ran
    assert row(pg, SHOP_SHOES)["reconcile_next_at"] is not None
    fake.uninstall(SHOP_SHOES)
    out = run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake))
    assert out["claimed"] == 1 and out.get("uninstalled") == 1
    assert row(pg, SHOP_SHOES)["status"] == "uninstalled"


def test_runner_backs_off_on_transient_failure_then_retains(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-t", digest="d2" * 32)
    t0 = lifecycle.utcnow()
    fake.identity_error = ShopifyApiError("transient", "upstream_unavailable")
    assert run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake, now=t0)).get("deferred") == 1
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "quarantined" and r["reconcile_attempts"] == 1 and r["reconcile_lease_id"] is None
    assert abs((r["reconcile_next_at"] - t0).total_seconds() - recovery.backoff_seconds(1)) < 1
    assert run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake, now=t0))["claimed"] == 0
    fake.identity_error = None
    later = t0 + timedelta(seconds=recovery.backoff_seconds(1) + 1)
    assert run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake, now=later)).get("retained") == 1
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["reconcile_attempts"] == 0 and r["reconcile_next_at"] is None
    assert recovery.backoff_seconds(30) == recovery.BACKOFF_CAP_SECONDS


def test_duplicate_redelivery_reschedules_a_deferred_reconciliation(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-d", digest="d3" * 32)
    fake.identity_error = ShopifyApiError("transient", "upstream_unavailable")
    run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake))
    assert row(pg, SHOP_SHOES)["reconcile_next_at"] > lifecycle.utcnow()
    fake.identity_error = None
    outcome, conn_id = uninstall_event(pg, SHOP_SHOES, webhook_id="w-d", digest="d3" * 32)
    assert outcome == "duplicate" and conn_id is not None
    assert row(pg, SHOP_SHOES)["reconcile_next_at"] <= lifecycle.utcnow()
    assert run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake)).get("retained") == 1


def test_competing_workers_claim_disjoint_rows_and_recover_expired_leases(pg):
    fake = FakeShopify()
    shops = [SHOP_SHOES, SHOP_SHIRTS, SHOP_PERFUME]
    for i, shop in enumerate(shops):
        connect(pg, Merchant(pg, f"m{i}"), shop, fake)
        uninstall_event(pg, shop, webhook_id=f"w-{i}", digest=f"{i}" * 64)
    now = lifecycle.utcnow()
    go = threading.Barrier(2)

    def worker():
        go.wait(10)
        with pg.Session() as db:
            return recovery.claim_due(db, now=now, limit=3)

    threads = [in_thread(worker) for _ in range(2)]
    for th, _ in threads:
        th.join(30)
    claims = [c for _, box in threads for c in box["value"]]
    ids = [c[0] for c in claims]
    assert len(ids) == 3 and len(set(ids)) == 3
    with pg.Session() as db:
        assert recovery.claim_due(db, now=now, limit=3) == []          # leases held
    after_crash = now + timedelta(seconds=recovery.RECONCILE_LEASE_SECONDS + 1)
    with pg.Session() as db:
        assert len(recovery.claim_due(db, now=after_crash, limit=3)) == 3   # crashed workers' leases expired


def test_reinstall_supersedes_a_claimed_reconciliation(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-r", digest="d4" * 32)
    with pg.Session() as db:
        [(conn_id, lease, version)] = recovery.claim_due(db, now=lifecycle.utcnow())
    assert complete(pg, a, handle, fake)["status"] == "active"           # generation 2
    with pg.Session() as db:
        result = run(recovery.run_claimed(db, connection_id=conn_id, lease=lease, claimed_version=version,
                                          api=fake, cipher=CIPHER))
    assert result == "lease_lost"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["generation"] == 2 and r["reconcile_lease_id"] is None


# ── Encryption at rest and exposure ───────────────────────────────────────────

def test_tokens_encrypted_at_rest_and_never_exposed(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    handle = ready(pg, a, SHOP_SHOES, fake)
    summary = complete(pg, a, handle, fake)
    access, refresh = fake.current[SHOP_SHOES]
    r = row(pg, SHOP_SHOES)
    assert r["access_token_enc"].startswith(crypto.PREFIX) and r["refresh_token_enc"].startswith(crypto.PREFIX)
    assert state_row(pg, handle)["code_enc"] is None
    dump = json.dumps([dict(x) for t in SHOPIFY_TABLE_NAMES for x in _sql(pg, f"SELECT * FROM {t}")], default=str)
    assert access not in dump and refresh not in dump and SECRET not in dump
    assert access not in json.dumps(summary) and refresh not in json.dumps(summary)
    with pg.Session() as db:
        from services.shopify_connection.models import ShopifyConnection
        conn = db.get(ShopifyConnection, r["id"])
        assert "sgcm1" not in repr(conn) and access not in repr(conn)
    # A ciphertext moved to another tenant's / generation's context fails.
    other = crypto.token_context(crypto.PURPOSE_ACCESS_TOKEN, tenant_id=a.tenant_id + 1000, shop_domain=SHOP_SHOES,
                                 generation=r["generation"])
    with pytest.raises(crypto.CredentialCryptoError):
        CIPHER.decrypt(r["access_token_enc"], other)
    newer = crypto.token_context(crypto.PURPOSE_ACCESS_TOKEN, tenant_id=a.tenant_id, shop_domain=SHOP_SHOES,
                                 generation=r["generation"] + 1)
    with pytest.raises(crypto.CredentialCryptoError):
        CIPHER.decrypt(r["access_token_enc"], newer)


def test_credential_check_constraint_is_enforced(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    with pytest.raises(IntegrityError):
        _sql(pg, "UPDATE shopify_connections SET status = 'disconnected'")
    with pytest.raises(IntegrityError):
        _sql(pg, "UPDATE shopify_connections SET access_token_enc = NULL")
    with pytest.raises(IntegrityError):
        _sql(pg, "UPDATE shopify_oauth_states SET status = 'completed', code_enc = 'sgcm1:x'")


# ── HTTP wiring (flag on) against PostgreSQL ──────────────────────────────────

def _http_app(pg, monkeypatch, fake):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    import core.database
    from core.database import get_db
    from routers import shopify_connection as router_module

    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(router_module, "_api", lambda _cfg: fake)
    monkeypatch.setattr(recovery, "_api_for", lambda _cfg: fake)
    monkeypatch.setattr(core.database, "SessionLocal", pg.Session)

    app = FastAPI()

    @app.middleware("http")
    async def inject_payload(request: Request, call_next):
        raw = request.headers.get("x-test-jwt-payload")
        if raw:
            request.state.jwt_payload = json.loads(raw)
        return await call_next(request)

    def db_dep():
        db = pg.Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = db_dep
    app.include_router(router_module.router)
    app.include_router(router_module.webhook_router)
    return TestClient(app)


def _signed_callback_query(shop, state, code):
    params = {"code": code, "host": "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvZ2VuZXJpYw", "shop": shop, "state": state,
              "timestamp": str(int(time.time()))}
    params["hmac"] = compute_query_hmac(params, SECRET)
    return urlencode(params)


def test_http_flow_end_to_end(pg, monkeypatch):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    client = _http_app(pg, monkeypatch, fake)
    headers = {"x-test-jwt-payload": json.dumps(a.payload())}

    resp = client.post("/merchant/integrations/shopify/start", json={"shop": SHOP_SHOES.upper()}, headers=headers)
    assert resp.status_code == 200, resp.text
    authorize = urlsplit(resp.json()["authorize_url"])
    assert authorize.netloc == SHOP_SHOES and authorize.path == "/admin/oauth/authorize"
    query = parse_qs(authorize.query)
    assert query["scope"] == ["read_products"] and query["redirect_uri"] == [CFG.redirect_uri]
    state = query["state"][0]

    support = {"x-test-jwt-payload": json.dumps(a.payload(impersonation=True, role="support_impersonation"))}
    assert client.post("/merchant/integrations/shopify/start", json={"shop": SHOP_SHOES},
                       headers=support).status_code == 403

    resp = client.get("/merchant/integrations/shopify/callback?" +
                      _signed_callback_query(SHOP_SHOES, state, fake.issue_code(SHOP_SHOES)),
                      follow_redirects=False)
    assert resp.status_code == 302
    location = urlsplit(resp.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CFG.dashboard_complete_url
    assert location.fragment.startswith("shopify_handle=") and state not in resp.headers["location"]
    handle = location.fragment.split("=", 1)[1]

    resp = client.post("/merchant/integrations/shopify/complete", json={"handle": handle}, headers=headers)
    assert resp.status_code == 200, resp.text
    status = client.get("/merchant/integrations/shopify/status", headers=headers)
    assert status.json()["connections"][0]["status"] == "active"
    access, refresh = fake.current[SHOP_SHOES]
    assert access not in status.text and refresh not in status.text

    body = json.dumps({"id": 7100000001, "myshopify_domain": SHOP_SHOES, "name": "Generic"}).encode()
    signature = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    hook_headers = {"X-Shopify-Hmac-Sha256": signature, "X-Shopify-Topic": "app/uninstalled",
                    "X-Shopify-Shop-Domain": SHOP_SHOES, "X-Shopify-Webhook-Id": "http-w-1",
                    "Content-Type": "application/json"}
    tampered = {**hook_headers, "X-Shopify-Hmac-Sha256": base64.b64encode(b"x" * 32).decode()}
    assert client.post("/webhooks/shopify/app-uninstalled", content=body, headers=tampered).status_code == 401
    fake.uninstall(SHOP_SHOES)
    resp = client.post("/webhooks/shopify/app-uninstalled", content=body, headers=hook_headers)
    assert resp.status_code == 200
    # The background attempt ran after the acknowledgement (leased path).
    status = client.get("/merchant/integrations/shopify/status", headers=headers)
    assert status.json()["connections"][0]["status"] == "uninstalled"

    def oversized():
        for _ in range(80):
            yield b"x" * 1024

    big = client.post("/webhooks/shopify/app-uninstalled", content=oversized(), headers={
        k: v for k, v in hook_headers.items() if k != "Content-Type"})
    assert big.status_code == 413


# ── Reconciliation request fencing, stale runners, late grants ───────────────

def test_duplicate_delivery_during_an_in_flight_probe_keeps_the_new_request(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest="e1" * 32)
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    # Redelivery requests a fresh probe; a second redelivery arrives while that
    # probe is in flight. The older probe's success must not clear it.
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-1", digest="e1" * 32)[0] == "duplicate"
    fired = []

    def redeliver_during_probe(shop, _token):
        if not fired:
            fired.append(1)
            assert uninstall_event(pg, shop, webhook_id="w-1", digest="e1" * 32)[0] == "duplicate"

    fake.identity_hook = redeliver_during_probe
    assert reconcile(pg, SHOP_SHOES, fake) == "deferred"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["revalidation_requested_at"] is not None
    fake.identity_hook = None
    fake.uninstall(SHOP_SHOES)              # the newer request is the one that matters
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"


def test_identical_body_redelivered_across_reinstall_during_probe(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    body = "e2" * 32
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-old", digest=body)
    fake.uninstall(SHOP_SHOES)
    assert reconcile(pg, SHOP_SHOES, fake) == "uninstalled"
    connect(pg, a, SHOP_SHOES, fake)
    assert uninstall_event(pg, SHOP_SHOES, webhook_id="w-old", digest=body)[0] == "duplicate"
    fake.identity_hook = lambda shop, _t: uninstall_event(pg, shop, webhook_id="w-old", digest=body)
    assert reconcile(pg, SHOP_SHOES, fake) == "deferred"
    fake.identity_hook = None
    assert row(pg, SHOP_SHOES)["revalidation_requested_at"] is not None
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"


def test_stale_runner_with_an_expired_lease_writes_nothing(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-s", digest="e3" * 32)
    t0 = lifecycle.utcnow()
    with pg.Session() as db:
        [(conn_id, stale_lease, version)] = recovery.claim_due(db, now=t0)
    later = t0 + timedelta(seconds=recovery.RECONCILE_LEASE_SECONDS + 1)
    fake.uninstall(SHOP_SHOES)
    out = run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake, now=later))
    assert out.get("uninstalled") == 1
    calls = fake.count("identity") + fake.count("refresh")
    with pg.Session() as db:
        assert run(recovery.run_claimed(db, connection_id=conn_id, lease=stale_lease, claimed_version=version,
                                        api=fake, cipher=CIPHER, now=later)) == "lease_lost"
    assert fake.count("identity") + fake.count("refresh") == calls
    assert row(pg, SHOP_SHOES)["status"] == "uninstalled"


def test_runner_lease_taken_over_mid_probe_fences_the_write(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    uninstall_event(pg, SHOP_SHOES, webhook_id="w-f", digest="e4" * 32)
    with pg.Session() as db:
        [(conn_id, lease, version)] = recovery.claim_due(db, now=lifecycle.utcnow())
    fake.identity_hook = lambda shop, _t: _sql(
        pg, "UPDATE shopify_connections SET reconcile_lease_id = 'other-worker', "
            "reconcile_lease_expires_at = now() + interval '2 minutes' WHERE id = :i", i=conn_id)
    fake.uninstall(SHOP_SHOES)
    with pg.Session() as db:
        result = run(recovery.run_claimed(db, connection_id=conn_id, lease=lease, claimed_version=version,
                                          api=fake, cipher=CIPHER))
    assert result == "superseded"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "quarantined" and r["reconcile_lease_id"] == "other-worker"


def test_batch_runner_and_competing_worker_process_each_row_once(pg):
    fake = FakeShopify()
    shops = [SHOP_SHOES, SHOP_SHIRTS, SHOP_PERFUME]
    for i, shop in enumerate(shops):
        connect(pg, Merchant(pg, f"m{i}"), shop, fake)
        uninstall_event(pg, shop, webhook_id=f"w-b{i}", digest=f"f{i}" * 32)
    hook, entered, release = barrier_hook()
    gate = {"armed": True}

    def slow_first(shop, _token):
        if gate.pop("armed", False):
            hook(shop)

    baseline = fake.count("identity")
    fake.identity_hook = slow_first
    th, box = in_thread(lambda: run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake,
                                                               limit=3)))
    assert entered.wait(20)
    other = run(recovery.run_recovery_tick(session_factory=pg.Session, env=ENV, api=fake, limit=3))
    assert other["claimed"] == 2 and other.get("retained") == 2
    release.set()
    th.join(30)
    assert box["value"]["claimed"] == 1 and box["value"].get("retained") == 1
    assert fake.count("identity") - baseline == 3
    assert all(row(pg, s)["status"] == "active" for s in shops)


def test_late_grant_after_lease_takeover_quarantines_the_incumbent(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    # The lease is taken over while this (delayed) exchange is still in flight;
    # Shopify then issues its grant anyway, retiring the stored refresh token.
    fake.exchange_hook = lambda shop: _sql(pg, "UPDATE shopify_shop_leases SET lease_id = 'new-worker' "
                                               "WHERE shop_domain = :s", s=shop)
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_SUPERSEDED
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "quarantined" and r["quarantine_reason"] == lifecycle.POSSIBLE_RETIRED
    assert r["generation"] == 1 and r["tenant_id"] == a.tenant_id
    # While the worker that took the lease over still holds it, the forced
    # refresh waits (deferred) instead of racing it.
    assert reconcile(pg, SHOP_SHOES, fake) == "deferred"
    _sql(pg, "DELETE FROM shopify_shop_leases WHERE lease_id = 'new-worker'")   # that worker finished
    # An access-token probe would still pass (retired access tokens live until
    # expiry); only a forced refresh may clear it — and it is refused.
    assert reconcile(pg, SHOP_SHOES, fake) == "reauth_required"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "reauth_required" and r["tenant_id"] == a.tenant_id and r["access_token_enc"] is None
    assert refused(start, pg, Merchant(pg, "b"), SHOP_SHOES) == lifecycle.C_SHOP_UNAVAILABLE


def test_exchange_deadline_stays_below_the_lease_and_flags_the_incumbent(pg, monkeypatch):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    monkeypatch.setattr(lifecycle, "EXCHANGE_LEASE_SECONDS", 12)
    monkeypatch.setattr(lifecycle, "LEASE_SAFETY_SECONDS", 10)
    fake.exchange_delay = 5.0
    began = time.monotonic()
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_EXCHANGE_FAILED
    assert time.monotonic() - began < 4.0
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_shop_leases")[0]["n"] == 0
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "quarantined" and r["quarantine_reason"] == lifecycle.POSSIBLE_RETIRED
    # Here the abandoned call never reached the grant: the forced refresh works.
    assert reconcile(pg, SHOP_SHOES, fake) == "retained"
    r = row(pg, SHOP_SHOES)
    assert r["status"] == "active" and r["credential_version"] == 2
    assert stored_tokens(pg, SHOP_SHOES) == fake.current[SHOP_SHOES]


def test_no_call_is_made_without_enough_lease_left(pg, monkeypatch):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOP_SHOES, fake)
    handle = ready(pg, a, SHOP_SHOES, fake)
    monkeypatch.setattr(lifecycle, "LEASE_SAFETY_SECONDS", 10_000)
    assert refused(complete, pg, a, handle, fake) == lifecycle.C_SUPERSEDED
    assert fake.count("exchange") == 1          # only the original connect
    assert row(pg, SHOP_SHOES)["status"] == "active"   # nothing was exchanged: nothing flagged
