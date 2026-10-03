"""ق-5 read-only membership readout: SELECT-only, secret-free, schema preflight, identity parsing.

The PostgreSQL cases run only with WA_CATALOG_SYNC_PG_TEST_DATABASE_URL (isolated test DB);
they create the model tables in a scratch schema, run every statement for real, then drop
``catalog_channel_retirements`` and prove the readout records the gap instead of failing.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "operators" / "catalog_q5_membership_readout.py"
for _entry in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# Links in the shape of the actual ق-4 export: Salla dev-store pages with the id glued to
# ``p`` (``/p1207801870``), the ``/p/<id>`` form store_sync emits, and Nahla public pages.
Q4_LINKS = [
    "https://salla.sa/dev-cgcaqkpx5wgewsyv/p1207801870",
    "https://salla.sa/dev-cgcaqkpx5wgewsyv/p1207801871?utm_source=x",
    "https://dev-cgcaqkpx5wgewsyv.salla.sa/p/1207801872",
    "https://api.nahlah.ai/public/catalog/items/nahla_p_176",
    "https://api.nahlah.ai/public/catalog/items/nahla_p_182",
]


def _load():
    spec = importlib.util.spec_from_file_location("catalog_q5_membership_readout", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _statements(mod, **overrides):
    kwargs = dict(
        catalog_id="871742015873294",
        tenant_id=35,
        product_ids=[176, 177, 178, 179, 180, 181, 182],
        content_ids=["nahla_p_176", "1207801870"],
        link_external_ids=["1207801870"],
        meta_item_ids=["9900000000000001"],
        store_marker="dev-cgcaqkpx5wgewsyv",
    )
    kwargs.update(overrides)
    return mod.build_statements(**kwargs)


# ── identity parsing ───────────────────────────────────────────────────────


def test_salla_ids_from_real_q4_link_shapes_and_nahla_links_are_excluded():
    mod = _load()
    assert mod.external_ids_from_links(Q4_LINKS) == ["1207801870", "1207801871", "1207801872"]
    assert mod.nahla_public_ids_from_links(Q4_LINKS) == ["nahla_p_176", "nahla_p_182"]
    # A Nahla public link never yields a Salla id, even though its last segment is an identity.
    assert mod.external_ids_from_links(["https://api.nahlah.ai/public/catalog/items/nahla_p_176"]) == []
    # A non-numeric slug is not mistaken for a Salla id.
    assert mod.external_ids_from_links(["https://salla.sa/dev-cgcaqkpx5wgewsyv/about-us"]) == []
    markers = mod.store_markers_from_links(Q4_LINKS)
    assert markers == ["dev-cgcaqkpx5wgewsyv"], markers
    assert mod.parse_id_range("176-182,190") == [176, 177, 178, 179, 180, 181, 182, 190]


# ── statement guards ────────────────────────────────────────────────────────


def test_every_statement_is_a_select_and_passes_the_write_guard():
    mod = _load()
    sts = _statements(mod)
    assert len(sts) >= 16
    for st in sts:
        assert st["sql"].strip().upper().startswith("SELECT"), st["key"]
        mod._assert_select_only(st["sql"])
        assert st["requires"], st["key"]


def test_write_guard_refuses_non_select():
    mod = _load()
    with pytest.raises(RuntimeError):
        mod._assert_select_only("UPDATE products SET meta_item_id = NULL")
    with pytest.raises(RuntimeError):
        mod._assert_select_only("SELECT 1;\nDELETE FROM products")


def test_no_secret_bearing_column_is_selected():
    mod = _load()
    joined = "\n".join(st["sql"].lower() for st in _statements(mod))
    for forbidden in ("access_token", "token_type", "app_secret", "password", "secret_enc"):
        assert forbidden not in joined, forbidden
    for pattern in (r"\bi\.config\s*(,|$|\n)", r"\bts\.store_settings\s*(,|$|\n)",
                    r"(?<!coalesce\()(?<!then )\bp\.metadata\s*(,|$|\n)",
                    r"\bw\.extra_metadata\b", r"\bw\.access_token\b", r"\bmeta_import_last_report\b"):
        assert re.search(pattern, joined) is None, pattern
    assert "i.config ->> 'store_id'" in joined
    assert "ts.store_settings ->> 'store_url'" in joined


def test_render_refuses_secret_shapes():
    mod = _load()
    with pytest.raises(RuntimeError):
        mod.render({"x": "EAAtoken"}, pretty=False)
    with pytest.raises(RuntimeError):
        mod.render({"x": "postgresql://u:p@h/db"}, pretty=False)
    assert "ok" in mod.render({"x": "ok"}, pretty=False)


def test_content_id_and_meta_item_id_are_matched_separately():
    mod = _load()
    sts = {s["key"]: s for s in _statements(mod, content_ids=["A-1"], meta_item_ids=["99"])}
    assert sts["memberships_matching_q4_content_ids"]["params"]["content_ids"] == ["A-1"]
    assert "m.retailer_id = any(%(content_ids)s)" in sts["memberships_matching_q4_content_ids"]["sql"].lower()
    assert sts["memberships_matching_q4_meta_item_ids"]["params"]["meta_item_ids"] == ["99"]
    assert "m.meta_item_id = any(%(meta_item_ids)s)" in sts["memberships_matching_q4_meta_item_ids"]["sql"].lower()
    assert sts["products_stamped_with_export_meta_item_ids"]["params"]["meta_item_ids"] == ["99"]
    # nahla_p_<id> identities derive from the local product ids, never from the Content IDs.
    assert sts["claims_on_nahla_identities"]["params"]["nahla_ids"][0] == "nahla_p_176"


# ── schema preflight ────────────────────────────────────────────────────────


def _full_schema(mod):
    from database.models import Base  # noqa: PLC0415

    schema = {}
    for table in mod.ALL_TABLES:
        if table in Base.metadata.tables:
            schema[table] = set(Base.metadata.tables[table].columns.keys())
    schema["alembic_version"] = {"version_num"}
    return schema


def test_missing_retirements_table_is_skipped_and_recorded_not_fatal():
    mod = _load()
    schema = _full_schema(mod)
    schema.pop("catalog_channel_retirements")
    sts = _statements(mod, schema=schema)
    runnable, skipped = mod.plan_statements(sts, schema)
    assert skipped == {"retirements_for_catalog": "table_missing:catalog_channel_retirements"}
    assert "retirements_for_catalog" not in {s["key"] for s in runnable}
    assert "catalog_channel_retirements" not in "\n".join(s["sql"] for s in runnable)
    assert len(runnable) == len(sts) - 1


def test_columns_added_after_the_pinned_migration_are_dropped_not_fatal():
    mod = _load()
    schema = _full_schema(mod)
    schema["products"] -= {"archived_at", "managed_confirmed_at", "managed_confirmed_by"}
    schema["meta_catalog_memberships"] -= {"salla_variant_id", "variant_id"}
    schema["tenants"] -= {"is_platform_tenant"}
    sts = {s["key"]: s for s in _statements(mod, schema=schema)}
    runnable, skipped = mod.plan_statements(list(sts.values()), schema)
    assert skipped == {}
    assert "archived_at" not in sts["products_by_id"]["sql"]
    assert set(sts["products_by_id"]["optional_missing"]["products"]) >= {"managed_confirmed_at", "managed_confirmed_by", "archived_at"}
    assert "salla_variant_id" not in sts["memberships_for_catalog"]["sql"]
    assert "is_platform_tenant" not in sts["tenants_involved"]["sql"]
    assert "archived" not in sts["product_stamp_indicator_by_tenant"]["sql"]
    # The claims query still runs with a NULL placeholder for the dropped column.
    assert "NULL::timestamptz AS archived_at" in sts["claims_on_nahla_identities"]["sql"]


def test_runner_records_the_missing_retirements_table_with_a_fake_connection():
    mod = _load()
    full = _full_schema(mod)
    full.pop("catalog_channel_retirements")
    executed = []

    class FakeCursor:
        def __init__(self):
            self._rows = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append(sql.strip())
            if sql is mod.SCHEMA_PROBE_SQL:
                self._rows = [{"table_name": t, "column_name": c} for t, cols in full.items() for c in sorted(cols)]
            elif sql is mod.ALEMBIC_SQL:
                self._rows = [{"version_num": "0093"}]
            else:
                self._rows = []

        def fetchall(self):
            return self._rows

    class FakeConn:
        closed = False
        rolled_back = False

        def cursor(self):
            return FakeCursor()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    conn = FakeConn()

    def build(schema):
        return _statements(mod, schema=schema)

    out = mod.run(build, "postgresql://unused", connect=lambda url: conn)
    assert out["read_only"] is True and out["secrets_included"] is False
    assert out["schema_preflight"]["tables_missing"] == ["catalog_channel_retirements"]
    assert out["schema_preflight"]["alembic_version"] == ["0093"]
    assert out["schema_preflight"]["nothing_created_or_migrated"] is True
    assert out["skipped"] == {"retirements_for_catalog": "table_missing:catalog_channel_retirements"}
    assert "retirements_for_catalog" not in out["results"]
    assert "memberships_for_catalog" in out["results"] and "store_marker_hits" in out["results"]
    assert conn.rolled_back and conn.closed
    assert executed[0].startswith("SET default_transaction_read_only = on")
    data_statements = [s for s in executed[2:] if not s.startswith("SELECT table_name") and s != mod.ALEMBIC_SQL.strip()]
    assert all(s.upper().startswith("SELECT") for s in data_statements)
    assert all("catalog_channel_retirements" not in s for s in data_statements)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_print_sql_and_require_inputs(tmp_path):
    cids = tmp_path / "cids.txt"
    cids.write_text("nahla_p_176\n# comment\n1207801870\n", encoding="utf-8")
    links = tmp_path / "links.txt"
    links.write_text("\n".join(Q4_LINKS) + "\n", encoding="utf-8")
    mids = tmp_path / "mids.txt"
    mids.write_text("9900000000000001\n", encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin"}
    res = subprocess.run(
        [sys.executable, str(SCRIPT), "--print-sql", "--content-ids-file", str(cids), "--links-file", str(links),
         "--meta-item-ids-file", str(mids), "--require-inputs"],
        capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["inputs"]["store_marker"] == "dev-cgcaqkpx5wgewsyv"
    assert payload["inputs"]["link_external_ids"] == ["1207801870", "1207801871", "1207801872"]
    assert payload["inputs"]["nahla_public_ids_from_links"] == ["nahla_p_176", "nahla_p_182"]
    assert payload["inputs"]["salla_link_count"] == 3 and payload["inputs"]["nahla_public_link_count"] == 2
    assert payload["inputs"]["meta_item_ids_provided"] is True
    assert all(s["sql"].upper().startswith("SELECT") for s in payload["statements"])

    # --require-inputs refuses to run without the Meta item id file.
    res_missing = subprocess.run(
        [sys.executable, str(SCRIPT), "--print-sql", "--content-ids-file", str(cids), "--links-file", str(links), "--require-inputs"],
        capture_output=True, text=True, env=env,
    )
    assert res_missing.returncode == 2 and "meta-item-ids-file" in res_missing.stderr

    res2 = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
    assert res2.returncode == 2 and "DATABASE_URL is not set" in res2.stderr
    assert subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True).returncode == 0


# ── real PostgreSQL (isolated test database only) ──────────────────────────


def _pg_url():
    url = (os.getenv("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL") or "").strip()
    if not url.startswith(("postgresql://", "postgresql+psycopg2://")):
        if (os.getenv("WA_CATALOG_SYNC_PG_REQUIRED") or "").strip() == "1":
            pytest.fail("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL is required")
        pytest.skip("WA_CATALOG_SYNC_PG_TEST_DATABASE_URL not set; isolated PostgreSQL case skipped")
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


@pytest.fixture
def q5_pg_schema():
    """A scratch schema holding the model tables the readout touches (created, then dropped)."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.pool import NullPool  # noqa: PLC0415

    url = _pg_url()
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
