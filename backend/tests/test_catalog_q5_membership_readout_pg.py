"""ق-5 read-only membership readout on real PostgreSQL.

Split from ``tests/test_catalog_q5_membership_readout.py`` (unit cases) so the
strict required PostgreSQL proofs run exactly these cases: the model tables are
created in a scratch schema of a throw-away database on the admin DSN in
``NAHLA_RELIABILITY_PG_ADMIN_DSN``, every statement runs for real, then
``catalog_channel_retirements`` is dropped and the readout must record the gap
instead of failing. The database is dropped at teardown. Without the variable
the module is skipped (reported as skipped, never as passed); with
``NAHLA_RELIABILITY_REQUIRE_PG=1`` and no variable it fails.
"""
from __future__ import annotations

import importlib.util
import os
import secrets
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "operators" / "catalog_q5_membership_readout.py"
for _entry in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

_DB_PREFIX = "q5_readout_proof"


def _load():
    spec = importlib.util.spec_from_file_location("catalog_q5_membership_readout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

ADMIN_URL = (os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") or "").strip()
if not ADMIN_URL and os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
    raise RuntimeError("NAHLA_RELIABILITY_REQUIRE_PG=1 but NAHLA_RELIABILITY_PG_ADMIN_DSN is not set")
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="NAHLA_RELIABILITY_PG_ADMIN_DSN not set (no real PostgreSQL)")


def _admin(statement: str) -> None:
    from sqlalchemy import create_engine as _ce, text as _text  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    eng = _ce(ADMIN_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as conn:
            conn.execute(_text(statement))
    finally:
        eng.dispose()


@pytest.fixture
def throwaway_pg_url():
    """A throw-away database on the admin DSN's server, dropped at teardown."""
    from sqlalchemy.engine.url import make_url  # noqa: PLC0415

    name = f"{_DB_PREFIX}_{secrets.token_hex(4)}"
    _admin(f'CREATE DATABASE "{name}"')
    try:
        yield make_url(ADMIN_URL).set(drivername="postgresql", database=name).render_as_string(hide_password=False)
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


# ── real PostgreSQL (isolated test database only) ──────────────────────────


@pytest.fixture
def q5_pg_schema(throwaway_pg_url):
    """A scratch schema holding the model tables the readout touches (created, then dropped)."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    url = throwaway_pg_url
    schema_name = f"q5_readout_itest_{os.getpid()}"
    engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    scoped = create_engine(url, poolclass=NullPool, connect_args={"options": f"-c search_path={schema_name}"})
    from database.models import (  # noqa: PLC0415
        Base, CatalogChannelRetirement, Integration, MetaCatalogMembership, Product, ProductVariant,
        StoreKnowledgeSnapshot, Tenant, TenantSettings, WhatsAppConnection,
    )
    tables = [Tenant, TenantSettings, Product, ProductVariant, MetaCatalogMembership, CatalogChannelRetirement,
              Integration, StoreKnowledgeSnapshot, WhatsAppConnection]
    Base.metadata.create_all(scoped, tables=[t.__table__ for t in tables])
    with scoped.begin() as conn:
        # Other test modules may have rendered JSONB columns as JSON on the shared
        # metadata; production columns are jsonb, so make the scratch schema match.
        for table, column in (("products", "metadata"), ("tenant_settings", "store_settings"),
                              ("store_knowledge_snapshots", "store_profile"), ("integrations", "config")):
            conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN "{column}" TYPE jsonb USING "{column}"::jsonb'))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('0093')"))
        conn.execute(text("INSERT INTO tenants (id, name, domain, is_active, is_platform_tenant) VALUES (35, 'q5 trial tenant', 'q5-trial.test', true, false)"))
        conn.execute(text("INSERT INTO tenants (id, name, domain, is_active, is_platform_tenant) VALUES (7, 'q5 other tenant', 'q5-other.test', true, false)"))
        conn.execute(text(
            "INSERT INTO tenant_settings (tenant_id, show_nahla_branding, branding_text, store_settings) VALUES "
            "(7, true, 'b', '{\"store_url\": \"https://salla.sa/dev-cgcaqkpx5wgewsyv\", \"salla_store_info\": {\"store_id\": \"555\", \"name\": \"dev store\"}}'::jsonb), "
            "(35, true, 'b', '{\"store_url\": \"https://salla.sa/current-store\"}'::jsonb)"))
        conn.execute(text(
            "INSERT INTO products (id, tenant_id, title, external_id, source, meta_item_id, meta_retailer_id, in_stock, has_variants, metadata) VALUES "
            "(176, 7, 'blouse', NULL, 'nahla_native', '9900000000000001', NULL, true, false, '{\"product_url\": \"https://api.nahlah.ai/public/catalog/items/nahla_p_176\", \"sync_meta\": {\"a\": 1}}'::jsonb), "
            "(500, 7, 'dress', '1207801870', 'salla', NULL, NULL, true, false, '{\"product_url\": \"https://salla.sa/dev-cgcaqkpx5wgewsyv/p1207801870\"}'::jsonb), "
            "(185, 35, 'jacket', '617350990', 'salla', NULL, NULL, true, true, NULL)"))
        conn.execute(text(
            "INSERT INTO product_variants (id, tenant_id, product_id, salla_variant_id, retailer_id, in_stock, is_default) VALUES "
            "(1, 35, 185, '111', '617350990-111', true, true)"))
        conn.execute(text(
            "INSERT INTO meta_catalog_memberships (tenant_id, catalog_id, retailer_id, product_id, meta_item_id, verified_at, provenance) VALUES "
            "(7, '871742015873294', 'nahla_p_176', 176, '9900000000000001', now(), 'meta_graph_reconcile')"))
        conn.execute(text(
            "INSERT INTO integrations (tenant_id, provider, external_store_id, config, enabled) VALUES "
            "(7, 'salla', '555', '{\"store_id\": \"555\", \"store_name\": \"dev store\", \"access_token\": \"EAA-must-never-be-selected\"}'::jsonb, true)"))
        conn.execute(text(
            "INSERT INTO whatsapp_connections (tenant_id, status, provider, meta_catalog_id) VALUES (35, 'connected', 'meta', NULL)"))
    yield {"url": url, "schema": schema_name, "engine": scoped}
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))


def _connect_scoped(mod, schema_name):
    import psycopg2  # noqa: PLC0415
    import psycopg2.extras  # noqa: PLC0415

    def connect(url):
        conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor, options=f"-c search_path={schema_name}")
        conn.set_session(readonly=True, autocommit=False)
        return conn

    return connect


def test_postgres_every_statement_executes_on_the_model_schema_and_tokens_never_appear(q5_pg_schema):
    mod = _load()

    def build(schema):
        return mod.build_statements(
            catalog_id="871742015873294", tenant_id=35, product_ids=[176, 177, 178, 179, 180, 181, 182],
            content_ids=["nahla_p_176", "1207801870"], link_external_ids=["1207801870"],
            meta_item_ids=["9900000000000001"], store_marker="dev-cgcaqkpx5wgewsyv", schema=schema,
        )

    out = mod.run(build, q5_pg_schema["url"], connect=_connect_scoped(mod, q5_pg_schema["schema"]))
    assert out["skipped"] == {}
    assert out["schema_preflight"]["tables_missing"] == []
    assert out["schema_preflight"]["alembic_version"] == ["0093"]
    res = out["results"]
    assert len(res) == 17
    assert res["memberships_for_catalog"]["rows"][0]["tenant_id"] == 7
    assert res["memberships_for_catalog"]["rows"][0]["provenance"] == "meta_graph_reconcile"
    assert res["memberships_matching_q4_meta_item_ids"]["row_count"] == 1
    assert res["products_by_id"]["rows"][0]["tenant_id"] == 7
    assert res["products_by_id"]["rows"][0]["sync_meta_keys"] == ["a"]
    assert res["products_matching_link_external_ids"]["rows"][0]["external_id"] == "1207801870"
    assert res["products_stamped_with_export_meta_item_ids"]["rows"][0]["id"] == 176
    hits = {(r["place"], r["tenant_id"]) for r in res["store_marker_hits"]["rows"]}
    assert ("tenant_settings", 7) in hits and ("products.metadata urls", 7) in hits
    assert {r["id"] for r in res["tenants_involved"]["rows"]} == {7, 35}
    assert res["whatsapp_connection_catalog_stamps"]["rows"][0]["tenant_id"] == 35
    text = mod.render(out, pretty=True)
    assert "EAA" not in text and "must-never-be-selected" not in text


def test_postgres_missing_retirements_table_is_recorded_and_the_rest_still_runs(q5_pg_schema):
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    mod = _load()
    with q5_pg_schema["engine"].begin() as conn:
        conn.execute(sa_text("DROP TABLE catalog_channel_retirements"))

    def build(schema):
        return mod.build_statements(
            catalog_id="871742015873294", tenant_id=35, product_ids=[176], content_ids=["nahla_p_176"],
            link_external_ids=[], meta_item_ids=["9900000000000001"], store_marker="dev-cgcaqkpx5wgewsyv", schema=schema,
        )

    out = mod.run(build, q5_pg_schema["url"], connect=_connect_scoped(mod, q5_pg_schema["schema"]))
    assert out["schema_preflight"]["tables_missing"] == ["catalog_channel_retirements"]
    assert out["skipped"] == {"retirements_for_catalog": "table_missing:catalog_channel_retirements"}
    assert "retirements_for_catalog" not in out["results"]
    assert len(out["results"]) == 16
    # Read-only session: the table was not re-created by the readout.
    with q5_pg_schema["engine"].connect() as conn:
        exists = conn.execute(sa_text("SELECT to_regclass('catalog_channel_retirements')")).scalar()
    assert exists is None
