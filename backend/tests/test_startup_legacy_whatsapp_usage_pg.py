"""Minimal legacy-column repair and baseline-versus-candidate startup proof.

Only disposable PG16/18 targets. Real SQL/source loop and real source startup
DB callbacks; all unrelated provider coroutines remain withheld by the existing
test-only probe. The accepted baseline is unchanged and its failures reproduced.
"""
from __future__ import annotations

import ast
import copy
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.pool import NullPool

from backend.tests import _alembic_topology_probe as topo
from backend.tests import _shopify_release_gate_harness as gate_harness
from backend.tests import test_shopify_release_gate_pg as pg
from backend.tests import test_shopify_startup_compatibility_pg as prior
from scripts.operators import shopify_release_gate_contract as contract

CANDIDATE = prior.CANDIDATE
BASELINE = Path(os.environ.get("NAHLA_BASELINE_STARTUP_ROOT", "/missing-baseline"))
LEGACY = prior.LEGACY
ALTERS = (
    "ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used SET DEFAULT 0",
    "ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used DROP NOT NULL",
)


def _source(root: Path):
    parsed = ast.parse((root / "backend/main.py").read_text(encoding="utf-8"))
    migration = next(n for n in ast.walk(parsed) if isinstance(n, ast.FunctionDef) and n.name == "_run_migrations")
    values = next(n.value for n in migration.body if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "safe_alters" for t in n.targets))
    statements = ast.literal_eval(values)
    loop = next(n for n in migration.body if isinstance(n, ast.For)
                and isinstance(n.iter, ast.Name) and n.iter.id == "safe_alters")
    assert len(statements) == 64 and tuple(s for s in statements if s in ALTERS) == ALTERS
    return parsed, loop


def _assert_only_column_guard_changed():
    baseline, _ = _source(BASELINE)
    candidate, loop = _source(CANDIDATE)
    transaction = loop.body[0].body[0]
    assert isinstance(transaction, ast.With) and len(transaction.body) == 2
    assert isinstance(transaction.body[0], ast.If)
    assert len(transaction.body[0].body) == 1 and isinstance(transaction.body[0].body[0], ast.Continue)
    del transaction.body[0]
    assert ast.dump(candidate, include_attributes=False) == ast.dump(baseline, include_attributes=False)


@pytest.fixture(scope="module")
def disposable_target():
    if not pg.ADMIN_URL or not pg.HOST_ALIAS:
        if os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") == "1":
            pytest.fail("strict column proof requires disposable admin DSN/alias")
        pytest.skip("no disposable PostgreSQL configured")
    admin = make_url(pg.ADMIN_URL)
    assert admin.host == "127.0.0.1" and admin.port == 5433 and admin.database == "postgres"
    assert admin.username == "nahla" and admin.password == "nahla_password"
    assert BASELINE.is_dir() and LEGACY.is_dir() and CANDIDATE.is_dir()
    _assert_only_column_guard_changed()


def _execute(engine, sql):
    with engine.begin() as conn:
        result = conn.execute(text(sql))
        return result.fetchall() if result.returns_rows else None


def _column_shape(engine):
    return _execute(engine, "SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
                    "pg_get_expr(d.adbin,d.adrelid) FROM pg_catalog.pg_attribute a "
                    "LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
                    "WHERE a.attrelid=pg_catalog.to_regclass('whatsapp_usage') "
                    "AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum")


def _run_source_loop(root, engine):
    _, loop = _source(root)
    records = []

    class Capture:
        # The imported release gate deliberately disables process-wide logging.
        # Collect the source warning calls directly without changing that guard.
        def warning(self, message, *arguments):
            records.append(message % arguments)
    namespace = {"engine": engine, "text": text, "logger": Capture(), "safe_alters": ALTERS}
    exec(compile(ast.Module(body=[copy.deepcopy(loop)], type_ignores=[]),
                 str(root / "backend/main.py"), "exec"), namespace)
    return records


def _assert_stale_column_failures(errors):
    assert len(errors) == 2
    for statement, error in zip(ALTERS, errors):
        assert error.startswith("Startup migration skipped statement: " + statement)
        assert "psycopg2.errors.UndefinedColumn" in error and '"conversations_used"' in error


@pytest.mark.parametrize("schema", ("public", "usage_probe"))
@pytest.mark.parametrize("column_present", (True, False), ids=("legacy-column", "dropped-column"))
def test_only_existing_legacy_column_is_repaired(disposable_target, schema, column_present):
    db = pg._fresh_db("startup_column")
    engine = create_engine(pg._loopback_url(db), poolclass=NullPool,
                           connect_args={"options": f"-c search_path={schema},public"})
    try:
        if schema != "public":
            _execute(engine, "CREATE SCHEMA usage_probe")
            # The decoy has the opposite column state: relation resolution must
            # follow the ALTER's search_path rather than a hardcoded public scan.
            decoy = ",conversations_used INTEGER NOT NULL DEFAULT 9" if not column_present else ""
            _execute(engine, "CREATE TABLE public.whatsapp_usage(id INTEGER PRIMARY KEY" + decoy + ")")
        _execute(engine, "CREATE TABLE whatsapp_usage(id INTEGER PRIMARY KEY,"
                        "conversations_used INTEGER NOT NULL DEFAULT 7,"
                        "service_conversations_used INTEGER NOT NULL DEFAULT 0,"
                        "marketing_conversations_used INTEGER NOT NULL DEFAULT 0)")
        _execute(engine, "INSERT INTO whatsapp_usage VALUES (1,42,11,5)")
        if not column_present:
            _execute(engine, "ALTER TABLE whatsapp_usage DROP COLUMN conversations_used")
        before_shape = _column_shape(engine)
        before_rows = _execute(engine, "SELECT CAST(t AS text) FROM whatsapp_usage t ORDER BY id")
        oid = _execute(engine, "SELECT pg_catalog.to_regclass('whatsapp_usage')::oid")[0][0]
        baseline_errors = _run_source_loop(BASELINE, engine)
        baseline_shape = _column_shape(engine)
        if column_present:
            assert baseline_errors == []
            _execute(engine, "ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used SET DEFAULT 7")
            _execute(engine, "ALTER TABLE whatsapp_usage ALTER COLUMN conversations_used SET NOT NULL")
            assert _column_shape(engine) == before_shape
        else:
            _assert_stale_column_failures(baseline_errors)
            assert baseline_shape == before_shape
        assert _run_source_loop(CANDIDATE, engine) == []
        assert _run_source_loop(CANDIDATE, engine) == []
        assert _column_shape(engine) == baseline_shape
        assert _execute(engine, "SELECT CAST(t AS text) FROM whatsapp_usage t ORDER BY id") == before_rows
        assert _execute(engine, "SELECT pg_catalog.to_regclass('whatsapp_usage')::oid")[0][0] == oid
        if column_present:
            _execute(engine, "INSERT INTO whatsapp_usage(id) VALUES (2)")
            _execute(engine, "INSERT INTO whatsapp_usage(id,conversations_used) VALUES (3,NULL)")
            assert _execute(engine, "SELECT conversations_used FROM whatsapp_usage WHERE id>=2 ORDER BY id") == [(0,), (None,)]
        if schema != "public":
            decoy_columns = _execute(engine, "SELECT attname FROM pg_catalog.pg_attribute "
                                    "WHERE attrelid='public.whatsapp_usage'::regclass AND attnum>0 AND NOT attisdropped")
            assert (("conversations_used",) in decoy_columns) is (not column_present)
        print("LEGACY_COLUMN_PROOF", schema, "present" if column_present else "dropped", "PASS")
    finally:
        engine.dispose()
        pg._drop(db)


@pytest.fixture(scope="module", params=("S0", "S3"))
def compared_state(request, tmp_path_factory, disposable_target):
    phase = request.param
    db = pg._fresh_db("startup_comparison")
    url = pg._loopback_url(db)
    work = tmp_path_factory.mktemp("startup_comparison_" + phase)
    try:
        for revision in ("0093", "0111", "0113", "0120"):
            result = pg._alembic(db, "upgrade", revision)
            assert result.returncode == 0, result.stderr[-2000:]
        deployed = prior._startup(LEGACY, url, work / "legacy_hook")
        _assert_stale_column_failures(deployed["sql_or_bootstrap_errors"])
        if phase == "S3":
            for revision in ("0121", "0122", "0123"):
                result = pg._alembic(db, "upgrade", revision)
                assert result.returncode == 0, result.stderr[-2000:]
        sentinels = topo.plant_sentinels(url)
        extra = prior._extra_rows(url, phase, sentinels["tenants"][0])
        schema = topo.schema_snapshot(url, exclude_prefixes=(), exclude=())
        identities = prior._identities(url)
        counts = topo.row_counts(url, topo.SENTINEL_TABLES)
        baseline = prior._startup(BASELINE, url, work / "baseline_hook")
        candidate = [prior._startup(CANDIDATE, url, work / ("candidate_hook_" + str(i))) for i in range(2)]
        yield {"phase": phase, "db": db, "url": url, "work": work, "deployed": deployed,
               "baseline": baseline, "candidate": candidate, "schema": schema,
               "identities": identities, "sentinels": sentinels, "counts": counts, "extra": extra}
    finally:
        pg._drop(db)


def test_candidate_removes_only_baseline_errors_and_preserves_startup_state(compared_state):
    state = compared_state
    _assert_stale_column_failures(state["baseline"]["sql_or_bootstrap_errors"])
    for report in (state["deployed"], state["baseline"], *state["candidate"]):
        assert report["db_tasks"] == ["_bootstrap_db_schema_bg", "_migrate_background"]
        assert report["safe_alters_ready"] and report["bootstrap_ready"]
        assert report["subprocesses"] == [{"operation": "cleanup", "returncode": 0},
                                          {"operation": "upgrade 0093", "returncode": 0}]
        assert report["withheld_tasks"] == ["_coexistence_client_id_repair_bg",
                                            "_reconcile_pending_moyasar_sweep", "run_runtime_heartbeat"]
    for report in state["candidate"]:
        assert report["sql_or_bootstrap_errors"] == []
    expected = topo.S0 if state["phase"] == "S0" else topo.S3
    topo.assert_rows(state["url"], expected, "after repaired startup")
    actual = topo.schema_snapshot(state["url"], exclude_prefixes=(), exclude=())
    assert actual == state["schema"], topo.schema_diff(actual, state["schema"])
    assert prior._identities(state["url"]) == state["identities"]
    topo.assert_sentinels(state["url"], state["sentinels"], "after repaired startup")
    assert topo.row_counts(state["url"], topo.SENTINEL_TABLES) == state["counts"]
    for table, hashes in state["extra"].items():
        assert prior._sql(state["url"], f"SELECT encode(sha256(convert_to(CAST(t AS text),'UTF8')),'hex') "
                          f"FROM {table} t ORDER BY CAST(t AS text)") == hashes, table
    if state["phase"] == "S0":
        assert not any(name.startswith("shopify_") for name in actual)
    print("REPAIRED_STARTUP_PRESERVATION", state["phase"], "PASS", json.dumps({
        "public_tables": len(actual), "relation_identities": len(state["identities"]),
        "candidate_boots": 2, "baseline_expected_errors": 2, "candidate_errors": 0,
        "version_rows": sorted(expected)}, sort_keys=True))


def test_release_gate_still_accepts_repaired_startup(compared_state):
    state = compared_state
    root = gate_harness.build_root(state["work"] / "gate_root", source=CANDIDATE)
    rc, out = gate_harness.run_gate(root, state["phase"], pg._alias_url(state["db"]), pg._target(state["db"]))
    checks = gate_harness.checks(out)
    assert rc == 0 and gate_harness.result(out) == contract.PASS, out[-3000:]
    assert len(checks) == 19 and all(value == (contract.PASS, None) for value in checks.values())
    print("REPAIRED_STARTUP_GATE", state["phase"], rc, json.dumps(checks, sort_keys=True))


def test_provider_and_scheduler_boundaries_still_hold(tmp_path):
    prior.test_disabled_scheduler_and_provider_boundaries_are_exercised(tmp_path)


def test_real_preflight_still_enforces_required_secrets(tmp_path):
    prior.test_actual_production_preflight_accepts_synthetic_config_and_refuses_missing_secret(tmp_path)
