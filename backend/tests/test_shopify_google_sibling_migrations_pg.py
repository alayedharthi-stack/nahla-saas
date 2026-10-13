"""Cross-Draft migration proofs on disposable PostgreSQL, never a deploy gate.

CI supplies the exact Google Draft migration as an external, SHA256-pinned file
using NAHLA_GOOGLE_MERCHANT_MIGRATION_FIXTURE and
NAHLA_GOOGLE_MERCHANT_MIGRATION_SHA256. It is copied into a temporary database
tree; no Google branch is merged and no application or Google runtime is loaded.
The normal Alembic environment and existing migration metadata imports remain.

NAHLA_RELIABILITY_PG_ADMIN_DSN must name the disposable test server. There is no
DATABASE_URL fallback. NAHLA_SHOPIFY_GOOGLE_SIBLING_REQUIRED=1 makes missing
inputs fail instead of skip; ordinary test collection without this optional
cross-PR fixture reports skips. Only randomly named databases created here are
modified or removed. No revision is stamped or version row fabricated.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from backend.tests import _alembic_topology_probe as topo
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as contract
from scripts.operators.bootstrap_migration_contract import build_normal_bootstrap_upgrade_argv


REPO = Path(__file__).resolve().parents[2]
GOOGLE_FILE = "0125_google_merchant_ledger.py"
VERSION_DIR = Path("database/migrations/versions")
SOURCE_HEADS = frozenset({"0092", "0111", "0114", "0115", "0124", "0125"})
S3_ROWS = frozenset({"0111", "0113", "0123"})
BOTH_ROWS = frozenset({"0111", "0113", "0124", "0125"})
NEW_TABLES = {
    "0124": ("shopify_owner_identities", "shopify_install_flows"),
    "0125": ("google_merchant_connections", "google_merchant_offer_ledger"),
}
GOOGLE_FUNCTIONS = ("google_merchant_connections_guard", "google_merchant_offer_ledger_guard")
REQUIRED = os.environ.get("NAHLA_SHOPIFY_GOOGLE_SIBLING_REQUIRED") == "1"


def _missing(message: str) -> None:
    if REQUIRED:
        pytest.fail(message)
    pytest.skip(message)


def _revision_bytes(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (root / VERSION_DIR).glob("*.py")}


def _assert_combined_graph(root: Path) -> None:
    preflight = gate.preflight_migration_files(root)
    assert preflight.status == contract.PASS, preflight.detail
    graph = gate.load_graph(root)
    assert graph["heads"] == SOURCE_HEADS
    assert graph["down"]["0124"] == graph["down"]["0125"] == ("0123",)
    original = gate.load_graph(REPO)
    assert graph["known"] == original["known"] | {"0125"}
    assert {key: value for key, value in graph["down"].items() if key != "0125"} == original["down"]
    assert preflight.detail["revisions"] == len(graph["known"])


@pytest.fixture(scope="module")
def combined_root(tmp_path_factory):
    source = os.environ.get("NAHLA_GOOGLE_MERCHANT_MIGRATION_FIXTURE", "")
    expected = os.environ.get("NAHLA_GOOGLE_MERCHANT_MIGRATION_SHA256", "")
    if not source and not expected:
        _missing("Pinned external Google migration fixture is not configured")
    assert source and re.fullmatch(r"[0-9a-f]{64}", expected), "Both fixture path and SHA256 are required"
    path = Path(source).resolve()
    assert path.is_file(), "External Google migration fixture is missing"
    assert not path.is_relative_to((REPO / VERSION_DIR).resolve()), "Fixture must remain outside the source migration tree"
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == expected, "Google migration fixture SHA256 mismatch"
    root = tmp_path_factory.mktemp("sibling_migration_source")
    shutil.copytree(REPO / "database", root / "database", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    before = _revision_bytes(REPO)
    assert GOOGLE_FILE not in before, "Google revision must be supplied externally, not merged"
    (root / VERSION_DIR / GOOGLE_FILE).write_bytes(payload)
    after = _revision_bytes(root)
    assert set(after) == set(before) | {GOOGLE_FILE}
    assert {name: after[name] for name in before} == before, "Existing migration bytes changed"
    _assert_combined_graph(root)
    return root


def test_combined_graph_has_exact_sibling_heads_and_remains_outside_release_gate(combined_root):
    _assert_combined_graph(combined_root)
    # A proof of future DDL compatibility must not widen S0/S3 production gates.
    assert set(contract.DEPLOY_PHASES) == {"S0", "S3"}
    for phase in contract.DEPLOY_PHASES.values():
        check, _ = gate.check_graph(combined_root, phase)
        assert (check.status, check.reason) == (contract.FAIL, contract.R_GRAPH_HEADS_MISMATCH)


@pytest.mark.parametrize("mutation", ("filename_only_rename", "wrong_parent", "extra_head"))
def test_combined_graph_rejects_collision_dependency_and_extra_head(combined_root, tmp_path, mutation):
    root = tmp_path / "mutated"
    shutil.copytree(combined_root, root)
    google = root / VERSION_DIR / GOOGLE_FILE
    source = google.read_text(encoding="utf-8")
    if mutation == "filename_only_rename":
        assert 'revision = "0125"' in source
        google.write_text(source.replace('revision = "0125"', 'revision = "0124"', 1), encoding="utf-8")
        result = gate.preflight_migration_files(root)
        assert result.status == contract.FAIL
        assert result.detail["duplicate_count"] == 1
        assert google.name == GOOGLE_FILE  # A new filename does not fix an old revision identifier.
    elif mutation == "wrong_parent":
        assert 'down_revision = "0123"' in source
        google.write_text(source.replace('down_revision = "0123"', 'down_revision = "0124"', 1), encoding="utf-8")
    else:
        (root / VERSION_DIR / "9125_unapproved_head.py").write_text(
            'revision = "9125"\ndown_revision = "0123"\nbranch_labels = None\ndepends_on = None\n',
            encoding="utf-8",
        )
    with pytest.raises(AssertionError):
        _assert_combined_graph(root)


@pytest.fixture(scope="module")
def admin_url():
    url = os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN", "").strip()
    if not url:
        _missing("NAHLA_RELIABILITY_PG_ADMIN_DSN is not set (no disposable PostgreSQL)")
    assert make_url(url).get_backend_name() == "postgresql", "Proof requires PostgreSQL"
    return url


def _admin(admin_url: str, statement: str) -> None:
    engine = create_engine(admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text(statement))
    finally:
        engine.dispose()


def _database_url(admin_url: str, name: str) -> str:
    assert re.fullmatch(r"sibling_[0-9a-f]{24}", name)
    return make_url(admin_url).set(database=name).render_as_string(hide_password=False)


def _new_database(admin_url: str, template: str | None = None) -> str:
    name = "sibling_" + secrets.token_hex(12)
    statement = f'CREATE DATABASE "{name}"'
    if template is not None:
        assert re.fullmatch(r"sibling_[0-9a-f]{24}", template)
        statement += f' TEMPLATE "{template}"'
    _admin(admin_url, statement)
    return name


def _drop_database(admin_url: str, name: str) -> None:
    assert re.fullmatch(r"sibling_[0-9a-f]{24}", name)
    _admin(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)')


def _sql(url: str, *statements: str):
    engine = create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            result = None
            for statement in statements:
                result = conn.execute(text(statement))
            return result.fetchall() if result is not None and result.returns_rows else None
    finally:
        engine.dispose()


def _alembic(root: Path, url: str, *args: str, bootstrap: bool = False) -> None:
    env = dict(os.environ)
    for variable in contract.LIBPQ_TARGET_ENV_VARS:
        env.pop(variable, None)
    env["DATABASE_URL"] = url
    env["PYTHONPATH"] = os.pathsep.join((str(REPO), str(REPO / "backend"), str(root / "database")))
    command = build_normal_bootstrap_upgrade_argv(python_executable=sys.executable) if bootstrap else [sys.executable, "-m", "alembic", *args]
    result = subprocess.run(command, cwd=root / "database", env=env, capture_output=True, text=True, timeout=600)
    diagnostic = result.stderr.replace(url, "<disposable-database>")
    password = make_url(url).password
    if password:
        diagnostic = diagnostic.replace(password, "<redacted>")
    assert result.returncode == 0, diagnostic[-3000:]


@pytest.fixture(scope="module")
def s3_template(combined_root, admin_url):
    name = _new_database(admin_url)
    url = _database_url(admin_url, name)
    try:
        for target in ("0093", "0102"):
            _alembic(combined_root, url, "upgrade", target)
        # Genuine 0103 adoption of the historic nonce shape, as in the release
        # gate proofs. Neither this fixture nor any proof edits version rows.
        _sql(url, """CREATE TABLE whatsapp_oauth_nonces (
            id SERIAL PRIMARY KEY, nonce_hash VARCHAR(64) NOT NULL,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            connection_mode VARCHAR(32) NOT NULL, redirect_uri_fingerprint VARCHAR(64) NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ NULL,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_whatsapp_oauth_nonces_hash UNIQUE (nonce_hash))""",
             "CREATE INDEX ix_whatsapp_oauth_nonces_expires_at ON whatsapp_oauth_nonces (expires_at)",
             "CREATE INDEX ix_whatsapp_oauth_nonces_tenant_id ON whatsapp_oauth_nonces (tenant_id)")
        nonce_oid = _sql(url, "SELECT 'whatsapp_oauth_nonces'::regclass::oid")
        for target in ("0103", "0111", "0113", "0123"):
            _alembic(combined_root, url, "upgrade", target)
        topo.assert_rows(url, S3_ROWS, "genuine S3 template")
        assert _sql(url, "SELECT 'whatsapp_oauth_nonces'::regclass::oid") == nonce_oid
        yield name
    finally:
        _drop_database(admin_url, name)


def _snapshot(url: str, exclude_tables=(), exclude_functions=()) -> dict:
    tables = topo.schema_snapshot(url, exclude_prefixes=(), exclude=("alembic_version", *exclude_tables))
    return {
        "tables": tables,
        "oids": [row for row in _sql(url, "SELECT c.relname, c.oid FROM pg_class c JOIN pg_namespace n "
                     "ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r','p') ORDER BY c.relname")
                 if row[0] in tables],
        "triggers": [row for row in _sql(url, "SELECT c.relname, t.tgname, t.tgenabled, pg_get_triggerdef(t.oid) "
                         "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
                         "WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname, t.tgname") if row[0] in tables],
        "functions": [row for row in _sql(url, "SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid) "
                          "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' "
                          "AND p.prokind IN ('f','p') ORDER BY p.proname, p.oid") if row[0] not in exclude_functions],
    }


def _assert_unchanged(url, baseline, planted, counts, expected_rows, applied):
    topo.assert_rows(url, expected_rows, "sibling step")
    new_tables = (*NEW_TABLES["0124"], *NEW_TABLES["0125"])
    assert _snapshot(url, new_tables, GOOGLE_FUNCTIONS) == baseline, "Retained S3 schema or relation identity changed"
    topo.assert_sentinels(url, planted, "sibling step")
    assert topo.row_counts(url, topo.SENTINEL_TABLES) == counts
    actual_tables = set(topo.schema_snapshot(url, exclude_prefixes=()))
    assert actual_tables == set(baseline["tables"]) | {table for rev in applied for table in NEW_TABLES[rev]}


def _seed_surviving_sibling(url: str, revision: str, tenant_id: int) -> tuple[str, str]:
    if revision == "0125":
        _sql(url, "INSERT INTO google_merchant_connections "
             "(tenant_id, account_id, content_language, feed_label, status) "
             f"VALUES ({int(tenant_id)}, '7100000042', 'en', 'US', 'authorized')")
        table = "google_merchant_connections"
    else:
        _sql(url, "INSERT INTO shopify_install_flows "
             "(id, state_hash, browser_hash, shop_domain, redirect_hash, phase, generation, expires_at, created_at) "
             "VALUES ('synthetic-flow', 'synthetic-state', 'synthetic-browser', 'neutral-shoes.myshopify.com', "
             "'synthetic-redirect', 'online', 1, now() + interval '5 minutes', now())")
        table = "shopify_install_flows"
    return table, _sql(url, f"SELECT encode(sha256(convert_to(CAST(t AS text), 'UTF8')), 'hex') FROM {table} t")[0][0]


@pytest.mark.parametrize("order", (("0124", "0125"), ("0125", "0124")), ids=("shopify_then_google", "google_then_shopify"))
@pytest.mark.parametrize("remove_first", ("0124", "0125"), ids=("downgrade_shopify", "downgrade_google"))
def test_sibling_upgrade_orders_bootstrap_and_branch_preserving_downgrade(combined_root, admin_url, s3_template, order, remove_first):
    name = _new_database(admin_url, s3_template)
    url = _database_url(admin_url, name)
    try:
        baseline = _snapshot(url)
        assert len([name for name in baseline["tables"] if name.startswith("shopify_")]) == 15
        planted = topo.plant_sentinels(url)
        counts = topo.row_counts(url, topo.SENTINEL_TABLES)
        applied = set()
        for revision in order:
            _alembic(combined_root, url, "upgrade", revision)
            applied.add(revision)
            _assert_unchanged(url, baseline, planted, counts, frozenset({"0111", "0113"} | applied), applied)
        before_bootstrap = _snapshot(url)
        _alembic(combined_root, url, bootstrap=True)
        assert _snapshot(url) == before_bootstrap
        _assert_unchanged(url, baseline, planted, counts, BOTH_ROWS, applied)

        survivor = next(revision for revision in order if revision != remove_first)
        table, row_digest = _seed_surviving_sibling(url, survivor, planted["tenants"][0])
        excluded_functions = GOOGLE_FUNCTIONS if remove_first == "0125" else ()
        survivor_schema = _snapshot(url, NEW_TABLES[remove_first], excluded_functions)
        _alembic(combined_root, url, "downgrade", f"{remove_first}@-1")
        applied.remove(remove_first)
        _assert_unchanged(url, baseline, planted, counts, frozenset({"0111", "0113", survivor}), applied)
        assert _snapshot(url, NEW_TABLES[remove_first], excluded_functions) == survivor_schema
        assert _sql(url, f"SELECT encode(sha256(convert_to(CAST(t AS text), 'UTF8')), 'hex') FROM {table} t") == [(row_digest,)]

        _alembic(combined_root, url, "upgrade", remove_first)
        applied.add(remove_first)
        _assert_unchanged(url, baseline, planted, counts, BOTH_ROWS, applied)
        assert _snapshot(url, NEW_TABLES[remove_first], excluded_functions) == survivor_schema
        assert _sql(url, f"SELECT encode(sha256(convert_to(CAST(t AS text), 'UTF8')), 'hex') FROM {table} t") == [(row_digest,)]
    finally:
        _drop_database(admin_url, name)
