"""Dormant Shopify catalog import against REAL PostgreSQL.

Throw-away databases are cloned from a template holding ``tenants``,
``users``, ``products``, ``product_variants`` and ``sync_logs`` (from
``models.Base``, so Salla writers can run beside the import), the Shopify
foundation tables created by revision 0121 itself and the catalog mirror
created by revision 0122 itself. Shopify is the in-process 2026-10 fake in
``shopify_catalog_fake.py`` (no network, no merchant, every credential
generated at runtime). Neutral generic merchants and categories only.

Without ``NAHLA_RELIABILITY_PG_ADMIN_DSN`` the module is skipped (reported as
skipped, never as passed); with ``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no
variable it fails. Inventoried in the required PostgreSQL proofs.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

_TESTS = Path(__file__).resolve().parent
_REPO = _TESTS.parents[1]
for entry in (str(_REPO), str(_REPO / "backend"), str(_REPO / "database"), str(_TESTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")

from shopify_catalog_fake import FakeCatalogShop, gid, level_gid  # noqa: E402

from services.shopify_catalog import config as C  # noqa: E402
from services.shopify_catalog import errors as E  # noqa: E402
from services.shopify_catalog import importer as importer_module  # noqa: E402
from services.shopify_catalog import store  # noqa: E402
from services.shopify_catalog.graphql import AdminGraphQL  # noqa: E402
from services.shopify_catalog.models import (  # noqa: E402
    CATALOG_TABLE_NAMES,
    KIND_PRODUCT,
    add_connection_tenant_key,
    create_catalog_tables,
)
from services.shopify_connection import crypto, lifecycle  # noqa: E402
from services.shopify_connection.actor import revalidate_actor  # noqa: E402
from services.shopify_connection.config import evaluate_availability  # noqa: E402
from services.shopify_connection.models import SHOPIFY_TABLE_NAMES  # noqa: E402
from backend.tests import _alembic_topology_probe as topo  # noqa: E402
from services.shopify_connection.oauth import TokenGrant  # noqa: E402
from services.shopify_connection.webhooks import SignedShopIdentity  # noqa: E402

_M0121 = _REPO / "database" / "migrations" / "versions" / "0121_shopify_connection_foundation.py"
_M0122 = _REPO / "database" / "migrations" / "versions" / "0122_shopify_catalog_mirror.py"

ENV = {
    "NAHLA_SHOPIFY_CONNECTION_ENABLED": "1",
    "NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED": "1",
    "SHOPIFY_CLIENT_ID": "test-client-" + secrets.token_hex(4),
    "SHOPIFY_CLIENT_SECRET": "shopify-test-secret-" + secrets.token_hex(16),
    "SHOPIFY_OAUTH_REDIRECT_URI": "https://api.shop-review.example.test/merchant/integrations/shopify/callback",
    "DASHBOARD_URL": "https://app.shop-review.example.test",
    "SHOPIFY_TOKEN_ENC_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode("ascii"),
}
CFG = evaluate_availability(ENV).config
CIPHER = crypto.TokenCipher(CFG.encryption_key) if CFG else None

SHOES = ("generic-shoes-store.myshopify.com", "gid://shopify/Shop/7100000001")
SHIRTS = ("cotton-shirts-demo.myshopify.com", "gid://shopify/Shop/7100000002")


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


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migrate(conn, path: Path, direction: str = "upgrade") -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module = _module(path, f"m_{path.stem}")
    with Operations.context(MigrationContext.configure(conn)):
        getattr(module, direction)()


def _base_tables(conn) -> None:
    from models import Base, Product, ProductVariant, SyncLog, Tenant, User

    Base.metadata.create_all(conn, tables=[Tenant.__table__, User.__table__, Product.__table__,
                                           ProductVariant.__table__, SyncLog.__table__])


def _fresh_db(prefix: str, *, template: str | None = None) -> str:
    name = f"{prefix}_{secrets.token_hex(4)}"
    _admin(f"CREATE DATABASE {name}" + (f" TEMPLATE {template}" if template else ""))
    return name


def _drop(name: str) -> None:
    _admin(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def _build(name: str, *, catalog: bool = True) -> None:
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    try:
        with eng.begin() as conn:
            _base_tables(conn)
            _migrate(conn, _M0121)
            if catalog:
                _migrate(conn, _M0122)
    finally:
        eng.dispose()


@pytest.fixture(scope="module")
def template_db():
    name = _fresh_db("shc_tmpl")
    _build(name)
    try:
        yield name
    finally:
        _drop(name)


@pytest.fixture()
def pg(template_db, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    name = _fresh_db("shc", template=template_db)
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


def _dump(pg, table: str):
    return [dict(r) for r in _sql(pg, f"SELECT * FROM {table} ORDER BY id")]


def _mirror(pg):
    return {t: _dump(pg, t) for t in ("shopify_catalog_products", "shopify_catalog_inventory_items",
                                      "shopify_catalog_variants", "shopify_catalog_locations",
                                      "shopify_catalog_inventory_levels")}


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

    def actor(self, pg):
        with pg.Session() as db:
            return revalidate_actor(db, self.payload())


def _encrypt(purpose, value, *, tenant_id, shop, generation):
    return CIPHER.encrypt(value, crypto.token_context(purpose, tenant_id=tenant_id, shop_domain=shop,
                                                      generation=generation))


def connect(pg, merchant: Merchant, fake: FakeCatalogShop, *, scopes="read_products", generation=1) -> int:
    """A synthetic active connection as the foundation would have stored it
    (claim is proven by the foundation's own suite; here only the state)."""
    now = datetime.now(timezone.utc)
    access, refresh = fake.issue_token(), f"shprt_{secrets.token_hex(16)}"
    fake.refresh_tokens = getattr(fake, "refresh_tokens", set()) | {refresh}
    return _sql(pg, (
        "INSERT INTO shopify_connections (tenant_id, shop_domain, shop_gid, status, generation, credential_version,"
        " access_token_enc, access_token_expires_at, refresh_token_enc, refresh_token_expires_at, granted_scopes,"
        " connected_at) VALUES (:t, :d, :g, 'active', :gen, 1, :a, :ae, :r, :re, :s, :now) RETURNING id"),
        t=merchant.tenant_id, d=fake.shop_domain, g=fake.shop_gid, gen=generation,
        a=_encrypt(crypto.PURPOSE_ACCESS_TOKEN, access, tenant_id=merchant.tenant_id, shop=fake.shop_domain,
                   generation=generation), ae=now + timedelta(hours=1),
        r=_encrypt(crypto.PURPOSE_REFRESH_TOKEN, refresh, tenant_id=merchant.tenant_id, shop=fake.shop_domain,
                   generation=generation), re=now + timedelta(days=30), s=scopes, now=now)[0]["id"]


def rotate_credential(pg, connection_id: int, fake: FakeCatalogShop, *, retire_old: bool = False) -> str:
    """Another worker's refresh: new access token, credential version + 1."""
    row = _sql(pg, "SELECT tenant_id, shop_domain, generation, credential_version FROM shopify_connections "
                   "WHERE id = :i", i=connection_id)[0]
    token = fake.issue_token()
    if retire_old:
        fake.tokens = {token}
    _sql(pg, "UPDATE shopify_connections SET access_token_enc = :a, credential_version = credential_version + 1,"
             " access_token_expires_at = :e WHERE id = :i",
         a=_encrypt(crypto.PURPOSE_ACCESS_TOKEN, token, tenant_id=row["tenant_id"], shop=row["shop_domain"],
                    generation=row["generation"]), e=datetime.now(timezone.utc) + timedelta(hours=1), i=connection_id)
    return token


class FakeTokenApi:
    """The foundation's token endpoint for this fake shop (refresh only)."""

    def __init__(self, fake: FakeCatalogShop):
        self.fake = fake

    async def refresh(self, *, shop_domain, refresh_token):
        token = self.fake.issue_token()
        return TokenGrant(token, f"shprt_{secrets.token_hex(16)}", 3600, 7776000, frozenset({"read_products"}))


async def _no_sleep(_seconds):
    return None


def begin(pg, merchant: Merchant, fake: FakeCatalogShop, actor=None) -> store.RunHandle:
    actor = actor or merchant.actor(pg)
    with pg.Session() as db:
        return store.begin_run(db, actor=actor, shop_domain=fake.shop_domain)


def execute(pg, handle: store.RunHandle, fake: FakeCatalogShop, **kw) -> str:
    imp = importer_module.CatalogImporter(
        session_factory=pg.Session, handle=handle, api=FakeTokenApi(fake), cipher=CIPHER,
        client=AdminGraphQL(transport=fake.transport(), sleep=_no_sleep), sleep=_no_sleep, **kw)
    return asyncio.run(imp.run())


def import_once(pg, merchant: Merchant, fake: FakeCatalogShop) -> tuple:
    handle = begin(pg, merchant, fake)
    return handle, execute(pg, handle, fake)


def run_row(pg, run_id: int):
    return _sql(pg, "SELECT * FROM shopify_catalog_import_runs WHERE id = :i", i=run_id)[0]


def status_of(pg, merchant: Merchant):
    with pg.Session() as db:
        return store.import_summaries(db, merchant.tenant_id)


def shoe_store(shop=SHOES, **kw) -> FakeCatalogShop:
    fake = FakeCatalogShop(*shop, **kw)
    fake.add_product("حذاء رياضي أبيض", options=[("المقاس", ["40", "41"])], variants=[
        {"price": "199.00", "title": "40", "options": [("المقاس", "40")], "sku": "SNK-W-40"},
        {"price": "199.50", "title": "41", "options": [("المقاس", "41")], "compare_at": "249.00", "image": True},
    ])
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90", "tracked": False}])
    fake.add_product("عطر ورد 100ml", images=0, variants=[{"price": "250.00"}], status="DRAFT", published=False)
    return fake


# ── Migration 0122 ────────────────────────────────────────────────────────────

def _schema(name: str) -> dict:
    eng = create_engine(_url(name), poolclass=NullPool, future=True)
    out = {}
    try:
        with eng.connect() as conn:
            present = set(inspect(conn).get_table_names())
            for table in list(CATALOG_TABLE_NAMES) + ["shopify_connections"]:
                if table not in present:
                    out[table] = None
                    continue
                cols = conn.execute(text(
                    "SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale,"
                    " is_nullable, column_default FROM information_schema.columns WHERE table_name = :t"
                    " ORDER BY ordinal_position"), {"t": table}).all()
                cons = conn.execute(text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint"
                    " WHERE conrelid = CAST(:t AS regclass) AND contype <> 'n' ORDER BY conname"), {"t": table}).all()
                idx = conn.execute(text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t ORDER BY indexname"),
                    {"t": table}).all()
                out[table] = {"columns": [tuple(c) for c in cols], "constraints": [tuple(c) for c in cons],
                              "indexes": [tuple(i) for i in idx]}
    finally:
        eng.dispose()
    return out


def test_migration_0122_creates_exactly_the_model_schema(template_db):
    other = _fresh_db("shc_meta")
    try:
        eng = create_engine(_url(other), poolclass=NullPool, future=True)
        with eng.begin() as conn:
            _base_tables(conn)
            _migrate(conn, _M0121)
            add_connection_tenant_key(conn)
            create_catalog_tables(conn)
        eng.dispose()
        assert _schema(template_db) == _schema(other)
    finally:
        _drop(other)


def test_migration_0122_refuses_without_the_foundation():
    name = _fresh_db("shc_nofound")
    try:
        eng = create_engine(_url(name), poolclass=NullPool, future=True)
        with eng.begin() as conn:
            _base_tables(conn)
        with pytest.raises(RuntimeError, match="0121 must be applied"):
            with eng.begin() as conn:
                _migrate(conn, _M0122)
        assert not set(CATALOG_TABLE_NAMES) & set(inspect(eng).get_table_names())
        eng.dispose()
    finally:
        _drop(name)


@pytest.mark.parametrize("preexisting", ["table", "partial_table", "connection_key"])
def test_migration_0122_refuses_preexisting_objects_unchanged(preexisting):
    name = _fresh_db("shc_pre")
    try:
        _build(name, catalog=False)
        eng = create_engine(_url(name), poolclass=NullPool, future=True)
        with eng.begin() as conn:
            if preexisting == "table":
                conn.execute(text("CREATE TABLE shopify_catalog_products (id bigint primary key)"))
            elif preexisting == "partial_table":
                conn.execute(text("CREATE TABLE shopify_catalog_import_runs (id bigint, status text)"))
            else:
                add_connection_tenant_key(conn)
        before = _schema(name)
        with pytest.raises(RuntimeError, match="not created by revision 0122"):
            with eng.begin() as conn:
                _migrate(conn, _M0122)
        assert _schema(name) == before
        eng.dispose()
    finally:
        _drop(name)


def test_migration_0122_downgrade_drops_only_its_objects(pg):
    merchant = Merchant(pg, "down")
    fake = shoe_store()
    connect(pg, merchant, fake)
    with pg.engine.begin() as conn:
        _migrate(conn, _M0122, "downgrade")
    names = set(inspect(pg.engine).get_table_names())
    assert not set(CATALOG_TABLE_NAMES) & names
    assert set(SHOPIFY_TABLE_NAMES) <= names and {"products", "product_variants"} <= names
    assert not [c for c in inspect(pg.engine).get_unique_constraints("shopify_connections")
                if c["name"] == "uq_shopify_connections_id_tenant"]
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_connections")[0]["n"] == 1


def test_alembic_chain_upgrades_0121_to_0122_and_back():
    name = _fresh_db("shc_chain")
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

        proc = alembic("upgrade", "0121")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S1, baseline, planted, counts, "S1 after upgrade 0121")
        proc = alembic("upgrade", "0122")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S2, baseline, planted, counts, "S2 after upgrade 0122")
        eng = create_engine(url, poolclass=NullPool, future=True)
        assert set(CATALOG_TABLE_NAMES) <= set(inspect(eng).get_table_names())

        proc = alembic("downgrade", "0122@-1")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S1, baseline, planted, counts, "S1 after downgrade 0122@-1")
        assert not (set(CATALOG_TABLE_NAMES) & set(inspect(eng).get_table_names()))
        assert set(SHOPIFY_TABLE_NAMES) <= set(inspect(eng).get_table_names())

        proc = alembic("downgrade", "0121@-1")
        assert proc.returncode == 0, proc.stderr[-3000:]
        topo.assert_step(url, topo.S0, baseline, planted, counts, "S0 after downgrade 0121@-1")
        assert not (set(SHOPIFY_TABLE_NAMES) & set(inspect(eng).get_table_names()))
        eng.dispose()
    finally:
        _drop(name)


# ── Import, products-only and per-location ────────────────────────────────────

def test_products_only_import_commits_the_mirror_and_nulls_inventory_evidence(pg):
    merchant = Merchant(pg, "shoes")
    fake = shoe_store()
    connect(pg, merchant, fake)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    run = run_row(pg, handle.run_id)
    assert run["status"] == "committed" and run["api_version"] == "2026-10"
    assert run["inventory_mode"] == "scope_missing" and run["shop_currency"] == "SAR"
    assert (run["products_count"], run["variants_count"], run["inventory_items_count"]) == (3, 4, 4)
    assert run["locations_count"] is None and run["inventory_levels_count"] is None   # not 0: not read
    assert fake.ops("NahlaCatalogLocations") == 0 and fake.ops("NahlaCatalogInventory") == 0
    products = {r["title"]: r for r in _dump(pg, "shopify_catalog_products")}
    assert products["عطر ورد 100ml"]["status"] == "DRAFT"
    assert products["عطر ورد 100ml"]["online_store_published_at"] is None
    assert products["عطر ورد 100ml"]["image_url"] is None     # missing stays absent
    variants = {r["title"]: r for r in _dump(pg, "shopify_catalog_variants")}
    assert variants["41"]["price"] == Decimal("199.50") and variants["41"]["compare_at_price"] == Decimal("249.00")
    assert variants["41"]["image_source"] == "variant" and variants["40"]["image_source"] == "product"
    assert all(v["currency_code"] == "SAR" for v in variants.values())
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_staged_records")[0]["n"] == 0
    # Nothing reaches the shared catalog hub.
    assert _sql(pg, "SELECT count(*) AS n FROM products")[0]["n"] == 0
    assert _sql(pg, "SELECT count(*) AS n FROM product_variants")[0]["n"] == 0
    [summary] = status_of(pg, merchant)
    assert summary["import"]["state"] == "imported"
    evidence = summary["import"]["last_success"]
    assert evidence["current_installation"] is True and evidence["api_version"] == "2026-10"
    assert evidence["inventory"] == {"mode": "scope_missing", "locations": None, "levels": None}
    with pg.Session() as db:
        avail = store.variant_availability(db, tenant_id=merchant.tenant_id, connection_id=handle.connection_id,
                                           variant_gid=gid("ProductVariant", next(iter(
                                               v for v, x in fake.variants.items() if x.title == "40"))))
    assert avail == {"state": "unknown", "basis": "inventory_scope_missing", "policy": "DENY"}


def _inventory_store():
    fake = FakeCatalogShop(*SHIRTS, scopes=("read_products", "read_inventory"))
    online = fake.add_location("مستودع الرياض")
    shop_only = fake.add_location("متجر جدة", online=False)
    closed = fake.add_location("مستودع قديم", active=False)
    shirt = fake.add_product("قميص قطني أزرق", variants=[
        {"price": "89.90", "title": "M", "levels": {online: 4, shop_only: 30, closed: 50}},
        {"price": "89.90", "title": "L", "tracked": False},
    ])
    item = next(v.item for v in fake.variants.values() if v.product == shirt and v.title == "M")
    fake.add_variant(shirt, price="89.90", title="M — listing 2", position=3, item=item)   # shared item
    fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00", "requires_components": True,
                                                  "levels": {online: 9}}])
    return fake


def test_per_location_inventory_with_a_wider_mock_grant(pg):
    merchant = Merchant(pg, "shirts")
    fake = _inventory_store()
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    run = run_row(pg, handle.run_id)
    assert run["inventory_mode"] == "per_location"
    assert (run["locations_count"], run["inventory_levels_count"], run["inventory_items_count"]) == (3, 4, 3)
    levels = _dump(pg, "shopify_catalog_inventory_levels")
    assert len(levels) == 4 and all("?inventory_item_id=" in r["inventory_level_gid"] for r in levels)
    by_title = {}
    with pg.Session() as db:
        for number, v in fake.variants.items():
            by_title[v.title] = store.variant_availability(db, tenant_id=merchant.tenant_id, connection_id=cid,
                                                           variant_gid=gid("ProductVariant", number))
    m = by_title["M"]
    assert m["state"] == "tracked" and m["online_fulfillable_available"] == 4   # never 84
    assert (m["fulfillable_locations"], m["excluded_locations"], m["variants_sharing_item"]) == (1, 2, 2)
    assert by_title["M — listing 2"]["online_fulfillable_available"] == 4      # same item, same units
    assert by_title["L"] == {"state": "untracked", "policy": "DENY"}
    assert by_title["Default Title"] == {"state": "unknown", "basis": "bundle_components"}
    [summary] = status_of(pg, merchant)
    assert summary["import"]["last_success"]["inventory"] == {"mode": "per_location", "locations": 3, "levels": 4}


def test_more_than_one_page_of_every_nested_connection(pg, monkeypatch):
    for name, value in (("PRODUCTS_PAGE", 1), ("NESTED_VARIANTS_PAGE", 1), ("FOLLOWUP_VARIANTS_PAGE", 1),
                        ("NESTED_PRODUCT_MEDIA_PAGE", 1), ("FOLLOWUP_MEDIA_PAGE", 1),
                        ("NESTED_VARIANT_MEDIA_PAGE", 1), ("LOCATIONS_PAGE", 1), ("NESTED_LEVELS_PAGE", 1),
                        ("FOLLOWUP_LEVELS_PAGE", 1), ("INVENTORY_ITEMS_PER_QUERY", 1), ("CONFIRM_NODES_PER_QUERY", 1)):
        monkeypatch.setattr(C, name, value)
    merchant = Merchant(pg, "pages")
    fake = FakeCatalogShop(*SHOES, scopes=("read_products", "read_inventory"))
    a, b = fake.add_location("مستودع الرياض"), fake.add_location("مستودع الدمام")
    product = fake.add_product("حذاء رياضي أبيض", images=3, variants=[
        {"price": "199.00", "title": "40", "levels": {a: 1, b: 2}, "image": True},
        {"price": "199.00", "title": "41", "levels": {a: 3}},
        {"price": "199.00", "title": "42"},
    ])
    linked = fake.products[product].media[:2]
    for v in fake.variants.values():
        if v.title == "42":
            v.media = list(linked)                    # two linked product images, >1 page
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90"}])
    connect(pg, merchant, fake, scopes="read_products,read_inventory")
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed", run_row(pg, handle.run_id)["failure_reason"]
    # Every connection had more than one page: >= 2 top-level pages, and at
    # least one follow-up page after each embedded first page.
    assert fake.ops("NahlaCatalogProducts") >= 2 and fake.ops("NahlaCatalogLocations") >= 2
    for op in ("NahlaCatalogProductVariants", "NahlaCatalogProductMedia", "NahlaCatalogVariantMedia",
               "NahlaCatalogInventoryLevels"):
        assert fake.ops(op) >= 1, op
    run = run_row(pg, handle.run_id)
    assert (run["products_count"], run["variants_count"], run["inventory_levels_count"]) == (2, 4, 3)
    images = [r for r in _dump(pg, "shopify_catalog_products") if r["title"] == "حذاء رياضي أبيض"][0]["images"]
    assert len(images) == 4


# ── Identity and isolation ────────────────────────────────────────────────────

def test_identical_raw_ids_in_two_tenants_and_shops_stay_isolated(pg):
    a, b = Merchant(pg, "a"), Merchant(pg, "b")
    fake_a, fake_b = shoe_store(), shoe_store(SHIRTS)
    for number, price in zip(sorted(fake_b.variants), ("1.00", "2.00", "3.00", "4.00")):
        fake_b.variants[number].price = price
    assert set(fake_a.products) == set(fake_b.products)   # the same raw ids in both shops
    assert set(fake_a.variants) == set(fake_b.variants)
    cid_a, cid_b = connect(pg, a, fake_a), connect(pg, b, fake_b)
    assert import_once(pg, a, fake_a)[1] == "committed"
    assert import_once(pg, b, fake_b)[1] == "committed"
    rows = _dump(pg, "shopify_catalog_products")
    by_conn = {}
    for r in rows:
        by_conn.setdefault(r["connection_id"], set()).add(r["product_gid"])
        assert r["tenant_id"] == (a.tenant_id if r["connection_id"] == cid_a else b.tenant_id)
    assert by_conn[cid_a] == by_conn[cid_b]
    prices_b = {str(r["price"]) for r in _dump(pg, "shopify_catalog_variants") if r["connection_id"] == cid_b}
    assert prices_b == {"1.00", "2.00", "3.00", "4.00"}
    assert [s["shop_domain"] for s in status_of(pg, a)] == [SHOES[0]]
    actor_b = b.actor(pg)
    with pg.Session() as db:
        with pytest.raises(store.StartRefused) as exc:
            store.begin_run(db, actor=actor_b, shop_domain=SHOES[0])
    assert exc.value.code == store.S_NOT_FOUND
    with pg.Session() as db:
        vgid = gid("ProductVariant", next(iter(fake_a.variants)))
        assert store.variant_availability(db, tenant_id=b.tenant_id, connection_id=cid_a,
                                          variant_gid=vgid)["basis"] == "not_in_mirror"


def test_salla_writers_with_colliding_raw_ids_never_touch_the_mirror(pg, monkeypatch):
    """The real Salla product writer and a Salla-style variant row use the
    very raw ids Shopify uses for this tenant's mirror."""
    from integrations.salla.sync import products as salla_products

    merchant = Merchant(pg, "collide")
    fake = shoe_store()
    connect(pg, merchant, fake)
    assert import_once(pg, merchant, fake)[1] == "committed"
    mirror_before = _mirror(pg)
    raw_product = sorted(fake.products)[0]
    raw_variant = sorted(fake.variants)[0]
    salla_items = [{"id": raw_product, "name": "منتج سلة", "price": {"amount": 10}, "sku": "S-1"},
                   {"id": raw_variant, "name": "منتج سلة آخر", "price": {"amount": 20}, "sku": "S-2"}]

    def salla_handler(request):
        return httpx.Response(200, json={"data": salla_items, "pagination": {"total_pages": 1}})

    real_client = httpx.AsyncClient
    with monkeypatch.context() as patch:   # salla_products.httpx is the global httpx module
        patch.setattr(salla_products.httpx, "AsyncClient",
                      lambda **kw: real_client(transport=httpx.MockTransport(salla_handler), **kw))
        patch.setattr(salla_products, "SessionLocal", pg.Session)
        result = asyncio.run(salla_products.fetch_and_sync_products("store-1", "salla-token", merchant.tenant_id))
    assert result["success"] and result["synced_records"] == 2
    product_id = _sql(pg, "SELECT id FROM products WHERE external_id = :e", e=str(raw_product))[0]["id"]
    _sql(pg, "INSERT INTO product_variants (tenant_id, product_id, salla_variant_id, price, in_stock, is_default)"
             " VALUES (:t, :p, :s, '10', true, false)", t=merchant.tenant_id, p=product_id, s=str(raw_variant))
    assert _mirror(pg) == mirror_before
    hub_before = {t: _dump(pg, t) for t in ("products", "product_variants")}
    handle, outcome = import_once(pg, merchant, fake)   # Shopify again, after Salla
    assert outcome == "committed"
    assert {t: _dump(pg, t) for t in ("products", "product_variants")} == hub_before
    assert not _sql(pg, "SELECT 1 FROM products WHERE external_id LIKE 'gid://%' OR source = 'shopify'")
    assert not _sql(pg, "SELECT 1 FROM product_variants WHERE salla_variant_id LIKE 'gid://%'")


def test_constraints_refuse_cross_tenant_parents_and_malformed_identities(pg):
    a, b = Merchant(pg, "fa"), Merchant(pg, "fb")
    fake_a, fake_b = shoe_store(), FakeCatalogShop(*SHIRTS, scopes=("read_products", "read_inventory"))
    loc = fake_b.add_location("مستودع الرياض")
    fake_b.add_product("قميص قطني أزرق", variants=[{"price": "89.90", "levels": {loc: 2}}])
    connect(pg, a, fake_a)
    connect(pg, b, fake_b, scopes="read_products,read_inventory")
    run_a = import_once(pg, a, fake_a)[0]
    run_b = import_once(pg, b, fake_b)[0]
    pa = _dump(pg, "shopify_catalog_products")[0]
    vb = [v for v in _dump(pg, "shopify_catalog_variants") if v["connection_id"] == run_b.connection_id][0]
    lb = _dump(pg, "shopify_catalog_inventory_levels")[0]

    def refused(statement, **params):
        with pytest.raises((IntegrityError, DBAPIError)):
            _sql(pg, statement, **params)

    # A variant of tenant B pointing at tenant A's product.
    refused("UPDATE shopify_catalog_variants SET product_id = :p WHERE id = :v", p=pa["id"], v=vb["id"])
    # A level pointing at another connection's item.
    item_a = _dump(pg, "shopify_catalog_inventory_items")[0]
    refused("UPDATE shopify_catalog_inventory_levels SET inventory_item_id = :i WHERE id = :l", i=item_a["id"], l=lb["id"])
    # A mirror row stamped with another connection's run.
    refused("UPDATE shopify_catalog_products SET last_seen_run_id = :r WHERE id = :p", r=run_b.run_id, p=pa["id"])
    # A tenant that does not own the connection.
    refused("UPDATE shopify_catalog_products SET tenant_id = :t WHERE id = :p", t=b.tenant_id, p=pa["id"])
    for bad in ("gid://shopify/Product/0", "gid://shopify/Product/12\n", "gid://shopify/ProductVariant/12"):
        refused("UPDATE shopify_catalog_products SET product_gid = :g WHERE id = :p", g=bad, p=pa["id"])
    for bad in ("gid://shopify/InventoryLevel/5", "gid://shopify/InventoryLevel/5?inventory_item_id=6&x=1",
                "gid://shopify/InventoryLevel/5?inventory_item_id=6#f"):
        refused("UPDATE shopify_catalog_inventory_levels SET inventory_level_gid = :g WHERE id = :l", g=bad, l=lb["id"])
    refused("UPDATE shopify_catalog_inventory_items SET tracking = 'unknown' WHERE id = :i", i=item_a["id"])
    refused("UPDATE shopify_catalog_import_runs SET locations_count = 1 WHERE id = :r", r=run_a.run_id)
    refused("UPDATE shopify_catalog_import_runs SET products_count = NULL WHERE id = :r", r=run_a.run_id)
    refused("UPDATE shopify_catalog_variants SET image_source = 'product', image_url = NULL WHERE id = :v", v=vb["id"])
    # One running run per connection, enforced by the database itself.
    handle = begin(pg, a, fake_a)
    refused("INSERT INTO shopify_catalog_import_runs (tenant_id, connection_id, run_seq, shop_domain, shop_gid,"
            " generation, api_version, status, lease_id, lease_expires_at) VALUES (:t, :c, 999, :d, :g, 1,"
            " '2026-10', 'running', 'x', now() + interval '1 minute')",
            t=a.tenant_id, c=handle.connection_id, d=fake_a.shop_domain, g=fake_a.shop_gid)


# ── Publication semantics ─────────────────────────────────────────────────────

def test_reimport_is_idempotent(pg):
    merchant = Merchant(pg, "idem")
    fake = shoe_store()
    connect(pg, merchant, fake)
    first = import_once(pg, merchant, fake)[0]
    before = _mirror(pg)
    second, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    after = _mirror(pg)
    for table in before:
        assert [r["id"] for r in after[table]] == [r["id"] for r in before[table]]
        assert all(r["first_seen_run_id"] == first.run_id for r in after[table])
        assert all(r["last_seen_run_id"] == second.run_id for r in after[table])
        assert all(r["lifecycle"] == "present" for r in after[table])
    run = run_row(pg, second.run_id)
    assert (run["products_removed"], run["variants_removed"], run["stale_skipped"]) == (0, 0, 0)


def test_deletions_are_tombstones_and_only_after_shopify_confirms(pg):
    merchant = Merchant(pg, "del")
    fake = shoe_store()
    connect(pg, merchant, fake)
    import_once(pg, merchant, fake)
    shirt = next(n for n, p in fake.products.items() if p.title == "قميص قطني أزرق")
    shoe = next(n for n, p in fake.products.items() if p.title == "حذاء رياضي أبيض")
    size_41 = next(n for n, v in fake.variants.items() if v.title == "41")
    fake.delete_product(shirt)
    fake.delete_variant(size_41)
    fake.products[shoe].media = [m for m in fake.products[shoe].media if m in fake.media][:1]
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    assert fake.ops("NahlaCatalogConfirm") >= 1
    products = {r["product_gid"]: r for r in _dump(pg, "shopify_catalog_products")}
    assert products[gid("Product", shirt)]["lifecycle"] == "deleted"
    assert products[gid("Product", shirt)]["removed_at"] is not None     # row retained
    variants = {r["variant_gid"]: r for r in _dump(pg, "shopify_catalog_variants")}
    assert variants[gid("ProductVariant", size_41)]["lifecycle"] == "deleted"
    assert sum(1 for v in variants.values() if v["lifecycle"] == "deleted") == 2
    assert sum(1 for i in _dump(pg, "shopify_catalog_inventory_items") if i["lifecycle"] == "detached") == 2
    run = run_row(pg, handle.run_id)
    assert (run["products_removed"], run["variants_removed"]) == (1, 2)
    # An absence Shopify does not confirm publishes nothing.
    before = _mirror(pg)
    perfume = next(n for n, p in fake.products.items() if p.title == "عطر ورد 100ml")
    hidden = fake.products.pop(perfume)
    fake.hooks["NahlaCatalogConfirm"] = lambda f, v, i: f.products.__setitem__(perfume, hidden)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_TRAVERSAL_INCONSISTENT
    assert _mirror(pg) == before


def test_a_failed_run_keeps_the_last_good_catalog(pg, monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    merchant = Merchant(pg, "fail")
    fake = shoe_store()
    connect(pg, merchant, fake)
    good = import_once(pg, merchant, fake)[0]
    before = _mirror(pg)
    fake.products[next(iter(fake.products))].title = "حذاء رياضي أبيض — جديد"
    fake.hooks["NahlaCatalogProducts"] = lambda f, v, i: httpx.Response(503) if v.get("after") else None
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed"
    assert run_row(pg, handle.run_id)["failure_reason"] == E.R_UPSTREAM_UNAVAILABLE
    assert _mirror(pg) == before
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_staged_records")[0]["n"] == 0
    [summary] = status_of(pg, merchant)
    assert summary["import"]["state"] == "error" and summary["import"]["reason"] == E.R_UPSTREAM_UNAVAILABLE
    assert summary["import"]["last_success"]["run"] == run_row(pg, good.run_id)["run_seq"]


def test_partial_variant_payload_fails_and_publishes_nothing(pg):
    merchant = Merchant(pg, "partial")
    fake = shoe_store()
    connect(pg, merchant, fake)
    original = fake._variant_node

    def without_price(v, media):
        node = original(v, media)
        node.pop("price")
        return node

    fake._variant_node = without_price
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_INVALID_RESPONSE
    assert all(not rows for rows in _mirror(pg).values())


def test_stale_observations_are_not_refreshed_and_never_yield_a_quantity(pg):
    merchant = Merchant(pg, "stale")
    fake = _inventory_store()
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    first = import_once(pg, merchant, fake)[0]
    future = datetime.now(timezone.utc) + timedelta(days=365)
    for table in ("shopify_catalog_variants", "shopify_catalog_inventory_items", "shopify_catalog_inventory_levels"):
        _sql(pg, f"UPDATE {table} SET source_updated_at = :f", f=future)
    _sql(pg, "UPDATE shopify_catalog_inventory_levels SET available = 777")
    second, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    assert run_row(pg, second.run_id)["stale_skipped"] > 0
    for table in ("shopify_catalog_variants", "shopify_catalog_inventory_items", "shopify_catalog_inventory_levels"):
        assert all(r["last_seen_run_id"] == first.run_id for r in _dump(pg, table)), table
    assert {r["available"] for r in _dump(pg, "shopify_catalog_inventory_levels")} == {777}
    with pg.Session() as db:
        for number in fake.variants:
            avail = store.variant_availability(db, tenant_id=merchant.tenant_id, connection_id=cid,
                                               variant_gid=gid("ProductVariant", number))
            assert "online_fulfillable_available" not in avail
            assert avail["state"] in ("unknown", "untracked")


def test_shop_currency_change_or_identity_mismatch_fails_closed(pg):
    merchant = Merchant(pg, "cur")
    fake = shoe_store()
    connect(pg, merchant, fake)
    fake.hooks["NahlaCatalogCount"] = lambda f, v, i: setattr(f, "currency", "USD")
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_SOURCE_CHANGED
    fake.hooks.clear()
    fake.currency = "SAR"
    fake.shop_gid = "gid://shopify/Shop/7100000099"
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_IDENTITY_MISMATCH
    assert all(not rows for rows in _mirror(pg).values())


def test_scope_is_read_from_shopify_and_the_stored_grant(pg):
    merchant = Merchant(pg, "scope")
    fake = shoe_store(scopes=("read_inventory",))
    connect(pg, merchant, fake)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_SCOPE_MISSING
    # Shopify reports read_inventory, but the stored (foundation) grant does not carry it.
    fake.scopes = ["read_products", "read_inventory"]
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed" and run_row(pg, handle.run_id)["inventory_mode"] == "scope_missing"
    assert fake.ops("NahlaCatalogLocations") == 0


def test_throttle_and_429_during_an_import_still_commit(pg):
    merchant = Merchant(pg, "throttle")
    fake = shoe_store()
    connect(pg, merchant, fake)
    throttled = {"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
                 "extensions": {"cost": {"requestedQueryCost": 850, "throttleStatus": {
                     "maximumAvailable": 2000.0, "currentlyAvailable": 10.0, "restoreRate": 100.0}}}}
    fake.hooks["NahlaCatalogProducts"] = lambda f, v, i: (
        httpx.Response(200, json=throttled, headers={"X-Shopify-API-Version": "2026-10"}) if i == 0
        else httpx.Response(429, headers={"Retry-After": "2"}) if i == 1 else None)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed" and fake.ops("NahlaCatalogProducts") == 3


# ── Runs, leases and fences under real concurrency ────────────────────────────

def test_concurrent_starts_yield_exactly_one_run(pg):
    merchant = Merchant(pg, "race")
    fake = shoe_store()
    connect(pg, merchant, fake)
    barrier = threading.Barrier(4)
    results = []

    def attempt():
        barrier.wait()
        try:
            results.append(begin(pg, merchant, fake))
        except store.StartRefused as exc:
            results.append(exc.code)

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sum(isinstance(r, store.RunHandle) for r in results) == 1
    assert sorted(r for r in results if isinstance(r, str)) == [store.S_IN_PROGRESS] * 3


def _wait_for_lock_waiters(pg, count: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = _sql(pg, "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = :d AND wait_event_type = 'Lock'",
                 d=pg.name)[0]["n"]
        if n >= count:
            return
        time.sleep(0.05)
    raise AssertionError("expected lock waiters did not appear")


def _hold(pg, statement: str, params: dict, seconds: float):
    ready = threading.Event()

    def body():
        with pg.engine.connect() as conn:
            tx = conn.begin()
            conn.execute(text(statement), params)
            ready.set()
            time.sleep(seconds)
            tx.rollback()

    thread = threading.Thread(target=body)
    thread.start()
    assert ready.wait(10)
    return thread


def _prepared_run(pg, merchant, fake):
    handle = begin(pg, merchant, fake)
    version = _sql(pg, "SELECT credential_version FROM shopify_connections WHERE id = :i",
                   i=handle.connection_id)[0]["credential_version"]
    with pg.Session() as db:
        store.record_preflight(db, handle, currency="SAR", inventory_mode="scope_missing", expected_products=0,
                               credential_version=version)
    return handle, version


@pytest.mark.parametrize("operation", ["heartbeat", "stage", "publish"])
@pytest.mark.parametrize("blocker", ["connection", "run"])
def test_a_lock_wait_beyond_the_lease_expiry_is_refused(pg, operation, blocker):
    merchant = Merchant(pg, f"wait-{operation}-{blocker}")
    fake = shoe_store()
    connect(pg, merchant, fake)
    handle, version = _prepared_run(pg, merchant, fake)
    _sql(pg, "UPDATE shopify_catalog_import_runs SET lease_expires_at = now() + interval '1 second' WHERE id = :i",
         i=handle.run_id)
    lease_before = run_row(pg, handle.run_id)["lease_expires_at"]
    statement = ("SELECT 1 FROM shopify_connections WHERE id = :i FOR UPDATE" if blocker == "connection"
                 else "SELECT 1 FROM shopify_catalog_import_runs WHERE id = :i FOR UPDATE")
    holder = _hold(pg, statement, {"i": handle.connection_id if blocker == "connection" else handle.run_id}, 2.5)
    started = time.monotonic()
    record = (KIND_PRODUCT, gid("Product", 1), None, {"gid": gid("Product", 1)})
    with pytest.raises(E.LeaseLost):
        with pg.Session() as db:
            if operation == "heartbeat":
                store.heartbeat(db, handle, credential_version=version)
            elif operation == "stage":
                store.stage(db, handle, [record], credential_version=version, pages=1)
            else:
                store.publish(db, handle, confirmed_absent={}, pages=1)
    holder.join(10)
    assert time.monotonic() - started >= 2.0   # it really waited past the expiry
    row = run_row(pg, handle.run_id)
    assert row["status"] == "running" and row["lease_expires_at"] == lease_before   # not renewed
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_staged_records")[0]["n"] == 0
    assert all(not rows for rows in _mirror(pg).values())


def test_a_fence_waiting_on_a_disconnect_holds_no_run_lock(pg):
    """Global order connection → run: while a disconnect holds the connection
    exclusively, a waiting fence holds nothing else, so a begin_run-style
    update of the run row proceeds at once (no chain, no cycle), and the
    fence then sees the committed disconnect."""
    merchant = Merchant(pg, "order1")
    fake = shoe_store()
    connect(pg, merchant, fake)
    handle, version = _prepared_run(pg, merchant, fake)
    actor = merchant.actor(pg)
    disconnecting = pg.Session()

    disconnecting.execute(text("SELECT 1 FROM shopify_connections WHERE id = :i FOR UPDATE"),
                          {"i": handle.connection_id})
    outcomes = {}

    def fence():
        try:
            with pg.Session() as db:
                store.heartbeat(db, handle, credential_version=version)
            outcomes["fence"] = "ok"
        except E.ImportFailure as exc:
            outcomes["fence"] = exc.reason
        except Exception as exc:  # noqa: BLE001
            outcomes["fence"] = type(exc).__name__

    t_fence = threading.Thread(target=fence)
    t_fence.start()
    _wait_for_lock_waiters(pg, 1)
    started = time.monotonic()
    _sql(pg, "SET lock_timeout = '3s'; UPDATE shopify_catalog_import_runs SET heartbeat_at = now() WHERE id = :i",
         i=handle.run_id)
    assert time.monotonic() - started < 2.0
    disconnecting.rollback()
    disconnecting.close()
    with pg.Session() as db:
        lifecycle.disconnect(db, actor=actor, shop_domain=fake.shop_domain)
    t_fence.join(30)
    assert outcomes["fence"] in ("ok", E.R_CONNECTION_UNAVAILABLE)
    assert _sql(pg, "SELECT status FROM shopify_connections WHERE id = :i", i=handle.connection_id)[0]["status"] \
        == "disconnected"
    with pytest.raises(E.ImportFailure) as exc:
        with pg.Session() as db:
            store.heartbeat(db, handle, credential_version=version)
    assert exc.value.reason == E.R_CONNECTION_UNAVAILABLE


def test_no_deadlock_between_begin_run_a_queued_disconnect_and_a_fence(pg):
    merchant = Merchant(pg, "order2")
    fake = shoe_store()
    connect(pg, merchant, fake)
    handle, version = _prepared_run(pg, merchant, fake)
    actor = merchant.actor(pg)
    outcomes = {}
    holder = pg.engine.connect()
    tx = holder.begin()
    holder.execute(text("SET LOCAL lock_timeout = '15s'"))
    holder.execute(text("SELECT 1 FROM shopify_connections WHERE id = :i FOR SHARE"), {"i": handle.connection_id})

    def disconnect():
        try:
            with pg.Session() as db:
                outcomes["disconnect"] = lifecycle.disconnect(db, actor=actor, shop_domain=fake.shop_domain)["status"]
        except Exception as exc:  # noqa: BLE001
            outcomes["disconnect"] = type(exc).__name__

    def fence():
        try:
            with pg.Session() as db:
                store.heartbeat(db, handle, credential_version=version)
            outcomes["fence"] = "ok"
        except E.ImportFailure as exc:
            outcomes["fence"] = exc.reason
        except Exception as exc:  # noqa: BLE001
            outcomes["fence"] = type(exc).__name__

    t_disconnect = threading.Thread(target=disconnect)
    t_disconnect.start()
    _wait_for_lock_waiters(pg, 1)          # the exclusive disconnect queues behind the share holder
    t_fence = threading.Thread(target=fence)
    t_fence.start()
    time.sleep(0.5)
    started = time.monotonic()
    holder.execute(text("UPDATE shopify_catalog_import_runs SET heartbeat_at = now() WHERE id = :i"),
                   {"i": handle.run_id})
    assert time.monotonic() - started < 5.0
    tx.commit()
    holder.close()
    t_disconnect.join(30)
    t_fence.join(30)
    assert outcomes["disconnect"] == "disconnected"
    assert outcomes["fence"] in ("ok", E.R_CONNECTION_UNAVAILABLE)   # never a deadlock or lock timeout


def test_tampered_handles_cannot_fence_or_close_another_run(pg):
    a, b = Merchant(pg, "ta"), Merchant(pg, "tb")
    fake_a, fake_b = shoe_store(), FakeCatalogShop(*SHIRTS)
    connect(pg, a, fake_a)
    cid_b = connect(pg, b, fake_b)
    real = begin(pg, a, fake_a)
    forged = store.RunHandle(run_id=real.run_id, lease_id=real.lease_id, tenant_id=b.tenant_id,
                             connection_id=cid_b, shop_domain=fake_b.shop_domain, shop_gid=fake_b.shop_gid,
                             generation=1)
    with pg.Session() as db:
        assert store.finish(db, forged, status="failed", reason=E.R_INTERNAL_ERROR) is False
    with pytest.raises(E.LeaseLost):
        with pg.Session() as db:
            store.heartbeat(db, forged)
    wrong_generation = store.RunHandle(**{**real.__dict__, "generation": 2})
    with pg.Session() as db:
        assert store.finish(db, wrong_generation, status="failed", reason=E.R_INTERNAL_ERROR) is False
    assert run_row(pg, real.run_id)["status"] == "running"


def test_lease_loss_writes_nothing_and_the_next_start_closes_the_run(pg, monkeypatch):
    merchant = Merchant(pg, "lease")
    fake = shoe_store()
    connect(pg, merchant, fake)
    handle = begin(pg, merchant, fake)
    real_clock = store.wall_clock

    def jump(f, v, i):
        monkeypatch.setattr(store, "wall_clock", lambda: real_clock() + timedelta(hours=1))

    fake.hooks["NahlaCatalogProducts"] = jump
    assert execute(pg, handle, fake) == importer_module.OUT_LEASE_LOST
    monkeypatch.setattr(store, "wall_clock", real_clock)
    row = run_row(pg, handle.run_id)
    assert row["status"] == "running" and row["failure_reason"] is None   # this worker wrote nothing
    assert all(not rows for rows in _mirror(pg).values())
    _sql(pg, "UPDATE shopify_catalog_import_runs SET lease_expires_at = now() - interval '1 second' WHERE id = :i",
         i=handle.run_id)
    [summary] = status_of(pg, merchant)
    assert summary["import"]["state"] == "error" and summary["import"]["reason"] == E.R_INTERRUPTED
    assert run_row(pg, handle.run_id)["status"] == "running"   # a status read never writes
    fake.hooks.clear()
    second, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    first = run_row(pg, handle.run_id)
    assert first["status"] == "failed" and first["failure_reason"] == E.R_INTERRUPTED


def test_an_old_completion_cannot_publish_after_recovery(pg):
    merchant = Merchant(pg, "old")
    fake = shoe_store()
    connect(pg, merchant, fake)
    old, version = _prepared_run(pg, merchant, fake)
    with pg.Session() as db:
        store.stage(db, old, [(KIND_PRODUCT, gid("Product", 1), None, {"gid": gid("Product", 1)})],
                    credential_version=version, pages=1)
    _sql(pg, "UPDATE shopify_catalog_import_runs SET lease_expires_at = now() - interval '1 second' WHERE id = :i",
         i=old.run_id)
    new = begin(pg, merchant, fake)   # closes the old run as interrupted
    assert run_row(pg, old.run_id)["failure_reason"] == E.R_INTERRUPTED
    with pytest.raises(E.LeaseLost):
        with pg.Session() as db:
            store.publish(db, old, confirmed_absent={}, pages=1)
    assert all(not rows for rows in _mirror(pg).values())
    assert execute(pg, new, fake) == "committed"


# ── Connection lifecycle during an import ─────────────────────────────────────

def _on_second_page(action):
    def hook(f, v, i):
        if i == 1:
            action()
    return hook


def test_disconnect_mid_import_cancels_and_keeps_the_last_good_catalog(pg, monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    merchant = Merchant(pg, "disc")
    fake = shoe_store()
    connect(pg, merchant, fake)
    import_once(pg, merchant, fake)
    before = _mirror(pg)
    actor = merchant.actor(pg)

    def disconnect():
        with pg.Session() as db:
            lifecycle.disconnect(db, actor=actor, shop_domain=fake.shop_domain)

    fake.calls.clear()
    fake.hooks["NahlaCatalogProducts"] = _on_second_page(disconnect)
    handle = begin(pg, merchant, fake)
    assert execute(pg, handle, fake) == "cancelled"
    assert run_row(pg, handle.run_id)["failure_reason"] == E.R_CONNECTION_UNAVAILABLE
    assert _mirror(pg) == before
    [summary] = status_of(pg, merchant)
    assert summary["connection_status"] == "disconnected"
    # A disconnect tombstones the connection (new generation): nothing was
    # imported for it, the earlier success is labelled as another installation.
    assert summary["import"]["state"] == "not_started" and summary["import"]["can_start"] is False
    assert summary["import"]["last_success"]["current_installation"] is False
    with pg.Session() as db:
        avail = store.variant_availability(db, tenant_id=merchant.tenant_id, connection_id=handle.connection_id,
                                           variant_gid=gid("ProductVariant", next(iter(fake.variants))))
    assert avail["basis"] == "connection_inactive"


def test_uninstall_mid_import_quarantines_and_cancels(pg, monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    merchant = Merchant(pg, "uninst")
    fake = shoe_store()
    connect(pg, merchant, fake)

    def uninstall():
        with pg.Session() as db:
            lifecycle.record_uninstall(db, identity=SignedShopIdentity(shop_id=7100000001, shop_domain=fake.shop_domain),
                                       webhook_id="w-" + secrets.token_hex(4), body_sha256="0" * 64,
                                       triggered_at_header=None)

    fake.hooks["NahlaCatalogProducts"] = _on_second_page(uninstall)
    handle = begin(pg, merchant, fake)
    assert execute(pg, handle, fake) == "cancelled"
    assert run_row(pg, handle.run_id)["failure_reason"] == E.R_CONNECTION_UNAVAILABLE
    assert all(not rows for rows in _mirror(pg).values())


def test_reinstall_mid_import_fences_the_old_generation(pg, monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    merchant = Merchant(pg, "reinst")
    fake = shoe_store()
    cid = connect(pg, merchant, fake)
    import_once(pg, merchant, fake)

    def reinstall():
        token = fake.issue_token()
        _sql(pg, "UPDATE shopify_connections SET generation = 2, credential_version = 1, access_token_enc = :a,"
                 " refresh_token_enc = :r WHERE id = :i",
             a=_encrypt(crypto.PURPOSE_ACCESS_TOKEN, token, tenant_id=merchant.tenant_id, shop=fake.shop_domain,
                        generation=2),
             r=_encrypt(crypto.PURPOSE_REFRESH_TOKEN, "shprt_" + secrets.token_hex(8), tenant_id=merchant.tenant_id,
                        shop=fake.shop_domain, generation=2), i=cid)

    fake.calls.clear()
    fake.hooks["NahlaCatalogProducts"] = _on_second_page(reinstall)
    handle = begin(pg, merchant, fake)
    assert execute(pg, handle, fake) == "cancelled"
    assert run_row(pg, handle.run_id)["failure_reason"] == E.R_CONNECTION_CHANGED
    [summary] = status_of(pg, merchant)
    assert summary["import"]["state"] == "not_started"           # nothing for the new installation yet
    assert summary["import"]["last_success"]["current_installation"] is False
    with pg.Session() as db:
        avail = store.variant_availability(db, tenant_id=merchant.tenant_id, connection_id=cid,
                                           variant_gid=gid("ProductVariant", next(iter(fake.variants))))
    assert avail["basis"] == "previous_installation"
    fake.hooks.clear()
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed" and run_row(pg, handle.run_id)["generation"] == 2
    [summary] = status_of(pg, merchant)
    assert summary["import"]["state"] == "imported"
    assert summary["import"]["last_success"]["current_installation"] is True


def test_a_rotated_credential_mid_import_is_followed_never_reused(pg, monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    merchant = Merchant(pg, "rot")
    fake = shoe_store()
    cid = connect(pg, merchant, fake)
    rotated = {}

    def rotate():
        rotated["token"] = rotate_credential(pg, cid, fake)

    fake.hooks["NahlaCatalogProducts"] = _on_second_page(rotate)
    handle = begin(pg, merchant, fake)
    assert execute(pg, handle, fake) == "committed"
    products_calls = [(i, tok) for i, ((op, _), tok) in enumerate(zip(fake.calls, fake.call_tokens))
                      if op == "NahlaCatalogProducts"]
    assert len(products_calls) == 4   # page 2 fetched again after the rotation
    rotation_at = products_calls[1][0]
    assert all(tok == rotated["token"] for _, tok in
               [(i, t) for i, t in enumerate(fake.call_tokens) if i > rotation_at])
    assert run_row(pg, handle.run_id)["products_count"] == 3
    assert len(_dump(pg, "shopify_catalog_products")) == 3
    # A rotation that also retires the old token: no retry with it.
    fake.calls.clear()
    fake.call_tokens.clear()
    fake.hooks["NahlaCatalogProducts"] = _on_second_page(lambda: rotate_credential(pg, cid, fake, retire_old=True))
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    assert run_row(pg, handle.run_id)["products_count"] == 3


def test_secrets_never_reach_logs_or_storage(pg, caplog):
    caplog.set_level(logging.DEBUG)
    merchant = Merchant(pg, "secret")
    fake = shoe_store()
    cid = connect(pg, merchant, fake)
    import_once(pg, merchant, fake)
    fake.tokens.clear()                     # every credential now refused (401)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_CREDENTIALS_REJECTED
    tokens = set(fake.call_tokens)
    assert tokens
    text_dump = json.dumps({t: _dump(pg, t) for t in CATALOG_TABLE_NAMES}, default=str)
    for token in tokens:
        assert token not in caplog.text and token not in text_dump
    assert "shpat_" not in caplog.text and "shpat_" not in text_dump
    # httpx's own INFO request line names method and path only; never a query.
    assert "graphql.json?" not in caplog.text and "access_token" not in caplog.text.lower()
    reasons = {r["failure_reason"] for r in _dump(pg, "shopify_catalog_import_runs")} - {None}
    assert reasons <= E.FAILURE_REASONS


# ── HTTP wiring with the flags on ─────────────────────────────────────────────

def _http(pg, monkeypatch, payload, *, env=ENV):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from core.database import get_db
    from routers import shopify_catalog_import as router_module

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()

    @app.middleware("http")
    async def inject(request: Request, call_next):
        request.state.jwt_payload = payload
        return await call_next(request)

    def db():
        session = pg.Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = db
    app.include_router(router_module.router)
    return TestClient(app)


def test_http_routes_with_the_flags_on(pg, monkeypatch):
    merchant, other = Merchant(pg, "http"), Merchant(pg, "http2")
    fake = shoe_store()
    connect(pg, merchant, fake)
    jobs = []

    async def job(handle):
        jobs.append(handle.run_id)
        return await importer_module.CatalogImporter(
            session_factory=pg.Session, handle=handle, api=FakeTokenApi(fake), cipher=CIPHER,
            client=AdminGraphQL(transport=fake.transport(), sleep=_no_sleep), sleep=_no_sleep).run()

    monkeypatch.setattr(importer_module, "run_import_job", job)
    base = "/merchant/integrations/shopify/catalog-import"
    client = _http(pg, monkeypatch, merchant.payload())
    assert client.get(f"{base}/status").json()["connections"][0]["import"]["state"] == "not_started"
    resp = client.post(f"{base}/start", json={"shop": fake.shop_domain})
    assert resp.status_code == 202 and resp.json() == {"status": "importing", "shop_domain": fake.shop_domain}
    assert len(jobs) == 1
    body = client.get(f"{base}/status").json()
    assert body["available"] is True and body["api_version"] == "2026-10"
    assert body["connections"][0]["import"]["state"] == "imported"
    assert "token" not in json.dumps(body).lower()
    # Support impersonation: read-only.
    support = _http(pg, monkeypatch, merchant.payload(role="support_impersonation", impersonation=True))
    assert support.get(f"{base}/status").status_code == 200
    assert support.post(f"{base}/start", json={"shop": fake.shop_domain}).status_code == 403
    # Another tenant cannot start this shop's import or see it.
    foreign = _http(pg, monkeypatch, other.payload())
    assert foreign.post(f"{base}/start", json={"shop": fake.shop_domain}).status_code == 404
    assert foreign.get(f"{base}/status").json()["connections"] == []
    assert len(jobs) == 1
    # Flag off again: 404.
    monkeypatch.setenv("NAHLA_SHOPIFY_CATALOG_IMPORT_ENABLED", "0")
    assert client.get(f"{base}/status").status_code == 404


def test_http_routes_fail_closed_without_revision_0122(monkeypatch):
    name = _fresh_db("shc_no0122")
    try:
        _build(name, catalog=False)
        eng = create_engine(_url(name), future=True)
        session_pg = SimpleNamespace(name=name, engine=eng, Session=sessionmaker(bind=eng, expire_on_commit=False))
        monkeypatch.delenv("REDIS_URL", raising=False)
        merchant = Merchant(session_pg, "noschema")
        client = _http(session_pg, monkeypatch, merchant.payload())
        base = "/merchant/integrations/shopify/catalog-import"
        status = client.get(f"{base}/status").json()
        assert status["available"] is False and status["reason"] == "storage_unavailable"
        resp = client.post(f"{base}/start", json={"shop": SHOES[0]})
        assert resp.status_code == 503 and resp.json()["detail"]["reason"] == "storage_unavailable"
        eng.dispose()
    finally:
        _drop(name)


# ── Admission: the actor is re-checked under locks inside the admitting transaction ─

@pytest.mark.parametrize("change", ["demoted", "deactivated", "reassigned", "tenant_inactive"])
def test_admission_refuses_an_actor_changed_after_revalidation(pg, change):
    merchant, elsewhere = Merchant(pg, f"adm-{change}"), Merchant(pg, f"adm2-{change}")
    fake = shoe_store()
    connect(pg, merchant, fake)
    actor = merchant.actor(pg)            # what the route revalidated a moment earlier
    statement = {
        "demoted": "UPDATE users SET role = 'viewer' WHERE id = :u",
        "deactivated": "UPDATE users SET is_active = false WHERE id = :u",
        "reassigned": "UPDATE users SET tenant_id = :o WHERE id = :u",
        "tenant_inactive": "UPDATE tenants SET is_active = false WHERE id = :t",
    }[change]
    _sql(pg, statement, u=merchant.user_id, o=elsewhere.tenant_id, t=merchant.tenant_id)
    with pg.Session() as db:
        with pytest.raises(store.StartRefused) as exc:
            store.begin_run(db, actor=actor, shop_domain=fake.shop_domain)
    assert exc.value.code == store.S_ACTOR_REVOKED
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_import_runs")[0]["n"] == 0


def test_a_concurrent_demotion_committed_first_refuses_the_start(pg):
    merchant = Merchant(pg, "adm-race1")
    fake = shoe_store()
    connect(pg, merchant, fake)
    actor = merchant.actor(pg)
    demotion = pg.engine.connect()
    tx = demotion.begin()
    demotion.execute(text("UPDATE users SET role = 'viewer' WHERE id = :u"), {"u": merchant.user_id})
    outcome = {}

    def start():
        try:
            with pg.Session() as db:
                outcome["result"] = store.begin_run(db, actor=actor, shop_domain=fake.shop_domain)
        except store.StartRefused as exc:
            outcome["result"] = exc.code

    thread = threading.Thread(target=start)
    thread.start()
    _wait_for_lock_waiters(pg, 1)         # the admission waits on the user row
    tx.commit()
    demotion.close()
    thread.join(30)
    assert outcome["result"] == store.S_ACTOR_REVOKED
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_import_runs")[0]["n"] == 0


def test_a_demotion_after_admission_waits_for_the_admitting_commit(pg):
    """The start holds the actor rows (FOR SHARE) while it waits on the
    connection; a demotion arriving then waits for the admission to commit:
    authority is judged once, atomically, at admission."""
    merchant = Merchant(pg, "adm-race2")
    fake = shoe_store()
    cid = connect(pg, merchant, fake)
    actor = merchant.actor(pg)
    holder = _hold(pg, "SELECT 1 FROM shopify_connections WHERE id = :i FOR UPDATE", {"i": cid}, 2.0)
    outcome = {}

    def start():
        with pg.Session() as db:
            outcome["handle"] = store.begin_run(db, actor=actor, shop_domain=fake.shop_domain)

    def demote():
        started = time.monotonic()
        _sql(pg, "UPDATE users SET role = 'viewer' WHERE id = :u", u=merchant.user_id)
        outcome["demotion_waited"] = time.monotonic() - started

    t_start = threading.Thread(target=start)
    t_start.start()
    _wait_for_lock_waiters(pg, 1)         # admission: actor rows held, waiting on the connection
    t_demote = threading.Thread(target=demote)
    t_demote.start()
    _wait_for_lock_waiters(pg, 2)         # the demotion waits on the admission's share lock
    holder.join(10)
    t_start.join(30)
    t_demote.join(30)
    assert isinstance(outcome.get("handle"), store.RunHandle)
    assert outcome["demotion_waited"] >= 0.5
    # After the demotion, no further admission for this actor.
    _sql(pg, "UPDATE shopify_catalog_import_runs SET status = 'failed', failure_reason = 'interrupted',"
             " lease_id = NULL, lease_expires_at = NULL, finished_at = now()")
    with pg.Session() as db:
        with pytest.raises(store.StartRefused) as exc:
            store.begin_run(db, actor=actor, shop_domain=fake.shop_domain)
    assert exc.value.code == store.S_ACTOR_REVOKED


# ── Availability never answers from evidence the committed run did not refresh ─

def _single_variant_store(*, tracked: bool):
    fake = FakeCatalogShop(*SHIRTS, scopes=("read_products", "read_inventory"))
    loc = fake.add_location("مستودع الرياض")
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90", "tracked": tracked,
                                                 **({"levels": {loc: 5}} if tracked else {})}])
    return fake


def _availability(pg, merchant, cid, fake):
    with pg.Session() as db:
        return store.variant_availability(db, tenant_id=merchant.tenant_id, connection_id=cid,
                                          variant_gid=gid("ProductVariant", next(iter(fake.variants))))


def test_a_newer_untracked_item_skipped_by_an_older_tracked_observation_is_stale_not_untracked(pg):
    merchant = Merchant(pg, "av1")
    fake = _single_variant_store(tracked=False)
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    import_once(pg, merchant, fake)
    assert _availability(pg, merchant, cid, fake)["state"] == "untracked"
    _sql(pg, "UPDATE shopify_catalog_inventory_items SET source_updated_at = now() + interval '1 year'")
    item = next(iter(fake.items))
    fake.items[item] = {"tracked": True, "updatedAt": "2026-10-01T07:30:00Z"}   # older than the stored row
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed" and run_row(pg, handle.run_id)["stale_skipped"] >= 1
    assert _dump(pg, "shopify_catalog_inventory_items")[0]["tracking"] == "untracked"   # not overwritten
    assert _availability(pg, merchant, cid, fake) == {"state": "unknown", "basis": "stale_observation",
                                                      "policy": "DENY"}


def test_a_stale_variant_with_an_untracked_item_is_stale_not_untracked(pg):
    merchant = Merchant(pg, "av2")
    fake = _single_variant_store(tracked=False)
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    import_once(pg, merchant, fake)
    _sql(pg, "UPDATE shopify_catalog_variants SET source_updated_at = now() + interval '1 year'")
    assert import_once(pg, merchant, fake)[1] == "committed"
    assert _availability(pg, merchant, cid, fake)["basis"] == "stale_observation"


def test_a_detached_item_is_never_a_definitive_answer(pg):
    merchant = Merchant(pg, "av3")
    fake = _single_variant_store(tracked=False)
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    import_once(pg, merchant, fake)
    _sql(pg, "UPDATE shopify_catalog_inventory_items SET lifecycle = 'detached', removed_at = now()")
    assert _availability(pg, merchant, cid, fake)["basis"] == "item_detached"


# ── A variant deleted between pages ───────────────────────────────────────────

def test_a_variant_deleted_between_pages_never_publishes_a_mixed_reading(pg, monkeypatch):
    monkeypatch.setattr(C, "NESTED_VARIANTS_PAGE", 1)
    monkeypatch.setattr(C, "FOLLOWUP_VARIANTS_PAGE", 2)
    merchant = Merchant(pg, "drift")
    fake = FakeCatalogShop(*SHOES)
    product = fake.add_product("حذاء رياضي أبيض", variants=[
        {"price": "199.00", "title": "40"}, {"price": "199.00", "title": "41"}, {"price": "199.00", "title": "42"}])
    connect(pg, merchant, fake)
    first, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    before = _mirror(pg)
    # Persistent drift: every follow-up page deletes the next first variant.
    def delete_first(f, v, i):
        remaining = sorted(n for n, x in f.variants.items() if x.product == product)
        if len(remaining) > 1:
            f.delete_variant(remaining[0])
            f.add_variant(product, price="199.00", title=f"new-{i}", position=9)

    fake.hooks["NahlaCatalogProductVariants"] = delete_first
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_SOURCE_CHANGED
    assert _mirror(pg) == before                                       # the old committed mirror survives
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_staged_records")[0]["n"] == 0
    # A single deletion between pages: the unit is re-read and the consistent reading commits.
    fake.hooks.clear()
    fake.calls.clear()
    doomed = sorted(n for n, x in fake.variants.items() if x.product == product)[0]
    fake.hooks["NahlaCatalogProductVariants"] = lambda f, v, i: f.delete_variant(doomed) if i == 0 else None
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    assert fake.ops("NahlaCatalogProducts") == 2
    present = {r["variant_gid"] for r in _dump(pg, "shopify_catalog_variants") if r["lifecycle"] == "present"}
    assert present == {gid("ProductVariant", n) for n, x in fake.variants.items() if x.product == product}
    assert run_row(pg, handle.run_id)["variants_count"] == len(present)


# ── Skipped stale rows keep their (newer) relationship graph ──────────────────

def test_a_stale_variant_keeps_its_newer_item_and_levels(pg):
    """The mirror's variant (newer) points to item B with a level; Shopify's
    older reading points the same variant to item A. The skipped variant must
    not be orphaned: B and B's level stay present (stale, never fresh)."""
    merchant = Merchant(pg, "graph")
    fake = FakeCatalogShop(*SHIRTS, scopes=("read_products", "read_inventory"))
    loc = fake.add_location("مستودع الرياض")
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90", "levels": {loc: 5}}])
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    first = import_once(pg, merchant, fake)[0]
    [variant_number] = list(fake.variants)
    item_b = fake.variants[variant_number].item
    _sql(pg, "UPDATE shopify_catalog_variants SET source_updated_at = now() + interval '1 year'")
    item_a = 990001
    fake.items[item_a] = {"tracked": True, "updatedAt": "2026-10-01T07:00:00Z"}
    fake.set_level(item_a, loc, 9)
    fake.variants[variant_number].item = item_a
    fake.items.pop(item_b)
    fake.levels.pop((item_b, loc))
    second, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed" and run_row(pg, second.run_id)["stale_skipped"] >= 1
    items = {r["inventory_item_gid"]: r for r in _dump(pg, "shopify_catalog_inventory_items")}
    b, a = items[gid("InventoryItem", item_b)], items[gid("InventoryItem", item_a)]
    [variant] = _dump(pg, "shopify_catalog_variants")
    assert variant["inventory_item_id"] == b["id"] and variant["last_seen_run_id"] == first.run_id
    assert b["lifecycle"] == "present" and a["lifecycle"] == "present"
    levels = {r["inventory_item_id"]: r for r in _dump(pg, "shopify_catalog_inventory_levels")}
    assert levels[b["id"]]["lifecycle"] == "present" and levels[b["id"]]["available"] == 5
    assert levels[a["id"]]["available"] == 9
    assert _availability(pg, merchant, cid, fake)["basis"] == "stale_observation"


def test_a_valid_json_partial_payload_cannot_clear_values_or_publish(pg):
    merchant = Merchant(pg, "omit")
    fake = shoe_store()
    connect(pg, merchant, fake)
    assert import_once(pg, merchant, fake)[1] == "committed"
    before = _mirror(pg)
    assert any(v["sku"] == "SNK-W-40" for v in before["shopify_catalog_variants"])
    original = fake._variant_node

    def without_sku(v, media):
        node = original(v, media)
        node.pop("sku")                        # omitted (not null), no GraphQL error
        return node

    fake._variant_node = without_sku
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and run_row(pg, handle.run_id)["failure_reason"] == E.R_INVALID_RESPONSE
    assert _mirror(pg) == before
    assert _sql(pg, "SELECT count(*) AS n FROM shopify_catalog_staged_records")[0]["n"] == 0


def test_one_file_shared_by_two_products_and_their_variants_commits(pg, monkeypatch):
    monkeypatch.setattr(C, "PRODUCTS_PAGE", 1)
    merchant = Merchant(pg, "files")
    fake = FakeCatalogShop(*SHOES)
    shoe = fake.add_product("حذاء رياضي أبيض", variants=[{"price": "199.00"}])
    shared = fake.products[shoe].media[0]
    perfume = fake.add_product("عطر ورد 100ml", images=0, variants=[{"price": "250.00"}, {"price": "260.00", "title": "x"}])
    fake.products[perfume].media = [shared]
    fake.products[perfume].featured = shared
    for v in fake.variants.values():
        v.media = [shared]
    connect(pg, merchant, fake)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed"
    url = fake.media[shared].url
    assert {r["image_url"] for r in _dump(pg, "shopify_catalog_products")} == {url}
    assert {(r["image_url"], r["image_source"]) for r in _dump(pg, "shopify_catalog_variants")} == {(url, "variant")}


# ── Read-only collision audit ─────────────────────────────────────────────────

def test_the_collision_audit_is_read_only_and_reports_counts_only(pg):
    import psycopg2

    sys.path.insert(0, str(_REPO / "scripts"))
    import audit_shopify_catalog_collisions_readonly as audit_module

    merchant = Merchant(pg, "audit")
    fake = shoe_store()
    connect(pg, merchant, fake)
    assert import_once(pg, merchant, fake)[1] == "committed"
    raw_product = sorted(fake.products)[0]
    _sql(pg, "INSERT INTO products (tenant_id, external_id, title, source, in_stock) VALUES (:t, :e, 'x', 'salla', true)",
         t=merchant.tenant_id, e=str(raw_product))
    _sql(pg, "INSERT INTO products (tenant_id, external_id, title, source, in_stock) VALUES (:t, 'legacy-1', 'y',"
             " 'shopify', true)", t=merchant.tenant_id)
    dsn = make_url(_url(pg.name)).set(drivername="postgresql").render_as_string(hide_password=False)
    before = _mirror(pg)
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            report = audit_module.audit(cur)
            with pytest.raises(psycopg2.Error):
                cur.execute("UPDATE products SET title = 'z'")   # the audit's transaction cannot write
        conn.rollback()
    finally:
        conn.close()
    checks = {c["check"]: c for c in report["checks"]}
    assert report["schema"] == {"foundation_0121": True, "catalog_0122": True, "connection_key": True,
                                "partial_catalog": False}
    assert report["verdict"] == "WARN"
    assert checks["legacy_hub.products_source_shopify_not_adopted"]["count"] == 1
    assert checks["raw_id_overlap.products_external_id"]["count"] == 1
    assert all(c["count"] == 0 for k, c in checks.items() if k.startswith("mirror_integrity."))
    assert _mirror(pg) == before
    assert _sql(pg, "SELECT count(*) AS n FROM products WHERE source = 'shopify'")[0]["n"] == 1   # never adopted
    # A Shopify GID in a Salla/generic identity column is a FAIL; the CLI never prints the DSN.
    _sql(pg, "UPDATE products SET external_id = 'gid://shopify/Product/1' WHERE external_id = 'legacy-1'")
    proc = subprocess.run([sys.executable, str(_REPO / "scripts" / "audit_shopify_catalog_collisions_readonly.py")],
                          env={**os.environ, "DATABASE_URL": dsn}, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert out["verdict"] == "FAIL" and pg.name not in proc.stdout and "postgresql://" not in proc.stdout


def test_a_retained_stale_graph_never_keeps_a_level_at_a_confirmed_deleted_location(pg):
    """The newer stored variant keeps item B (not staged this run); B's level
    sits at a location Shopify confirms gone. That level is detached with the
    location (no present level at a deleted location), while the rest of the
    retained B graph stays, availability stays unknown, and a failing run
    changes nothing."""
    merchant = Merchant(pg, "graphloc")
    fake = FakeCatalogShop(*SHIRTS, scopes=("read_products", "read_inventory"))
    old_loc = fake.add_location("مستودع قديم")
    kept_loc = fake.add_location("مستودع الرياض")
    fake.add_product("قميص قطني أزرق", variants=[{"price": "89.90", "levels": {old_loc: 5, kept_loc: 2}}])
    cid = connect(pg, merchant, fake, scopes="read_products,read_inventory")
    import_once(pg, merchant, fake)
    [variant_number] = list(fake.variants)
    item_b = fake.variants[variant_number].item
    _sql(pg, "UPDATE shopify_catalog_variants SET source_updated_at = now() + interval '1 year'")
    new_loc = fake.add_location("مستودع جدة")
    item_a = 990002
    fake.items[item_a] = {"tracked": True, "updatedAt": "2026-10-01T07:00:00Z"}
    fake.set_level(item_a, new_loc, 9)
    fake.variants[variant_number].item = item_a
    fake.items.pop(item_b)
    for key in [k for k in fake.levels if k[0] == item_b]:
        fake.levels.pop(key)
    fake.locations.pop(old_loc)                                      # confirmed gone by nodes(ids:)
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "committed", run_row(pg, handle.run_id)["failure_reason"]
    locations = {r["location_gid"]: r for r in _dump(pg, "shopify_catalog_locations")}
    assert locations[gid("Location", old_loc)]["lifecycle"] == "deleted"
    items = {r["inventory_item_gid"]: r for r in _dump(pg, "shopify_catalog_inventory_items")}
    b = items[gid("InventoryItem", item_b)]
    assert b["lifecycle"] == "present"                              # the newer variant's item survives
    levels = {(r["inventory_item_id"], r["location_id"]): r for r in _dump(pg, "shopify_catalog_inventory_levels")}
    at_old = levels[(b["id"], locations[gid("Location", old_loc)]["id"])]
    at_kept = levels[(b["id"], locations[gid("Location", kept_loc)]["id"])]
    assert at_old["lifecycle"] == "detached"                        # never present at a deleted location
    assert at_kept["lifecycle"] == "present" and at_kept["available"] == 2   # unconfirmed, retained
    assert not _sql(pg, "SELECT 1 FROM shopify_catalog_inventory_levels l JOIN shopify_catalog_locations c"
                        " ON c.id = l.location_id WHERE l.lifecycle = 'present' AND c.lifecycle <> 'present'")
    assert _availability(pg, merchant, cid, fake)["basis"] == "stale_observation"
    # A failing run afterwards still changes nothing.
    before = _mirror(pg)
    fake.tokens.clear()
    handle, outcome = import_once(pg, merchant, fake)
    assert outcome == "failed" and _mirror(pg) == before
