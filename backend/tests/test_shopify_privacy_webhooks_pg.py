"""Dormant Shopify privacy compliance webhooks against REAL PostgreSQL.

Throw-away databases are cloned from a template holding the whole
``models.Base`` schema (so the legacy / generic stores the audit counts are
the real ones and the Salla / shared hub rows are real ``products``), the
Shopify foundation created by revision 0121 itself, the catalog mirror by
0122 itself and the privacy tables by 0123 itself. Shopify is an in-process
fake of the documented token behaviour (no network, no merchant); every
credential, email and phone is generated at runtime. Neutral generic
merchants and categories only. Lifecycle transitions (connect, uninstall,
reconcile, reinstall) go through the foundation's real functions.

Nothing here erases real data: the cleanup helper and the executor run only
on these disposable fixtures, and the executor only with a synthetic,
test-local policy object (the shipped ``approved_policy()`` is ``None``).

Without ``NAHLA_RELIABILITY_PG_ADMIN_DSN`` the module is skipped (reported as
skipped, never as passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no
variable it fails. Inventoried in the required PostgreSQL proofs.
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
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_TESTS = Path(__file__).resolve().parent
_REPO = _TESTS.parents[1]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")

from services.shopify_connection import crypto, lifecycle  # noqa: E402
from services.shopify_connection.actor import revalidate_actor  # noqa: E402
from services.shopify_connection.config import evaluate_availability  # noqa: E402
from services.shopify_connection.oauth import ShopIdentity, ShopifyApiError, TokenGrant, VerifiedCallback  # noqa: E402
from services.shopify_connection.webhooks import SignedShopIdentity  # noqa: E402
from services.shopify_privacy import audit, cleanup, store, worker  # noqa: E402
from services.shopify_privacy.config import AUDIT_VERSION, TOPIC_PATHS  # noqa: E402
from backend.tests import _alembic_topology_probe as topo  # noqa: E402
from backend.tests._shopify_webhook_fake import FakeWebhookSubscriptions  # noqa: E402
from services.shopify_privacy.models import (  # noqa: E402
    MAX_DELIVERY_ROWS_PER_REQUEST,
    MAX_EVENTS_PER_REQUEST,
    PRIVACY_TABLE_NAMES,
    ShopifyPrivacyBase,
    create_privacy_tables,
)
from services.shopify_privacy.payloads import parse_privacy_body  # noqa: E402

_VERSIONS = _REPO / "database" / "migrations" / "versions"
_M0121 = _VERSIONS / "0121_shopify_connection_foundation.py"
_M0122 = _VERSIONS / "0122_shopify_catalog_mirror.py"
_M0123 = _VERSIONS / "0123_shopify_privacy_requests.py"

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
WORKER_ENV = {"NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED": "1"}
DESTRUCTIVE_ENV = {"NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED": "1"}
# Test-local fixture policy object — never the shipped approved_policy() (None).
FIXTURE_POLICY = cleanup.ApprovedErasurePolicy(
    version="synthetic-fixture-policy-v0", approved_by="test-fixture", scope=frozenset({"catalog_mirror"}),
    retained={"shopify_connections": "fixture: ownership tombstone retained"})

SHOES = "generic-shoes-store.myshopify.com"
SHIRTS = "cotton-shirts-demo.myshopify.com"
PERFUME = "rose-perfume-demo.myshopify.com"
GIDS = {SHOES: "gid://shopify/Shop/7100000001", SHIRTS: "gid://shopify/Shop/7100000002",
        PERFUME: "gid://shopify/Shop/7100000003"}


def shop_id(shop: str) -> int:
    return int(GIDS[shop].rsplit("/", 1)[1])


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


def _module(path: Path):
    spec = importlib.util.spec_from_file_location(f"m_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(conn, path: Path, direction: str = "upgrade") -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(conn)):
        getattr(_module(path), direction)()


def _base_tables(conn) -> None:
    from models import Base

    Base.metadata.create_all(conn)


def _fresh_db(prefix: str, *, template: str | None = None) -> str:
    name = f"{prefix}_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name}" + (f" TEMPLATE {template}" if template else ""))
    return name


def _drop(name: str) -> None:
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _build(name: str, *, privacy: bool = True, catalog: bool = True) -> None:
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _migrate(conn, _M0121)
            if catalog:
                _migrate(conn, _M0122)
            if privacy:
                _migrate(conn, _M0123)
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def template_db():
    name = _fresh_db("shpv_tmpl")
    _build(name)
    try:
        yield name
    finally:
        _drop(name)


@pytest.fixture()
def pg(template_db, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    name = _fresh_db("shpv", template=template_db)
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


def _one(pg, statement: str, **params):
    return _sql(pg, statement, **params)[0]


class Merchant:
    def __init__(self, pg, label: str):
        self.tenant_id = _one(pg, "INSERT INTO tenants (name, is_active, is_platform_tenant) "
                                  "VALUES (:n, true, false) RETURNING id", n=f"متجر تجريبي عام {label}")["id"]
        self.user_id = _one(pg, "INSERT INTO users (username, email, role, is_active, email_verified, tenant_id) "
                                "VALUES (:u, :e, 'merchant', true, true, :t) RETURNING id",
                            u=f"merchant-{label}-{secrets.token_hex(3)}",
                            e=f"{label}-{secrets.token_hex(3)}@example.test", t=self.tenant_id)["id"]
        self.jti = secrets.token_hex(16)

    def actor(self, pg):
        with pg.Session() as db:
            return revalidate_actor(db, {"tenant_id": self.tenant_id, "user_id": self.user_id, "role": "merchant",
                                         "jti": self.jti})


# ── Fake Shopify (documented token behaviour; no network) ─────────────────────

class FakeShopify(FakeWebhookSubscriptions):
    def __init__(self):
        self.lock = threading.Lock()
        self.codes = {}
        self.valid_access = {}
        self.valid_refresh = {}
        self.calls = []
        self._webhook_state()

    def issue_code(self, shop):
        code = secrets.token_hex(16)
        with self.lock:
            self.codes[code] = shop
        return code

    def _issue(self, shop):
        for token in [t for t, s in self.valid_refresh.items() if s == shop]:
            self.valid_refresh.pop(token)
        access, refresh = f"shpat_{secrets.token_hex(16)}", f"shprt_{secrets.token_hex(16)}"
        self.valid_access[access] = shop
        self.valid_refresh[refresh] = shop
        return TokenGrant(access, refresh, 3600, 7776000, frozenset({"read_products"}))

    def uninstall(self, shop):
        with self.lock:
            for table in (self.valid_access, self.valid_refresh):
                for token in [t for t, s in table.items() if s == shop]:
                    table.pop(token)
        self.uninstall_webhooks(shop)

    async def exchange_code(self, *, shop_domain, code):
        with self.lock:
            self.calls.append(("exchange", shop_domain))
            if self.codes.pop(code, None) != shop_domain:
                raise ShopifyApiError("permanent", "grant_rejected")
            return self._issue(shop_domain)

    async def refresh(self, *, shop_domain, refresh_token):
        with self.lock:
            self.calls.append(("refresh", shop_domain))
            if self.valid_refresh.pop(refresh_token, None) != shop_domain:
                raise ShopifyApiError("permanent", "grant_rejected")
            return self._issue(shop_domain)

    async def shop_identity(self, *, shop_domain, access_token):
        with self.lock:
            self.calls.append(("identity", shop_domain))
            if self.valid_access.get(access_token) != shop_domain:
                raise ShopifyApiError("rejected", "credentials_rejected")
        return ShopIdentity(shop_gid=GIDS[shop_domain], shop_domain=shop_domain)


def run(coro):
    return asyncio.run(coro)


def begin_auth(pg, merchant, shop):
    with pg.Session() as db:
        url, _ = lifecycle.begin_authorization(db, actor=merchant.actor(pg), shop_domain=shop, config=CFG)
    return parse_qs(urlsplit(url).query)["state"][0]


def connect(pg, merchant, shop, fake):
    """The foundation's real flow: start → callback → authenticated completion."""
    state = begin_auth(pg, merchant, shop)
    with pg.Session() as db:
        handle = lifecycle.accept_callback(db, callback=VerifiedCallback(shop, state, fake.issue_code(shop)),
                                           config=CFG, cipher=CIPHER)
    with pg.Session() as db:
        return run(lifecycle.complete_authorization(db, actor=merchant.actor(pg), handle=handle, config=CFG,
                                                    cipher=CIPHER, api=fake))


def conn_row(pg, shop):
    rows = _sql(pg, "SELECT * FROM shopify_connections WHERE shop_domain = :s", s=shop)
    return dict(rows[0]) if rows else None


def uninstall(pg, shop, fake, *, webhook_id=None):
    """Genuine uninstall: Shopify revokes, the uninstall webhook quarantines
    (same generation), the probe confirms (tombstone, generation + 1)."""
    fake.uninstall(shop)
    identity = SignedShopIdentity(shop_id=shop_id(shop), shop_domain=shop)
    with pg.Session() as db:
        lifecycle.record_uninstall(db, identity=identity, webhook_id=webhook_id or secrets.token_hex(8),
                                   body_sha256=secrets.token_hex(32), triggered_at_header=None)
    return foundation_probe(pg, shop, fake)


def foundation_probe(pg, shop, fake):
    with pg.Session() as db:
        return run(lifecycle.reconcile(db, connection_id=conn_row(pg, shop)["id"], api=fake, cipher=CIPHER))


# ── Privacy helpers ───────────────────────────────────────────────────────────

def _email():
    return f"customer.{secrets.token_hex(4)}@example.test"


def _phone():
    return "+9665" + str(secrets.randbelow(10 ** 8)).zfill(8)


def body(topic: str, shop: str, **over) -> bytes:
    base = {"shop_id": shop_id(shop), "shop_domain": shop}
    if topic == "customers/redact":
        base.update(customer={"id": 191167, "email": _email(), "phone": _phone()}, orders_to_redact=[299938, 280263])
    elif topic == "customers/data_request":
        base.update(customer={"id": 191167, "email": _email(), "phone": _phone()}, orders_requested=[299938],
                    data_request={"id": 9999})
    base.update(over)
    return json.dumps(base, separators=(",", ":")).encode()


def deliver(pg, topic, shop, data=None, *, webhook_id=None, triggered_at=None, now=None):
    data = data if data is not None else body(topic, shop)
    payload = parse_privacy_body(data, topic)
    with pg.Session() as db:
        return store.record_receipt(db, payload=payload, body_sha256=hashlib.sha256(data).hexdigest(),
                                    webhook_id=webhook_id, triggered_at=triggered_at, now=now)


def req(pg, request_id):
    return dict(_one(pg, "SELECT * FROM shopify_privacy_requests WHERE id = :i", i=request_id))


def tick(pg, now=None, limit=10):
    return worker.run_tick(session_factory=pg.Session, env=WORKER_ENV, now=now, limit=limit)


def count(pg, table, where="true", **params):
    return int(_one(pg, f"SELECT count(*) AS n FROM {table} WHERE {where}", **params)["n"])


def dump_text(pg) -> str:
    out = []
    for table in PRIVACY_TABLE_NAMES + ("shopify_connection_audit",):
        out.append(json.dumps([dict(r) for r in _sql(pg, f"SELECT * FROM {table}")], default=str, ensure_ascii=False))
    return "\n".join(out)


def seed_mirror(pg, connection_id, tenant_id, shop, *, raw=1):
    """A committed snapshot, a failed run with staged rows: one row in every
    mirror table, keyed by raw id ``raw`` (identical raw ids across shops)."""
    g = conn_row(pg, shop)["generation"]
    p = dict(t=tenant_id, c=connection_id, d=shop, g=GIDS[shop], gen=g, now=datetime.now(timezone.utc))
    run_id = _one(pg, (
        "INSERT INTO shopify_catalog_import_runs (tenant_id, connection_id, run_seq, shop_domain, shop_gid, generation,"
        " api_version, status, inventory_mode, shop_currency, traversal_complete, pages_fetched, products_count,"
        " variants_count, inventory_items_count, locations_count, inventory_levels_count, started_at, finished_at,"
        " committed_at) VALUES (:t, :c, 1, :d, :g, :gen, '2026-10', 'committed', 'per_location', 'SAR', true, 1,"
        " 1, 1, 1, 1, 1, :now, :now, :now) RETURNING id"), **p)["id"]
    failed = _one(pg, (
        "INSERT INTO shopify_catalog_import_runs (tenant_id, connection_id, run_seq, shop_domain, shop_gid, generation,"
        " api_version, status, failure_reason, started_at, finished_at) VALUES (:t, :c, 2, :d, :g, :gen, '2026-10',"
        " 'failed', 'source_changed_during_import', :now, :now) RETURNING id"), **p)["id"]
    _sql(pg, "INSERT INTO shopify_catalog_staged_records (run_id, kind, gid, payload) VALUES (:r, 'product', :gid,"
             " '{}'::jsonb)", r=failed, gid=f"gid://shopify/Product/{raw}")
    p.update(run=run_id, raw=raw)
    product = _one(pg, (
        "INSERT INTO shopify_catalog_products (tenant_id, connection_id, product_gid, title, handle, status, options,"
        " images, is_gift_card, requires_selling_plan, has_only_default_variant, variants_count, source_created_at,"
        " source_updated_at, lifecycle, first_seen_run_id, last_seen_run_id) VALUES (:t, :c,"
        " 'gid://shopify/Product/' || :raw, 'حذاء رياضي أبيض', 'white-sneaker', 'ACTIVE', '[]'::jsonb,"
        " '[]'::jsonb, false, false, true, 1, :now, :now, 'present', :run, :run) RETURNING id"), **p)["id"]
    item = _one(pg, (
        "INSERT INTO shopify_catalog_inventory_items (tenant_id, connection_id, inventory_item_gid, tracking,"
        " source_updated_at, lifecycle, first_seen_run_id, last_seen_run_id) VALUES (:t, :c,"
        " 'gid://shopify/InventoryItem/' || :raw, 'tracked', :now, 'present', :run, :run) RETURNING id"), **p)["id"]
    _sql(pg, (
        "INSERT INTO shopify_catalog_variants (tenant_id, connection_id, product_id, inventory_item_id, variant_gid,"
        " title, position, price, currency_code, selected_options, inventory_policy, available_for_sale,"
        " requires_components, source_created_at, source_updated_at, lifecycle, first_seen_run_id, last_seen_run_id)"
        " VALUES (:t, :c, :p, :i, 'gid://shopify/ProductVariant/' || :raw, '40', 1, 199.00, 'SAR', '[]'::jsonb,"
        " 'DENY', true, false, :now, :now, 'present', :run, :run)"), p=product, i=item, **p)
    location = _one(pg, (
        "INSERT INTO shopify_catalog_locations (tenant_id, connection_id, location_gid, name, is_active,"
        " fulfills_online_orders, lifecycle, first_seen_run_id, last_seen_run_id) VALUES (:t, :c,"
        " 'gid://shopify/Location/' || :raw, 'المستودع الرئيسي', true, true, 'present', :run, :run) RETURNING id"),
        **p)["id"]
    _sql(pg, (
        "INSERT INTO shopify_catalog_inventory_levels (tenant_id, connection_id, inventory_item_id, location_id,"
        " inventory_level_gid, available, source_updated_at, lifecycle, first_seen_run_id, last_seen_run_id) VALUES"
        " (:t, :c, :i, :l, 'gid://shopify/InventoryLevel/' || :raw || '?inventory_item_id=' || :raw, 5, :now,"
        " 'present', :run, :run)"), i=item, l=location, **p)


def mirror_counts(pg, connection_id, tenant_id):
    with pg.Session() as db:
        return audit.mirror_counts(db, connection_id=connection_id, tenant_id=tenant_id)


def salla_product(pg, tenant_id, raw="1"):
    return _one(pg, "INSERT INTO products (tenant_id, title, external_id, source, in_stock) VALUES"
                    " (:t, 'قميص قطني أزرق', :x, 'salla', true) RETURNING id", t=tenant_id, x=raw)["id"]


def shop_with_mirror(pg, label, shop, fake):
    merchant = Merchant(pg, label)
    connect(pg, merchant, shop, fake)
    conn = conn_row(pg, shop)
    seed_mirror(pg, conn["id"], merchant.tenant_id, shop)
    return merchant, conn


# ── Migration 0123 ────────────────────────────────────────────────────────────

def _schema(eng) -> dict:
    insp = inspect(eng)
    out = {}
    for t in PRIVACY_TABLE_NAMES:
        out[t] = {
            "columns": sorted((c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default")))
                              for c in insp.get_columns(t)),
            "pk": insp.get_pk_constraint(t).get("constrained_columns"),
            "uniques": sorted((u["name"], tuple(u["column_names"])) for u in insp.get_unique_constraints(t)),
            "checks": sorted((c["name"], " ".join(c["sqltext"].split())) for c in insp.get_check_constraints(t)),
            "fks": sorted((tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]),
                           (f.get("options") or {}).get("ondelete")) for f in insp.get_foreign_keys(t)),
        }
    with eng.connect() as conn:
        out["indexes"] = sorted(tuple(r) for r in conn.execute(text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE tablename LIKE 'shopify_privacy%'")).all())
    return out


def test_migration_schema_equals_model_metadata():
    by_migration, by_model = _fresh_db("shpv_mig"), _fresh_db("shpv_mod")
    try:
        _build(by_migration)
        _build(by_model, privacy=False)
        e2 = create_engine(_url(by_model), poolclass=NullPool, future=True)
        with e2.begin() as conn:
            create_privacy_tables(conn)
        e1 = create_engine(_url(by_migration), poolclass=NullPool, future=True)
        a, b = _schema(e1), _schema(e2)
        e1.dispose()
        e2.dispose()
        assert a == b
        checks = dict(a["shopify_privacy_requests"]["checks"])
        for name in ("ck_shopify_privacy_requests_completion", "ck_shopify_privacy_requests_attribution",
                     "ck_shopify_privacy_requests_topic_fields", "ck_shopify_privacy_requests_blocked_policy"):
            assert name in checks
        assert any(i[1] == "uq_shopify_privacy_requests_open" and "WHERE" in i[2] for i in a["indexes"])
    finally:
        _drop(by_migration)
        _drop(by_model)


def _names(eng):
    insp = inspect(eng)
    return sorted(insp.get_table_names()), {t: sorted(c["name"] for c in insp.get_columns(t))
                                            for t in insp.get_table_names() if t.startswith("shopify_")}


@pytest.mark.parametrize("shape", ["full_model_tables", "partial_one_table", "malformed_table"])
def test_migration_refuses_any_preexisting_privacy_table_and_changes_nothing(shape):
    name = _fresh_db("shpv_pre")
    _build(name, privacy=False)
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            if shape == "full_model_tables":
                create_privacy_tables(conn)
            elif shape == "partial_one_table":
                conn.execute(text("CREATE TABLE shopify_privacy_request_events (id bigint)"))
            else:
                conn.execute(text("CREATE TABLE shopify_privacy_requests (id integer, state text CHECK (true))"))
        before = _names(eng)
        with pytest.raises(RuntimeError, match="already exist"):
            with eng.begin() as conn:
                _migrate(conn, _M0123)
        assert _names(eng) == before
    finally:
        eng.dispose()
        _drop(name)


@pytest.mark.parametrize("missing", ["catalog_tables", "connection_key"])
def test_migration_refuses_without_revision_0122(missing):
    name = _fresh_db("shpv_no22")
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _migrate(conn, _M0121)
            if missing == "connection_key":
                _migrate(conn, _M0122)
                conn.execute(text("ALTER TABLE shopify_catalog_import_runs DROP CONSTRAINT fk_shopify_catalog_import_runs_connection"))
                for t in ("shopify_catalog_products", "shopify_catalog_inventory_items", "shopify_catalog_locations"):
                    conn.execute(text(f"ALTER TABLE {t} DROP CONSTRAINT fk_{t}_connection"))
                conn.execute(text("ALTER TABLE shopify_connections DROP CONSTRAINT uq_shopify_connections_id_tenant"))
        before = _names(eng)
        with pytest.raises(RuntimeError, match="0122|0121"):
            with eng.begin() as conn:
                _migrate(conn, _M0123)
        assert _names(eng) == before and not (set(PRIVACY_TABLE_NAMES) & set(before[0]))
    finally:
        eng.dispose()
        _drop(name)


def test_downgrade_drops_only_the_privacy_tables():
    name = _fresh_db("shpv_down")
    _build(name)
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        before = set(inspect(eng).get_table_names())
        assert set(PRIVACY_TABLE_NAMES) <= before
        with eng.begin() as conn:
            _migrate(conn, _M0123, "downgrade")
        after = set(inspect(eng).get_table_names())
        assert after == before - set(PRIVACY_TABLE_NAMES)
        assert {"shopify_connections", "shopify_catalog_products", "products", "tenants"} <= after
    finally:
        eng.dispose()
        _drop(name)


def test_alembic_chain_upgrades_0122_to_0123_and_back():
    name = _fresh_db("shpv_chain")
    url = _url(name)
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(_REPO / "database"), env=env,
                              capture_output=True, text=True, timeout=600)

    try:
        # The deployed topology {0111, 0113, 0120} (three applied branch heads) is built by genuine
        # upgrades only; nothing is stamped, merged or fabricated. Every later step asserts the exact
        # alembic_version rows, the retained 0111/0113 rows, the non-Shopify schema by definition and
        # synthetic sentinel rows in generic, catalog and retained-branch tables.
        for target in ("0093", "0111", "0113", "0120"):
            proc = alembic("upgrade", target)
            assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_rows(url, topo.S0, "S0")
        baseline = topo.schema_snapshot(url)
        planted = topo.plant_sentinels(url)
        counts = topo.row_counts(url, topo.SENTINEL_TABLES)
        topo.assert_step(url, topo.S0, baseline, planted, counts, "S0")

        for target, state in (("0121", topo.S1), ("0122", topo.S2), ("0123", topo.S3)):
            proc = alembic("upgrade", target)
            assert proc.returncode == 0, proc.stderr[-3000:]
            topo.assert_step(url, state, baseline, planted, counts, f"after upgrade {target}")
        eng = create_engine(url, poolclass=NullPool, future=True)
        assert set(PRIVACY_TABLE_NAMES) <= set(inspect(eng).get_table_names())

        proc = alembic("downgrade", "0123@-1")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S2, baseline, planted, counts, "S2 after downgrade 0123@-1")
        names = set(inspect(eng).get_table_names())
        assert not (set(PRIVACY_TABLE_NAMES) & names) and "shopify_catalog_products" in names

        proc = alembic("downgrade", "0122@-1")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S1, baseline, planted, counts, "S1 after downgrade 0122@-1")
        names = set(inspect(eng).get_table_names())
        assert "shopify_catalog_products" not in names and "shopify_connections" in names

        proc = alembic("downgrade", "0121@-1")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S0, baseline, planted, counts, "S0 after downgrade 0121@-1")
        assert not {t for t in inspect(eng).get_table_names() if t.startswith("shopify_")}
        eng.dispose()
    finally:
        _drop(name)


def test_composite_key_isolates_tenants_and_checks_refuse_fabricated_states(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    connect(pg, a, SHOES, fake)
    conn_a = conn_row(pg, SHOES)
    base = ("INSERT INTO shopify_privacy_requests (topic, shop_domain, shop_gid, body_sha256, identity_state,"
            " tenant_id, connection_id, state, next_attempt_at, received_at, last_delivery_at, deadline_at{extra})"
            " VALUES ('shop/redact', :d, :g, :h, :ident, :t, :c, :state, :due, now(), now(), now() + interval '30 days'"
            "{extra_v})")

    def insert(**kw):
        extra = kw.pop("extra", "")
        extra_v = kw.pop("extra_v", "")
        params = dict(d=SHOES, g=GIDS[SHOES], h=secrets.token_hex(32), ident="matched", t=a.tenant_id,
                      c=conn_a["id"], state="received", due=datetime.now(timezone.utc))
        params.update(kw)
        _sql(pg, base.format(extra=extra, extra_v=extra_v), **params)

    insert()  # well-formed
    with pytest.raises(IntegrityError):
        insert(t=b.tenant_id)  # another tenant's attribution through tenant A's connection
    with pytest.raises(IntegrityError):
        insert(state="completed", due=None)  # completion without evidence or policy
    with pytest.raises(IntegrityError):
        insert(extra=", completion_evidence", extra_v=", '{\"deleted\": {}}'::jsonb")  # evidence on an open state
    with pytest.raises(IntegrityError):
        insert(state="blocked_policy", due=None, extra=", reason", extra_v=", 'x'")  # no proposal / generation
    with pytest.raises(IntegrityError):
        insert(ident="unknown")  # unknown identity with a tenant
    with pytest.raises(IntegrityError):
        _sql(pg, "INSERT INTO shopify_privacy_requests (topic, shop_domain, shop_gid, body_sha256, identity_state,"
                 " state, next_attempt_at, received_at, last_delivery_at, deadline_at, customer_ref_kind)"
                 " VALUES ('customers/redact', :d, :g, :h, 'unknown', 'received', now(), now(), now(),"
                 " now() + interval '30 days', 'email')", d=SHOES, g=GIDS[SHOES], h=secrets.token_hex(32))
    with pytest.raises(IntegrityError):
        insert(extra=", attempts", extra_v=", -1")  # counters are bounded


# ── Receipt ───────────────────────────────────────────────────────────────────

def _http_client(pg, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import shopify_privacy_webhooks as router_module

    monkeypatch.setenv("NAHLA_SHOPIFY_PRIVACY_INTAKE_ENABLED", "1")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    router_module.set_receipt_session_factory(pg.Session)
    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app, raise_server_exceptions=False), router_module


def _signed(data: bytes, topic: str, shop: str, **over):
    h = {"X-Shopify-Hmac-Sha256": base64.b64encode(hmac.new(SECRET.encode(), data, hashlib.sha256).digest()).decode(),
         "X-Shopify-Topic": topic, "X-Shopify-Shop-Domain": shop, "Content-Type": "application/json"}
    h.update(over)
    return h


def test_http_receipts_store_no_raw_customer_value_header_or_body(pg, monkeypatch):
    fake = FakeShopify()
    merchant = Merchant(pg, "http")
    connect(pg, merchant, SHOES, fake)
    client, router_module = _http_client(pg, monkeypatch)
    try:
        email, phone, token = _email(), _phone(), "shpat_" + secrets.token_hex(16)
        stored_order = 987654321
        data = body("customers/redact", SHOES, customer={"id": 55443322, "email": email, "phone": phone},
                    orders_to_redact=[stored_order])
        headers = _signed(data, "customers/redact", SHOES, **{"X-Shopify-Webhook-Id": email,
                                                             "X-Shopify-Triggered-At": token})
        assert client.post(TOPIC_PATHS["customers/redact"], content=data, headers=headers).status_code == 200
        email_only = body("customers/data_request", SHOES, customer={"email": _email()}, data_request={"id": 2 ** 63 - 1})
        good = _signed(email_only, "customers/data_request", SHOES,
                       **{"X-Shopify-Webhook-Id": "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043",
                          "X-Shopify-Triggered-At": "2026-10-09T18:00:27Z"})
        assert client.post("/webhooks/shopify/privacy", content=email_only, headers=good).status_code == 200
        shop = body("shop/redact", SHOES)
        assert client.post(TOPIC_PATHS["shop/redact"], content=shop, headers=_signed(shop, "shop/redact", SHOES)).status_code == 200
        rows = _sql(pg, "SELECT * FROM shopify_privacy_requests ORDER BY id")
        assert [r["topic"] for r in rows] == ["customers/redact", "customers/data_request", "shop/redact"]
        assert all(r["tenant_id"] == merchant.tenant_id and r["identity_state"] == "matched" for r in rows)
        assert rows[0]["customer_ref_kind"] == "id" and rows[0]["orders_count"] == 1
        assert rows[1]["customer_ref_kind"] == "email" and rows[1]["data_request_id"] == 2 ** 63 - 1
        assert rows[0]["body_sha256"] == hashlib.sha256(data).hexdigest()
        deliveries = _sql(pg, "SELECT webhook_id, header_triggered_at FROM shopify_privacy_deliveries ORDER BY id")
        assert deliveries[0]["webhook_id"] is None and deliveries[0]["header_triggered_at"] is None
        assert deliveries[1]["webhook_id"] == "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043"
        everything = dump_text(pg)
        for value in (email, phone, token, "55443322", str(stored_order), data.decode(), headers["X-Shopify-Hmac-Sha256"]):
            assert value not in everything
        # Overflow is a deterministic 400 with nothing stored.
        over = body("customers/data_request", SHOES, data_request={"id": 2 ** 63})
        resp = client.post(TOPIC_PATHS["customers/data_request"], content=over,
                           headers=_signed(over, "customers/data_request", SHOES))
        assert resp.status_code == 400 and count(pg, "shopify_privacy_requests") == 3
        # Cross-topic forgery of a stored signed body changes nothing.
        resp = client.post(TOPIC_PATHS["shop/redact"], content=data, headers=_signed(data, "shop/redact", SHOES))
        assert resp.status_code == 400 and count(pg, "shopify_privacy_requests") == 3
    finally:
        router_module.set_receipt_session_factory(None)


def test_two_tenants_with_identical_raw_ids_stay_isolated(pg):
    fake = FakeShopify()
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    connect(pg, a, SHOES, fake)
    connect(pg, b, SHIRTS, fake)
    same = dict(customer={"id": 191167}, orders_to_redact=[299938])
    _, ra = deliver(pg, "customers/redact", SHOES, body("customers/redact", SHOES, **same))
    _, rb = deliver(pg, "customers/redact", SHIRTS, body("customers/redact", SHIRTS, **same))
    assert ra != rb
    assert req(pg, ra)["tenant_id"] == a.tenant_id and req(pg, ra)["connection_id"] == conn_row(pg, SHOES)["id"]
    assert req(pg, rb)["tenant_id"] == b.tenant_id and req(pg, rb)["connection_id"] == conn_row(pg, SHIRTS)["id"]
    tick(pg)
    assert req(pg, ra)["state"] == req(pg, rb)["state"] == "assessed"
    assert count(pg, "shopify_privacy_request_events", "tenant_id = :t", t=a.tenant_id) >= 2
    assert count(pg, "shopify_privacy_request_events", "tenant_id = :t AND request_id = :r", t=a.tenant_id, r=rb) == 0


def test_unknown_and_conflicting_identity_never_mutate_or_rebind(pg):
    fake = FakeShopify()
    a = Merchant(pg, "a")
    connect(pg, a, SHOES, fake)
    before = conn_row(pg, SHOES)
    # Unknown shop: no tenant.
    _, unknown = deliver(pg, "shop/redact", PERFUME)
    # Conflict: the signed domain is owned, but with another shop id.
    forged = json.dumps({"shop_id": 7199999999, "shop_domain": SHOES}).encode()
    _, conflict = deliver(pg, "shop/redact", SHOES, forged)
    for rid, state in ((unknown, "unknown"), (conflict, "conflict")):
        r = req(pg, rid)
        assert r["identity_state"] == state and r["tenant_id"] is None and r["connection_id"] is None
    tick(pg)
    assert req(pg, unknown)["state"] == "manual_review" and req(pg, unknown)["reason"] == "unknown_shop_identity"
    assert req(pg, conflict)["state"] == "manual_review" and req(pg, conflict)["reason"] == "identity_conflict"
    after = conn_row(pg, SHOES)
    assert {k: after[k] for k in ("tenant_id", "status", "generation", "reconcile_request_version",
                                  "revalidation_requested_at")} == \
        {k: before[k] for k in ("tenant_id", "status", "generation", "reconcile_request_version",
                                "revalidation_requested_at")}
    assert req(pg, unknown)["proposed_action"]["shopify_connection"] == "none"
    assert req(pg, unknown)["deadline_at"] > datetime.now(timezone.utc)


def test_concurrent_identical_deliveries_resolve_to_one_request(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    data = body("customers/redact", SHOES)
    barrier = threading.Barrier(8)
    results, errors = [], []

    def go():
        try:
            barrier.wait(10)
            results.append(deliver(pg, "customers/redact", SHOES, data))
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors and len(results) == 8
    assert len({rid for _o, rid in results}) == 1
    assert sorted(o for o, _ in results).count("received") == 1
    r = req(pg, results[0][1])
    assert r["delivery_count"] == 8 and r["last_delivery_at"] >= r["received_at"]


def test_changed_unsigned_headers_attach_and_never_suppress(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    data = body("shop/redact", SHOES)
    ids = [f"0000000{i}-bdd9-4b37-8a5f-bf7d70bcd043" for i in range(3)]
    outs = [deliver(pg, "shop/redact", SHOES, data, webhook_id=w) for w in ids]
    outs.append(deliver(pg, "shop/redact", SHOES, data, webhook_id=ids[0]))  # same id again: no new log row
    assert len({rid for _o, rid in outs}) == 1 and req(pg, outs[0][1])["delivery_count"] == 4
    assert count(pg, "shopify_privacy_deliveries") == 3
    # The same unsigned id reused with another signed body is another request.
    other = body("customers/redact", SHOES)
    _, rid = deliver(pg, "customers/redact", SHOES, other, webhook_id=ids[0])
    assert rid != outs[0][1]


def test_receipt_lock_timeout_is_a_503_and_changes_nothing(pg, monkeypatch):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    data = body("customers/redact", SHOES)
    _, rid = deliver(pg, "customers/redact", SHOES, data)
    client, router_module = _http_client(pg, monkeypatch)
    holder = pg.engine.connect()
    tx = holder.begin()
    try:
        holder.execute(text("SELECT id FROM shopify_privacy_requests WHERE id = :i FOR UPDATE"), {"i": rid})
        started = time.monotonic()
        resp = client.post(TOPIC_PATHS["customers/redact"], content=data,
                           headers=_signed(data, "customers/redact", SHOES))
        assert resp.status_code == 503 and resp.json() == {"error": "retry"}
        assert time.monotonic() - started < 3.5
        tx.rollback()
        assert req(pg, rid)["delivery_count"] == 1
        resp = client.post(TOPIC_PATHS["customers/redact"], content=data,
                           headers=_signed(data, "customers/redact", SHOES))
        assert resp.status_code == 200 and req(pg, rid)["delivery_count"] == 2
    finally:
        holder.close()
        router_module.set_receipt_session_factory(None)


def test_storage_without_revision_0123_is_a_503(pg, monkeypatch):
    with pg.engine.begin() as conn:
        _migrate(conn, _M0123, "downgrade")
    client, router_module = _http_client(pg, monkeypatch)
    try:
        data = body("shop/redact", SHOES)
        assert client.post(TOPIC_PATHS["shop/redact"], content=data,
                           headers=_signed(data, "shop/redact", SHOES)).status_code == 503
    finally:
        router_module.set_receipt_session_factory(None)


def test_real_dedicated_pool_exhaustion_is_a_bounded_503_and_recovers(pg, monkeypatch):
    """Actual PostgreSQL pool exhaustion (not a simulated timeout): the real
    dedicated receipt factory against the disposable database, all of its
    bounded connections held. The delivery is answered 503 within the
    budget, an unrelated endpoint keeps answering meanwhile, nothing is
    committed, and receipts resume once the connections are released. The
    shared engine's settings are untouched."""
    import httpx

    import session as shared
    from routers import shopify_privacy_webhooks as router_module
    from services.shopify_privacy import config

    shared_before = (shared.engine.pool.size(), shared.engine.pool._timeout, str(shared.engine.url))
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    client, router_module = _http_client(pg, monkeypatch)
    factory = store.dedicated_session_factory(_url(pg.name))
    engine = factory.kw["bind"]
    router_module.set_receipt_session_factory(factory)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router_module.router)

    @app.get("/unrelated")
    async def unrelated():
        return {"ok": True}

    held = [engine.connect() for _ in range(config.RECEIPT_WORKERS)]
    data = body("shop/redact", SHOES)
    try:
        async def scenario():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                started = time.monotonic()
                delivery = asyncio.create_task(http.post(TOPIC_PATHS["shop/redact"], content=data,
                                                         headers=_signed(data, "shop/redact", SHOES)))
                await asyncio.sleep(0.1)
                probe_started = time.monotonic()
                other = await http.get("/unrelated")
                probe_elapsed = time.monotonic() - probe_started
                resp = await delivery
                return resp, other, probe_elapsed, time.monotonic() - started

        resp, other, probe_elapsed, elapsed = asyncio.run(scenario())
        assert resp.status_code == 503 and resp.json() == {"error": "retry"}
        assert elapsed < config.RECEIPT_BUDGET_SECONDS + 0.5
        assert other.status_code == 200 and probe_elapsed < 0.5
        assert count(pg, "shopify_privacy_requests") == 0
        for conn in held:
            conn.close()
        held = []
        deadline = time.monotonic() + 5
        while router_module._SLOTS._value != config.RECEIPT_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)
        resp = client.post(TOPIC_PATHS["shop/redact"], content=data, headers=_signed(data, "shop/redact", SHOES))
        assert resp.status_code == 200 and count(pg, "shopify_privacy_requests") == 1
        assert (shared.engine.pool.size(), shared.engine.pool._timeout, str(shared.engine.url)) == shared_before
    finally:
        for conn in held:
            conn.close()
        router_module.set_receipt_session_factory(None)
        engine.dispose()


def _held_commit_sessions(pg):
    """A session factory whose next COMMIT is held (after every statement of
    the transaction ran, so its row locks are held) until released."""
    from sqlalchemy import event

    factory = sessionmaker(bind=pg.engine, expire_on_commit=False, future=True)
    gate = SimpleNamespace(armed=True, entered=threading.Event(), release=threading.Event(), pid=None)

    def hold(session):
        if gate.armed:
            gate.armed = False
            gate.pid = session.connection().exec_driver_sql("SELECT pg_backend_pid()").scalar()
            gate.entered.set()
            assert gate.release.wait(30), "held commit never released"

    event.listen(factory, "before_commit", hold)
    return factory, gate


def _wait_until_blocked_by(pg, blocker_pid, timeout=1.2):
    """Positive evidence: a backend waits on a lock held by ``blocker_pid``."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        rows = _sql(pg, "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                        " AND :b = ANY(pg_blocking_pids(pid))", b=blocker_pid)
        if rows:
            return [r["pid"] for r in rows]
        time.sleep(0.02)
    return []


def test_late_delivery_waiting_behind_a_later_one_keeps_timestamps_monotone(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    data = body("customers/redact", SHOES)
    received = datetime.now(timezone.utc) + timedelta(seconds=30)
    _, rid = deliver(pg, "customers/redact", SHOES, data, now=received)
    # Reversed supplied clocks without contention: clamped to the row.
    _, again = deliver(pg, "customers/redact", SHOES, data, now=received - timedelta(seconds=20))
    r = req(pg, rid)
    assert again == rid and r["last_delivery_at"] == r["received_at"] == received
    # Real lock wait with reversed supplied clocks: delivery A (the later clock)
    # holds the open row and is paused just before its COMMIT; delivery B (the
    # earlier clock) must wait on A's backend, then stamp no earlier than A.
    t_early, t_late = received - timedelta(seconds=25), received + timedelta(minutes=5)
    held, gate = _held_commit_sessions(pg)
    payload = parse_privacy_body(data, "customers/redact")
    digest = hashlib.sha256(data).hexdigest()
    box = {}

    def delivery_a():
        try:
            with held() as db:
                box["a"] = store.record_receipt(db, payload=payload, body_sha256=digest, webhook_id=None,
                                                triggered_at=None, now=t_late)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            box["a_error"] = exc

    def delivery_b():
        try:
            box["b"] = deliver(pg, "customers/redact", SHOES, data, now=t_early)
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            box["b_error"] = exc

    ta, tb = threading.Thread(target=delivery_a), threading.Thread(target=delivery_b)
    try:
        ta.start()
        assert gate.entered.wait(20)              # A holds the row lock, uncommitted
        tb.start()
        waiters = _wait_until_blocked_by(pg, gate.pid)
        assert waiters and tb.is_alive() and "b" not in box, "B never waited on A's row lock"
    finally:
        gate.release.set()                        # A commits its later stamp (also on failure)
        ta.join(20)
        if tb.is_alive() or tb.ident is not None:
            tb.join(20)
    assert "a_error" not in box and "b_error" not in box, (box.get("a_error"), box.get("b_error"))
    assert box["a"] == ("duplicate", rid) and box["b"] == ("duplicate", rid)
    r = req(pg, rid)
    assert r["delivery_count"] == 4
    assert r["last_delivery_at"] == t_late and r["last_delivery_at"] >= r["received_at"]


_COMMIT_LOCK_KEY = 7_100_424_242


def _install_commit_hold_trigger(pg):
    """Fixture only (disposable database): a deferred constraint trigger on new
    privacy requests that, at COMMIT, waits on an advisory lock."""
    _sql(pg, f"""
        CREATE FUNCTION fixture_hold_privacy_commit() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock({_COMMIT_LOCK_KEY});
            RETURN NULL;
        END $$""")
    _sql(pg, "CREATE CONSTRAINT TRIGGER fixture_hold_privacy_commit AFTER INSERT ON shopify_privacy_requests"
             " DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fixture_hold_privacy_commit()")


def test_late_commit_or_blocked_commit_answers_503_and_the_redelivery_is_idempotent(pg, monkeypatch):
    """(1) PostgreSQL is executing the receipt's COMMIT at the deadline (a
    fixture deferred trigger waits on an advisory lock during COMMIT): the
    delivery is answered 503, the request stays invisible until the COMMIT
    completes, and Shopify's redelivery attaches to that one request.
    (2) A receipt blocked on the row past the deadline rolls back instead of
    committing: nothing changes and the redelivery attaches. One request
    throughout."""
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    client, router_module = _http_client(pg, monkeypatch)
    from services.shopify_privacy import config

    def slots_idle():
        end = time.monotonic() + 10
        while router_module._SLOTS._value != config.RECEIPT_WORKERS and time.monotonic() < end:
            time.sleep(0.01)
        return router_module._SLOTS._value == config.RECEIPT_WORKERS

    data = body("shop/redact", SHOES)
    headers = _signed(data, "shop/redact", SHOES)
    lock_holder = pg.engine.connect()
    row_holder = None
    row_tx = None
    poster = None
    try:
        # (1) COMMIT in flight at the deadline, completing after the 503.
        _install_commit_hold_trigger(pg)
        holder_pid = lock_holder.execute(text("SELECT pg_backend_pid()")).scalar()
        lock_holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _COMMIT_LOCK_KEY})
        monkeypatch.setattr(router_module, "RECEIPT_BUDGET_SECONDS", 0.5)
        started = time.monotonic()
        resp = client.post(TOPIC_PATHS["shop/redact"], content=data, headers=headers)
        assert resp.status_code == 503 and resp.json() == {"error": "retry"}
        assert time.monotonic() - started < 1.5
        committing = _sql(pg, "SELECT pid FROM pg_stat_activity WHERE state = 'active' AND query = 'COMMIT'"
                              " AND wait_event_type = 'Lock' AND :h = ANY(pg_blocking_pids(pid))", h=holder_pid)
        assert len(committing) == 1, "the receipt's backend is not executing COMMIT behind the fixture lock"
        assert count(pg, "shopify_privacy_requests") == 0          # uncommitted: invisible
        lock_holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _COMMIT_LOCK_KEY})
        assert slots_idle()
        rows = _sql(pg, "SELECT id, delivery_count FROM shopify_privacy_requests")
        assert len(rows) == 1 and rows[0]["delivery_count"] == 1    # the late COMMIT landed, exactly once
        rid = rows[0]["id"]
        monkeypatch.setattr(router_module, "RECEIPT_BUDGET_SECONDS", config.RECEIPT_BUDGET_SECONDS)
        assert client.post(TOPIC_PATHS["shop/redact"], content=data, headers=headers).status_code == 200
        assert req(pg, rid)["delivery_count"] == 2 and count(pg, "shopify_privacy_requests") == 1

        # (2) blocked on the row past the deadline: rolled back, never committed late.
        monkeypatch.setattr(router_module, "RECEIPT_BUDGET_SECONDS", 0.3)
        row_holder = pg.engine.connect()
        row_tx = row_holder.begin()
        row_pid = row_holder.execute(text("SELECT pg_backend_pid()")).scalar()
        row_holder.execute(text("SELECT id FROM shopify_privacy_requests WHERE id = :i FOR UPDATE"), {"i": rid})
        box = {}
        poster = threading.Thread(target=lambda: box.setdefault(
            "resp", client.post(TOPIC_PATHS["shop/redact"], content=data, headers=headers)))
        poster.start()
        assert _wait_until_blocked_by(pg, row_pid), "the receipt never waited on the row"
        poster.join(10)
        assert box["resp"].status_code == 503
        row_tx.rollback()                 # released after the 503, before the receipt's own lock timeout
        row_tx = None
        assert slots_idle()
        assert req(pg, rid)["delivery_count"] == 2                   # rolled back, not committed late
        monkeypatch.setattr(router_module, "RECEIPT_BUDGET_SECONDS", config.RECEIPT_BUDGET_SECONDS)
        assert client.post(TOPIC_PATHS["shop/redact"], content=data, headers=headers).status_code == 200
        assert req(pg, rid)["delivery_count"] == 3 and count(pg, "shopify_privacy_requests") == 1
    finally:
        try:
            lock_holder.execute(text("SELECT pg_advisory_unlock_all()"))
        finally:
            lock_holder.close()
        if row_tx is not None:
            row_tx.rollback()
        if row_holder is not None:
            row_holder.close()
        if poster is not None:
            poster.join(10)
        slots_idle()
        router_module.set_receipt_session_factory(None)


def test_signed_replay_flood_is_bounded_and_lifecycle_still_progresses(pg):
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    connect(pg, merchant, SHOES, fake)
    data = body("shop/redact", SHOES)
    n = MAX_EVENTS_PER_REQUEST + 40
    for i in range(n):
        wid = f"{i:08x}-bdd9-4b37-8a5f-bf7d70bcd043"
        deliver(pg, "shop/redact", SHOES, data, webhook_id=wid)
        if i % 10 == 0:
            tick(pg)
    rid = _one(pg, "SELECT id FROM shopify_privacy_requests")["id"]
    assert count(pg, "shopify_privacy_requests") == 1 and req(pg, rid)["delivery_count"] == n
    assert count(pg, "shopify_privacy_deliveries") == MAX_DELIVERY_ROWS_PER_REQUEST
    assert count(pg, "shopify_privacy_request_events") == MAX_EVENTS_PER_REQUEST
    assert count(pg, "shopify_privacy_request_events", "action = 'event_log_capped'") == 1
    # Fences still use the counters: the genuine uninstall is still processed.
    uninstall(pg, SHOES, fake)
    deliver(pg, "shop/redact", SHOES, data)
    for _ in range(4):
        tick(pg)
    r = req(pg, rid)
    assert r["state"] == "blocked_policy" and r["evaluated_delivery_count"] == n + 1


# ── Worker: customer requests and audit scope ─────────────────────────────────

def test_matched_customer_request_persists_the_bounded_assessment(pg):
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    connect(pg, merchant, SHOES, fake)
    _, rid = deliver(pg, "customers/data_request", SHOES)
    assert tick(pg)["claimed"] == 1
    r = req(pg, rid)
    assert r["state"] == "assessed" and r["reason"] == "no_structured_customer_records_in_audited_scope"
    p = r["proposed_action"]
    assert r["audit_version"] == AUDIT_VERSION == p["audit_version"]
    assert p["catalog_free_text"] == "not_assessed" and p["generic_customer_mapping"] == "unresolved"
    assert p["legal_fulfillment"] == "not_asserted" and "merchant_response_policy" in p["blocked_on"]
    assert r["completed_at"] is None and r["deadline_at"] > datetime.now(timezone.utc)


LEGACY_ROWS = {
    "integrations": "INSERT INTO integrations (provider, tenant_id, external_store_id, config) VALUES ('shopify', :t,"
                    " '7100000001', jsonb_build_object('store_email', :e, 'access_token', :tok))",
    "webhook_events": "INSERT INTO webhook_events (provider, tenant_id, store_id, raw_body, status, attempts,"
                      " received_at, created_at, updated_at) VALUES ('shopify', NULL, :d, :e, 'processed', 0, now(),"
                      " now(), now())",
    "products": "INSERT INTO products (tenant_id, title, source, in_stock) VALUES (:t, 'عطر ورد 100ml', 'shopify', true)",
    "orders": "INSERT INTO orders (tenant_id, status, source) VALUES (:t, 'paid', 'shopify')",
    "tenant_settings": "INSERT INTO tenant_settings (tenant_id, show_nahla_branding, branding_text, store_settings)"
                       " VALUES (:t, true, 'x', jsonb_build_object('shopify_access_token', :tok))",
    "customer_name_provenance": None,
    "external_customer_profiles": None,
}


@pytest.mark.parametrize("legacy", sorted(LEGACY_ROWS))
def test_any_legacy_shopify_footprint_blocks_the_no_data_outcome(pg, legacy):
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    connect(pg, merchant, SHOES, fake)
    email, tok = _email(), "shpat_" + secrets.token_hex(16)
    p = dict(t=merchant.tenant_id, d=SHOES, e=email, tok=tok)
    if legacy == "customer_name_provenance":
        cid = _one(pg, "INSERT INTO customers (tenant_id, name) VALUES (:t, 'نورة عبدالله') RETURNING id", **p)["id"]
        _sql(pg, "INSERT INTO customer_name_provenance (tenant_id, customer_id, authority, source, merchant_locked,"
                 " created_at, updated_at) VALUES (:t, :c, 'verified_ecommerce', 'shopify_order', false, now(), now())",
             c=cid, **p)
    elif legacy == "external_customer_profiles":
        iid = _one(pg, "INSERT INTO integrations (provider, tenant_id) VALUES ('shopify', :t) RETURNING id", **p)["id"]
        _sql(pg, "INSERT INTO external_customer_profiles (id, tenant_id, identity_namespace, integration_connection_id,"
                 " external_customer_ref, created_at, updated_at) VALUES (gen_random_uuid(), :t, 'shopify', :i,"
                 " '191167', now(), now())",
             i=iid, **p)
    else:
        _sql(pg, LEGACY_ROWS[legacy], **p)
    _, rid = deliver(pg, "customers/redact", SHOES)
    tick(pg)
    r = req(pg, rid)
    assert r["state"] == "manual_review" and r["reason"] == "legacy_shopify_data_present"
    footprint = r["proposed_action"]["legacy_shopify_footprint"]
    assert sum(footprint["total"].values()) >= 1
    text_ = json.dumps(r["proposed_action"], ensure_ascii=False)
    assert email not in text_ and tok not in text_   # counts only


def test_audited_schema_drift_routes_to_manual_review(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    _sql(pg, "ALTER TABLE shopify_catalog_products ADD COLUMN buyer_note text")
    _, rid = deliver(pg, "customers/redact", SHOES)
    tick(pg)
    assert req(pg, rid)["state"] == "manual_review" and req(pg, rid)["reason"] == "audit_schema_drift"


def test_unavailable_legacy_check_blocks(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    _sql(pg, "ALTER TABLE tenant_settings RENAME COLUMN store_settings TO store_settings_old")
    _, rid = deliver(pg, "customers/redact", SHOES)
    tick(pg)
    assert req(pg, rid)["reason"] == "legacy_audit_unavailable"


def test_unknown_identity_is_reconsidered_when_the_connect_flow_commits(pg):
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    _, rid = deliver(pg, "customers/redact", SHOES)
    tick(pg)
    assert req(pg, rid)["state"] == "manual_review" and req(pg, rid)["reason"] == "unknown_shop_identity"
    connect(pg, merchant, SHOES, fake)
    summary = tick(pg)
    assert summary["reconsidered"] == 1
    r = req(pg, rid)
    assert r["identity_state"] == "matched" and r["tenant_id"] == merchant.tenant_id
    assert r["state"] == "assessed"


def test_connect_committing_between_unknown_evaluation_and_write_is_fenced(pg, monkeypatch):
    """The real connect flow commits after the worker evaluated "unknown" and
    before it writes: the write-time identity fence requeues the request."""
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    _, rid = deliver(pg, "shop/redact", SHOES)
    real_decide = worker.decide
    fired = {}

    def decide_then_connect(db, request, identity, now):
        decision = real_decide(db, request, identity, now)
        if not fired:
            fired["identity"] = identity.state
            connect(pg, merchant, SHOES, fake)   # commits in other sessions
        return decision

    monkeypatch.setattr(worker, "decide", decide_then_connect)
    with pg.Session() as db:
        request_id, lease = worker.claim_next(db, now=datetime.now(timezone.utc))
        outcome = worker.process(db, request_id=request_id, lease=lease)
    assert fired["identity"] == "unknown" and outcome == "requeued"
    r = req(pg, rid)
    assert r["state"] == "received" and r["lease_id"] is None and r["tenant_id"] is None
    monkeypatch.setattr(worker, "decide", real_decide)
    tick(pg)
    r = req(pg, rid)
    assert r["identity_state"] == "matched" and r["tenant_id"] == merchant.tenant_id
    # The delivery was observed before any installation existed: review, never
    # a probe-based or no-data outcome for the new installation.
    assert r["state"] == "manual_review" and r["reason"] == "installed_after_last_delivery"


# ── Worker: shop redaction lifecycle ──────────────────────────────────────────

def test_confirmed_uninstall_yields_a_counts_only_blocked_proposal(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    assert uninstall(pg, SHOES, fake) == "uninstalled"
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    r = req(pg, rid)
    assert r["state"] == "blocked_policy" and r["reason"] == "destructive_policy_not_approved"
    assert r["eligible_generation"] == conn_row(pg, SHOES)["generation"]
    proposal = r["proposed_action"]
    assert proposal["delete_on_approval"] == mirror_counts(pg, conn["id"], merchant.tenant_id)
    assert proposal["delete_on_approval"] == {t: (2 if t == "shopify_catalog_import_runs" else 1)
                                              for t in audit.MIRROR_DELETE_ORDER}
    assert proposal["retained_pending_policy"]["shopify_connections"] == 1
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8  # nothing deleted


def test_active_installation_requests_a_probe_and_a_retained_probe_stays_open(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    r, c = req(pg, rid), conn_row(pg, SHOES)
    assert r["state"] == "awaiting_evidence" and r["probe_delivery_count"] == 1
    assert c["revalidation_requested_at"] is not None and r["probe_reconcile_version"] == c["reconcile_request_version"]
    assert foundation_probe(pg, SHOES, fake) == "retained"
    tick(pg, now=datetime.now(timezone.utc) + timedelta(hours=7))
    r = req(pg, rid)
    assert r["state"] == "manual_review" and r["reason"] == "active_installation_retained"
    assert r["completed_at"] is None and r["deadline_at"] > datetime.now(timezone.utc)
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8


def test_retained_replay_never_swallows_a_later_genuine_uninstall_with_the_same_body(pg):
    """Review case: an identical signed shop/redact replayed while active is
    retained; the genuine uninstall later (quarantine in the same generation,
    then the tombstone) and the same body again must reach a proposal."""
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    data = body("shop/redact", SHOES)
    _, rid = deliver(pg, "shop/redact", SHOES, data)
    tick(pg)
    assert foundation_probe(pg, SHOES, fake) == "retained"
    tick(pg, now=datetime.now(timezone.utc) + timedelta(hours=7))
    assert req(pg, rid)["reason"] == "active_installation_retained"
    generation = conn_row(pg, SHOES)["generation"]
    # Genuine uninstall: the webhook quarantines in the SAME generation.
    fake.uninstall(SHOES)
    with pg.Session() as db:
        lifecycle.record_uninstall(db, identity=SignedShopIdentity(shop_id=shop_id(SHOES), shop_domain=SHOES),
                                   webhook_id="genuine-1", body_sha256=secrets.token_hex(32), triggered_at_header=None)
    assert conn_row(pg, SHOES)["status"] == "quarantined" and conn_row(pg, SHOES)["generation"] == generation
    _, again = deliver(pg, "shop/redact", SHOES, data)
    assert again == rid and req(pg, rid)["state"] == "received"
    tick(pg)
    r = req(pg, rid)
    assert r["state"] == "awaiting_evidence" and r["probe_delivery_count"] == 2  # a new probe for the new delivery
    assert foundation_probe(pg, SHOES, fake) == "uninstalled"
    assert conn_row(pg, SHOES)["generation"] == generation + 1
    tick(pg, now=datetime.now(timezone.utc) + timedelta(hours=7))
    r = req(pg, rid)
    assert r["state"] == "blocked_policy" and r["eligible_generation"] == generation + 1


def test_reinstall_never_erases_the_new_generation_and_never_suppresses_a_later_request(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    data = body("shop/redact", SHOES)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES, data)
    tick(pg)
    assert req(pg, rid)["state"] == "blocked_policy"
    old_generation = req(pg, rid)["eligible_generation"]
    connect(pg, merchant, SHOES, fake)                       # same-tenant reinstall
    new_generation = conn_row(pg, SHOES)["generation"]
    assert new_generation == old_generation + 1 and conn_row(pg, SHOES)["status"] == "active"
    with pg.Session() as db:
        with pytest.raises(cleanup.ExecutionRefused) as excinfo:
            cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY, operator_ref="fixture-op",
                                           env=DESTRUCTIVE_ENV)
    assert excinfo.value.code == cleanup.C_CONNECTION_CHANGED
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8 and req(pg, rid)["state"] == "blocked_policy"
    # The old body arriving after the reinstall attaches; the active new
    # installation is probed and retained, never erased; the request stays open.
    deliver(pg, "shop/redact", SHOES, data)
    tick(pg)
    assert req(pg, rid)["state"] == "awaiting_evidence"
    assert foundation_probe(pg, SHOES, fake) == "retained"
    tick(pg, now=datetime.now(timezone.utc) + timedelta(hours=7))
    assert req(pg, rid)["reason"] == "active_installation_retained"
    # A later genuine uninstall with the identical body is still processed.
    uninstall(pg, SHOES, fake)
    deliver(pg, "shop/redact", SHOES, data)
    tick(pg)
    r = req(pg, rid)
    assert r["state"] == "blocked_policy" and r["eligible_generation"] == conn_row(pg, SHOES)["generation"]
    assert count(pg, "shopify_privacy_requests") == 1


def test_delivery_observed_before_the_current_installation_is_reviewed_not_erased(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    connect(pg, merchant, SHOES, fake)        # reinstall before the worker runs
    uninstall(pg, SHOES, fake)                # ... and a new uninstall
    tick(pg)
    r = req(pg, rid)
    assert r["state"] == "manual_review" and r["reason"] == "installed_after_last_delivery"


def test_old_leased_worker_cannot_close_away_a_newer_delivery(pg):
    fake = FakeShopify()
    shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    data = body("shop/redact", SHOES)
    _, rid = deliver(pg, "shop/redact", SHOES, data)
    with pg.Session() as db:
        request_id, lease = worker.claim_next(db, now=datetime.now(timezone.utc))
        request = db.get(worker.ShopifyPrivacyRequest, request_id)
        identity = store.resolve_identity(db, request.shop_domain, request.shop_gid)
        evaluated = int(request.delivery_count)
        decision = worker.decide(db, request, identity, datetime.now(timezone.utc))
        db.rollback()
        deliver(pg, "shop/redact", SHOES, data)            # newer delivery while leased
        outcome = worker.apply_decision(db, request_id=request_id, lease=lease, evaluated_count=evaluated,
                                        identity=identity, decision=decision, now=datetime.now(timezone.utc))
    assert decision.state == "blocked_policy" and outcome == "requeued"
    r = req(pg, rid)
    assert r["state"] == "received" and r["delivery_count"] == 2 and r["eligible_generation"] is None
    tick(pg)
    assert req(pg, rid)["state"] == "blocked_policy" and req(pg, rid)["evaluated_delivery_count"] == 2


def test_identical_body_across_reinstall_while_the_old_request_is_leased(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    data = body("shop/redact", SHOES)
    _, rid = deliver(pg, "shop/redact", SHOES, data)
    with pg.Session() as db:
        request_id, lease = worker.claim_next(db, now=datetime.now(timezone.utc))
        request = db.get(worker.ShopifyPrivacyRequest, request_id)
        identity = store.resolve_identity(db, request.shop_domain, request.shop_gid)
        decision = worker.decide(db, request, identity, datetime.now(timezone.utc))
        evaluated = int(request.delivery_count)
        db.rollback()
        connect(pg, merchant, SHOES, fake)                  # reinstall during the lease
        deliver(pg, "shop/redact", SHOES, data)             # identical body after it
        outcome = worker.apply_decision(db, request_id=request_id, lease=lease, evaluated_count=evaluated,
                                        identity=identity, decision=decision, now=datetime.now(timezone.utc))
    assert decision.state == "blocked_policy" and outcome == "requeued"
    tick(pg)
    r = req(pg, rid)
    assert r["state"] == "awaiting_evidence" and r["delivery_count"] == 2   # the new installation gets a probe
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8


def test_lifecycle_change_during_evaluation_requeues(pg):
    fake = FakeShopify()
    shop_with_mirror(pg, "a", SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    with pg.Session() as db:
        request_id, lease = worker.claim_next(db, now=datetime.now(timezone.utc))
        request = db.get(worker.ShopifyPrivacyRequest, request_id)
        identity = store.resolve_identity(db, request.shop_domain, request.shop_gid)
        decision = worker.decide(db, request, identity, datetime.now(timezone.utc))
        db.rollback()
        fake.uninstall(SHOES)
        with pg.Session() as other:
            lifecycle.record_uninstall(other, identity=SignedShopIdentity(shop_id=shop_id(SHOES), shop_domain=SHOES),
                                       webhook_id="x", body_sha256=secrets.token_hex(32), triggered_at_header=None)
        outcome = worker.apply_decision(db, request_id=request_id, lease=lease,
                                        evaluated_count=int(request.delivery_count), identity=identity,
                                        decision=decision, now=datetime.now(timezone.utc))
    assert outcome == "requeued" and req(pg, rid)["probe_delivery_count"] is None


def test_crashed_worker_lease_expires_and_a_stale_writer_is_fenced(pg, monkeypatch):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    _, rid = deliver(pg, "customers/redact", SHOES)
    t0 = datetime.now(timezone.utc)
    with pg.Session() as db:
        _, stale_lease = worker.claim_next(db, now=t0)      # worker A "crashes" after claiming
    with pg.Session() as db:
        assert worker.claim_next(db, now=t0 + timedelta(seconds=5)) is None   # still leased
    later = t0 + timedelta(seconds=worker.WORKER_LEASE_SECONDS + 5)
    with pg.Session() as db:
        request_id, fresh = worker.claim_next(db, now=later)  # restart recovery
    assert fresh != stale_lease and req(pg, rid)["attempts"] == 2
    with pg.Session() as db:
        assert worker.process(db, request_id=rid, lease=stale_lease, now=later) == "lease_lost"
    # A holder whose lease ran out by the write-time clock is fenced too.
    monkeypatch.setattr(worker, "wall_clock", lambda: later + timedelta(seconds=worker.WORKER_LEASE_SECONDS + 1))
    with pg.Session() as db:
        assert worker.process(db, request_id=rid, lease=fresh, now=later) == "lease_lost"
    monkeypatch.undo()
    with pg.Session() as db:
        assert worker.process(db, request_id=rid, lease=fresh, now=later) == "assessed"


def test_awaiting_evidence_backs_off_then_times_out_to_review(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    now = datetime.now(timezone.utc)
    tick(pg, now=now)
    r = req(pg, rid)
    assert r["state"] == "awaiting_evidence" and r["next_attempt_at"] == now + timedelta(seconds=worker.backoff_seconds(1))
    assert tick(pg, now=now + timedelta(seconds=1))["claimed"] == 0   # not due yet
    for _ in range(worker.WORKER_MAX_EVIDENCE_ATTEMPTS + 2):
        now = now + timedelta(days=1)
        tick(pg, now=now)
        if req(pg, rid)["state"] != "awaiting_evidence":
            break
    r = req(pg, rid)
    assert r["state"] == "manual_review" and r["reason"] == "uninstall_evidence_timeout"


def test_in_flight_authorization_defers_the_proposal(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    begin_auth(pg, merchant, SHOES)                       # a reinstall has started
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    assert req(pg, rid)["state"] == "awaiting_evidence" and req(pg, rid)["reason"] == "in_flight_authorization"


def test_disconnected_shop_is_reviewed_without_a_confirmed_uninstall(pg):
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    connect(pg, merchant, SHOES, fake)
    with pg.Session() as db:
        lifecycle.disconnect(db, actor=merchant.actor(pg), shop_domain=SHOES)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    assert req(pg, rid)["state"] == "manual_review" and req(pg, rid)["reason"] == "no_confirmed_uninstall"


def test_competing_workers_claim_disjoint_requests(pg):
    fake = FakeShopify()
    connect(pg, Merchant(pg, "a"), SHOES, fake)
    for i in range(6):
        deliver(pg, "customers/redact", SHOES, body("customers/redact", SHOES, orders_to_redact=[i + 1]))
    claimed, lock = [], threading.Lock()

    def claimer():
        with pg.Session() as db:
            while True:
                got = worker.claim_next(db, now=datetime.now(timezone.utc))
                if got is None:
                    return
                with lock:
                    claimed.append(got[0])

    threads = [threading.Thread(target=claimer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 6


# ── Cleanup helper and executor (disposable fixtures only) ────────────────────

def _snapshot_unrelated(pg, tenant_ids):
    return {
        "products": [dict(r) for r in _sql(pg, "SELECT id, tenant_id, external_id, source FROM products ORDER BY id")],
        "tenants": count(pg, "tenants"), "users": count(pg, "users"), "integrations": count(pg, "integrations"),
        "connections": [dict(r) for r in _sql(pg, "SELECT id, tenant_id, shop_domain, shop_gid, status, generation"
                                                  " FROM shopify_connections ORDER BY id")],
        "connection_audit": count(pg, "shopify_connection_audit"),
        "webhook_events": count(pg, "shopify_webhook_events"), "oauth_states": count(pg, "shopify_oauth_states"),
    }


def test_cleanup_helper_deletes_exactly_one_connection_and_tenant(pg):
    fake = FakeShopify()
    a, conn_a = shop_with_mirror(pg, "a", SHOES, fake)
    b, conn_b = shop_with_mirror(pg, "b", SHIRTS, fake)
    salla_product(pg, a.tenant_id, "1")        # same raw id as the mirror rows
    salla_product(pg, b.tenant_id, "1")
    before = _snapshot_unrelated(pg, [a.tenant_id, b.tenant_id])
    with pg.Session() as db:
        nothing = cleanup.delete_shop_mirror(db, connection_id=conn_a["id"], tenant_id=b.tenant_id)
        db.commit()
    assert sum(nothing.values()) == 0                       # mismatched pair matches nothing
    with pg.Session() as db:
        deleted = cleanup.delete_shop_mirror(db, connection_id=conn_a["id"], tenant_id=a.tenant_id)
        db.commit()
    assert deleted == {t: 1 for t in audit.MIRROR_DELETE_ORDER if t != "shopify_catalog_import_runs"} | {
        "shopify_catalog_import_runs": 2}
    assert sum(mirror_counts(pg, conn_a["id"], a.tenant_id).values()) == 0
    assert mirror_counts(pg, conn_b["id"], b.tenant_id) == {t: (2 if t == "shopify_catalog_import_runs" else 1)
                                                           for t in audit.MIRROR_DELETE_ORDER}
    assert _snapshot_unrelated(pg, [a.tenant_id, b.tenant_id]) == before


def test_executor_refuses_without_flag_or_policy_and_changes_nothing(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    for env, policy, code in ((None, FIXTURE_POLICY, cleanup.C_DESTRUCTIVE_DISABLED),
                              (DESTRUCTIVE_ENV, cleanup.approved_policy(), cleanup.C_POLICY_NOT_APPROVED)):
        with pg.Session() as db:
            with pytest.raises(cleanup.ExecutionRefused) as excinfo:
                cleanup.execute_shop_redaction(db, request_id=rid, policy=policy, operator_ref="fixture-op",
                                               env=env or {})
        assert excinfo.value.code == code
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8
    assert req(pg, rid)["state"] == "blocked_policy" and cleanup.approved_policy() is None


def test_fixture_policy_execution_erases_only_the_mirror_scope(pg):
    fake = FakeShopify()
    a, conn_a = shop_with_mirror(pg, "a", SHOES, fake)
    b, conn_b = shop_with_mirror(pg, "b", SHIRTS, fake)
    salla_product(pg, a.tenant_id, "1")
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    before = _snapshot_unrelated(pg, [a.tenant_id, b.tenant_id])
    with pg.Session() as db:
        deleted = cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY, operator_ref="fixture-op",
                                                 env=DESTRUCTIVE_ENV)
    assert sum(deleted.values()) == 8 and sum(mirror_counts(pg, conn_a["id"], a.tenant_id).values()) == 0
    assert sum(mirror_counts(pg, conn_b["id"], b.tenant_id).values()) == 8
    after = _snapshot_unrelated(pg, [a.tenant_id, b.tenant_id])
    assert after == before   # tombstone, audit, events, states, products, tenants, users unchanged
    r = req(pg, rid)
    assert r["state"] == "completed" and r["policy_version"] == FIXTURE_POLICY.version
    evidence = r["completion_evidence"]
    assert evidence["deleted"] == deleted and evidence["executed_scope"] == "catalog_mirror_only"
    assert evidence["legal_fulfillment"] == "not_asserted"
    assert evidence["retained_obligations"] == "pending_owner_policy"
    assert evidence["retained_by_policy"] == dict(FIXTURE_POLICY.retained)
    assert set(evidence["deleted"]) == set(audit.MIRROR_DELETE_ORDER)
    assert count(pg, "shopify_privacy_request_events", "action = 'executed'") == 1
    # A completed request is never reused: the same body opens a new one.
    _, again = deliver(pg, "shop/redact", SHOES)
    assert again != rid


@pytest.mark.parametrize("change", ["legacy_row", "schema_drift", "audit_version"])
def test_executor_revalidates_audit_scope_and_preserves_everything(pg, change):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    assert req(pg, rid)["state"] == "blocked_policy"
    if change == "legacy_row":
        _sql(pg, "INSERT INTO integrations (provider, tenant_id) VALUES ('shopify', :t)", t=merchant.tenant_id)
        expected = cleanup.C_LEGACY_FOOTPRINT
    elif change == "schema_drift":
        _sql(pg, "ALTER TABLE shopify_catalog_locations ADD COLUMN contact_line text")
        expected = cleanup.C_AUDIT_SCHEMA_CHANGED
    else:
        _sql(pg, "UPDATE shopify_privacy_requests SET audit_version = 'older-audit-v0' WHERE id = :i", i=rid)
        expected = cleanup.C_AUDIT_VERSION_CHANGED
    with pg.Session() as db:
        with pytest.raises(cleanup.ExecutionRefused) as excinfo:
            cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY, operator_ref="fixture-op",
                                           env=DESTRUCTIVE_ENV)
    assert excinfo.value.code == expected
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8
    assert req(pg, rid)["state"] == "blocked_policy" and req(pg, rid)["completed_at"] is None


def test_executor_refuses_an_unsupported_extra_scope_and_changes_nothing(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    before = (_snapshot_unrelated(pg, [merchant.tenant_id]), req(pg, rid), mirror_counts(pg, conn["id"], merchant.tenant_id))
    wider = cleanup.ApprovedErasurePolicy(version="synthetic-fixture-policy-v0", approved_by="test-fixture",
                                          scope=frozenset({"catalog_mirror", "unsupported_scope"}))
    with pg.Session() as db:
        with pytest.raises(cleanup.ExecutionRefused) as excinfo:
            cleanup.execute_shop_redaction(db, request_id=rid, policy=wider, operator_ref="fixture-op",
                                           env=DESTRUCTIVE_ENV)
    assert excinfo.value.code == cleanup.C_POLICY_SCOPE_UNSUPPORTED
    after = (_snapshot_unrelated(pg, [merchant.tenant_id]), req(pg, rid), mirror_counts(pg, conn["id"], merchant.tenant_id))
    assert after == before and after[1]["state"] == "blocked_policy"


def test_executor_refuses_a_redelivery_since_evaluation(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    data = body("shop/redact", SHOES)
    _, rid = deliver(pg, "shop/redact", SHOES, data)
    tick(pg)
    deliver(pg, "shop/redact", SHOES, data)
    with pg.Session() as db:
        with pytest.raises(cleanup.ExecutionRefused) as excinfo:
            cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY, operator_ref="fixture-op",
                                           env=DESTRUCTIVE_ENV)
    assert excinfo.value.code == cleanup.C_NOT_BLOCKED_POLICY
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 8


def test_reinstall_racing_the_executor_waits_and_builds_a_new_generation(pg, monkeypatch):
    """The executor holds the connection row (FOR NO KEY UPDATE) while it
    deletes. A reinstall started meanwhile (its start and callback do not wait:
    they only reference the row) reaches its claim, which waits on the lock —
    proven from pg_stat_activity — and then creates generation + 1 over an
    empty mirror. Every actor uses its own session and connection."""
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    erased_generation = conn_row(pg, SHOES)["generation"]
    entered, release = threading.Event(), threading.Event()
    real_delete = cleanup.delete_shop_mirror
    box = {}

    def slow_delete(db, **kw):
        out = real_delete(db, **kw)
        box["executor_pid"] = db.execute(text("SELECT pg_backend_pid()")).scalar()
        entered.set()
        assert release.wait(60)
        return out

    monkeypatch.setattr(cleanup, "delete_shop_mirror", slow_delete)

    def execute():
        with pg.Session() as db:
            box["deleted"] = cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY,
                                                            operator_ref="fixture-op", env=DESTRUCTIVE_ENV)

    def reinstall(handle):
        with pg.Session() as db:
            box["claim"] = run(lifecycle.complete_authorization(db, actor=merchant.actor(pg), handle=handle,
                                                                config=CFG, cipher=CIPHER, api=fake))

    t1 = threading.Thread(target=execute)
    t1.start()
    assert entered.wait(30)
    state = begin_auth(pg, merchant, SHOES)                 # not blocked by the executor
    with pg.Session() as db:
        handle = lifecycle.accept_callback(db, callback=VerifiedCallback(SHOES, state, fake.issue_code(SHOES)),
                                           config=CFG, cipher=CIPHER)
    t2 = threading.Thread(target=reinstall, args=(handle,))
    t2.start()
    blocked = []
    for _ in range(100):
        blocked = _sql(pg, "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND :e = ANY("
                           "pg_blocking_pids(pid))", e=box["executor_pid"])
        if blocked:
            break
        time.sleep(0.05)
    assert blocked, "the reinstall's claim never waited on the executor's lock"
    assert "claim" not in box and t1.is_alive()
    # The catalog fence's FOR SHARE on the connection conflicts too.
    with pg.engine.connect() as fence:
        with fence.begin():
            fence.execute(text("SET LOCAL lock_timeout = '200ms'"))
            with pytest.raises(DBAPIError) as excinfo:
                fence.execute(text("SELECT id FROM shopify_connections WHERE id = :i FOR SHARE"), {"i": conn["id"]})
            assert getattr(excinfo.value.orig, "pgcode", None) == "55P03"   # lock_not_available
    release.set()
    t1.join(30)
    t2.join(30)
    assert sum(box["deleted"].values()) == 8 and box["claim"]["status"] == "active"
    c = conn_row(pg, SHOES)
    assert c["status"] == "active" and c["generation"] == erased_generation + 1
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 0
    assert req(pg, rid)["completion_evidence"]["connection_generation"] == erased_generation


def test_running_import_blocks_the_executor(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    uninstall(pg, SHOES, fake)
    _, rid = deliver(pg, "shop/redact", SHOES)
    tick(pg)
    _sql(pg, "INSERT INTO shopify_catalog_import_runs (tenant_id, connection_id, run_seq, shop_domain, shop_gid,"
             " generation, api_version, status, lease_id, lease_expires_at) VALUES (:t, :c, 9, :d, :g, 1, '2026-10',"
             " 'running', 'lease-x', now() + interval '10 minutes')",
         t=merchant.tenant_id, c=conn["id"], d=SHOES, g=GIDS[SHOES])
    with pg.Session() as db:
        with pytest.raises(cleanup.ExecutionRefused) as excinfo:
            cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY, operator_ref="fixture-op",
                                           env=DESTRUCTIVE_ENV)
    assert excinfo.value.code == cleanup.C_IN_FLIGHT
    assert sum(mirror_counts(pg, conn["id"], merchant.tenant_id).values()) == 9


# ── Read-only audit script ────────────────────────────────────────────────────

def _run_audit(name):
    env = {**os.environ, "DATABASE_URL": make_url(_url(name)).set(drivername="postgresql").render_as_string(
        hide_password=False)}
    proc = subprocess.run([sys.executable, str(_REPO / "scripts" / "audit_shopify_privacy_readonly.py")],
                          cwd=str(_REPO), env=env, capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout


def test_audit_script_is_read_only_and_counts_only(pg):
    fake = FakeShopify()
    merchant = Merchant(pg, "a")
    connect(pg, merchant, SHOES, fake)
    email, tok = _email(), "shpat_" + secrets.token_hex(16)
    _sql(pg, "INSERT INTO integrations (provider, tenant_id, config) VALUES ('shopify', :t,"
             " jsonb_build_object('store_email', :e, 'access_token', :k))", t=merchant.tenant_id, e=email, k=tok)
    deliver(pg, "customers/redact", SHOES)
    _sql(pg, "CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
    _sql(pg, "INSERT INTO alembic_version VALUES ('0123')")
    before = dump_text(pg)
    rc, out = _run_audit(pg.name)
    report = json.loads(out)
    assert rc == 0 and report["verdict"] == "WARN" and report["schema"]["privacy_0123"]
    checks = {c["check"]: c for c in report["checks"]}
    assert checks["legacy.integrations_provider_shopify"]["count"] == 1
    assert checks["audited_schema.tables_with_column_drift"]["status"] == "PASS"
    assert checks["requests.customers/redact.received"]["count"] == 1
    for value in (email, tok, SHOES, GIDS[SHOES]):
        assert value not in out
    assert dump_text(pg) == before


def test_audit_script_flags_a_privacy_table_collision_before_0123():
    name = _fresh_db("shpv_col")
    try:
        _build(name, privacy=False)
        eng = create_engine(_url(name), poolclass=NullPool, future=True)
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version VALUES ('0122')"))
            conn.execute(text("CREATE TABLE shopify_privacy_requests (id bigint)"))
        eng.dispose()
        rc, out = _run_audit(name)
        report = json.loads(out)
        assert rc == 1 and report["verdict"] == "FAIL"
        labels = {c["check"] for c in report["checks"] if c["status"] == "FAIL"}
        assert {"schema.partial_privacy_tables", "schema.privacy_tables_without_revision_0123"} <= labels
    finally:
        _drop(name)


def test_audit_script_flags_unrecorded_privacy_tables_without_alembic_version():
    name = _fresh_db("shpv_nover")
    try:
        _build(name)                         # full, well-formed 0123 tables; no alembic_version table at all
        rc, out = _run_audit(name)
        report = json.loads(out)
        assert rc == 1 and report["verdict"] == "FAIL"
        assert "schema.privacy_tables_without_revision_0123" in {
            c["check"] for c in report["checks"] if c["status"] == "FAIL"}
    finally:
        _drop(name)


def test_audit_script_never_prints_stored_strings_of_malformed_tables():
    sentinel = "s" + secrets.token_hex(6) + "@ex.test"   # fits topic varchar(32) / state varchar(24)
    for shape in ("unchecked_columns", "extra_column"):
        name = _fresh_db("shpv_mal")
        try:
            _build(name, privacy=False)
            eng = create_engine(_url(name), poolclass=NullPool, future=True)
            with eng.begin() as conn:
                conn.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
                conn.execute(text("INSERT INTO alembic_version VALUES ('0123')"))
                create_privacy_tables(conn)
                # Same columns, no constraints: arbitrary stored strings.
                for c in ("ck_shopify_privacy_requests_topic", "ck_shopify_privacy_requests_state",
                          "ck_shopify_privacy_requests_shop_domain", "ck_shopify_privacy_requests_reason",
                          "ck_shopify_privacy_requests_topic_fields", "ck_shopify_privacy_requests_schedule"):
                    conn.execute(text(f"ALTER TABLE shopify_privacy_requests DROP CONSTRAINT {c}"))
                if shape == "extra_column":
                    conn.execute(text("ALTER TABLE shopify_privacy_requests ADD COLUMN note text"))
                conn.execute(text(
                    "INSERT INTO shopify_privacy_requests (topic, shop_domain, shop_gid, body_sha256, identity_state,"
                    " state, received_at, last_delivery_at, deadline_at) VALUES (:s, :s, 'gid://shopify/Shop/1',"
                    " repeat('a', 64), 'unknown', :s, now(), now(), now() + interval '1 day')"), {"s": sentinel})
            eng.dispose()
            rc, out = _run_audit(name)
            report = json.loads(out)
            assert sentinel not in out and sentinel.split("@")[0] not in out
            assert rc == 1 and report["verdict"] == "FAIL"
            failed = {c["check"] for c in report["checks"] if c["status"] == "FAIL"}
            if shape == "extra_column":
                assert "schema.privacy_tables_malformed" in failed
                assert not any(c["check"].startswith("requests.") for c in report["checks"])
            else:
                assert "requests.unknown_topic_or_state" in failed
        finally:
            _drop(name)


# ── Salla regression: the shared hub is untouched by the whole flow ───────────

def test_privacy_flow_never_touches_salla_or_shared_catalog_rows(pg):
    fake = FakeShopify()
    merchant, conn = shop_with_mirror(pg, "a", SHOES, fake)
    salla_id = salla_product(pg, merchant.tenant_id, "1")
    _sql(pg, "INSERT INTO integrations (provider, tenant_id, external_store_id) VALUES ('salla', :t, '1')",
         t=merchant.tenant_id)
    hub_before = [dict(r) for r in _sql(pg, "SELECT * FROM products ORDER BY id")]
    integrations_before = [dict(r) for r in _sql(pg, "SELECT * FROM integrations ORDER BY id")]
    for topic in ("customers/data_request", "customers/redact", "shop/redact"):
        deliver(pg, topic, SHOES)
    uninstall(pg, SHOES, fake)
    deliver(pg, "shop/redact", SHOES, body("shop/redact", SHOES, extra_field=True))
    for _ in range(3):
        tick(pg)
    with pg.Session() as db:
        rid = db.execute(text("SELECT id FROM shopify_privacy_requests WHERE state = 'blocked_policy'")).scalar()
    with pg.Session() as db:
        cleanup.execute_shop_redaction(db, request_id=rid, policy=FIXTURE_POLICY, operator_ref="fixture-op",
                                       env=DESTRUCTIVE_ENV)
    assert [dict(r) for r in _sql(pg, "SELECT * FROM products ORDER BY id")] == hub_before
    assert [dict(r) for r in _sql(pg, "SELECT * FROM integrations ORDER BY id")] == integrations_before
    assert _one(pg, "SELECT source FROM products WHERE id = :i", i=salla_id)["source"] == "salla"
