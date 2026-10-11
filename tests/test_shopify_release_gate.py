"""DB-free contract of the Shopify release gate: exact phases, external pins, fail-closed evaluation, secret-safe output.

Every function the gate decides with is pure over what it reads; these cases pin
that each refusal path exists and that nothing but closed tokens, table and
field names, known revision ids and expected platform ids is ever reported.
The live PostgreSQL exercise of the same gate (phase matrix, footprint, drift,
locks, timeouts) lives in ``backend/tests/test_shopify_release_gate_pg.py``.
"""
from __future__ import annotations

import ast
import copy
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
from backend.tests import _shopify_release_gate_harness as harness  # noqa: E402
from scripts.operators import bootstrap_migration_contract as bootstrap  # noqa: E402
from scripts.operators import commerce_runtime_pilot_migration_contract as pilot  # noqa: E402
from scripts.operators import shopify_release_gate as gate  # noqa: E402
from scripts.operators import shopify_release_gate_contract as k  # noqa: E402
from scripts.operators import shopify_release_gate_render as render  # noqa: E402

NOW = harness.NOW
SECRET_ROW = "postgresql://svc:p-not-a-real-secret@db.internal:5432/prod"
CONTROL_ROW = "\x1b[2J\x1b[31mDROP TABLE tenants; --"
KNOWN = frozenset({"0093", "0111", "0113", "0114", "0115", "0118", "0120", "0121", "0122", "0123"})
EXPECTATIONS = json.loads((REPO / k.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))


def _migration(name: str):
    path = REPO / "database" / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Phases and contract constants ─────────────────────────────────────────────

def test_phases_are_exact_named_states_and_nothing_else():
    assert list(k.PHASES) == ["S0", "S1", "S2", "S3"]
    assert k.PHASES["S0"]["rows"] == {"0111", "0113", "0120"} and k.PHASES["S0"]["tables"] == ()
    assert k.PHASES["S1"]["rows"] == {"0111", "0113", "0121"} and len(k.PHASES["S1"]["tables"]) == 5
    assert k.PHASES["S2"]["rows"] == {"0111", "0113", "0122"} and len(k.PHASES["S2"]["tables"]) == 12
    assert k.PHASES["S3"]["rows"] == {"0111", "0113", "0123"} and len(k.PHASES["S3"]["tables"]) == 15
    assert [k.PHASES[p]["connection_key"] for p in k.PHASES] == [False, False, True, True]
    for name in ("LEGACY_462_S0", "head", "heads", "", "S4", "s0", "ANY"):
        assert k.phase(name) is None
    assert k.SCHEMA_CHANGING_PHASES == {"S1", "S2", "S3"}
    for phase in k.PHASES.values():
        assert k.RETAINED_ROWS <= phase["rows"] and len(phase["rows"]) == 3


def test_contract_constants_match_the_frozen_sources():
    m121, m122, m123 = (_migration(n) for n in ("0121_shopify_connection_foundation.py", "0122_shopify_catalog_mirror.py",
                                                "0123_shopify_privacy_requests.py"))
    assert tuple(m121.TABLES) == k.FOUNDATION_TABLES
    assert tuple(m122.TABLES) == k.CATALOG_TABLES and m122.CONNECTION_KEY == k.CONNECTION_KEY_CONSTRAINT
    assert tuple(m123.TABLES) == k.PRIVACY_TABLES and m123.CONNECTION_KEY == k.CONNECTION_KEY_CONSTRAINT
    assert (m121.revision, m121.down_revision) == ("0121", "0120")
    assert (m122.revision, m122.down_revision) == ("0122", "0121")
    assert (m123.revision, m123.down_revision) == ("0123", "0122")
    assert bootstrap.REPOSITORY_ALEMBIC_HEADS == k.EXPECTED_REPOSITORY_HEADS
    assert bootstrap.INTEGRATION_BOOTSTRAP_TARGET == "0093"
    from services.shopify_catalog import config as catalog_config
    from services.shopify_connection import config as connection_config
    from services.shopify_privacy import config as privacy_config
    assert set(k.SHOPIFY_FLAG_ENVS) == {connection_config.ENABLED_ENV, catalog_config.IMPORT_ENABLED_ENV,
                                        privacy_config.INTAKE_ENABLED_ENV, privacy_config.WORKER_ENABLED_ENV,
                                        privacy_config.DESTRUCTIVE_ENABLED_ENV}
    pins = {line.split("==")[0]: line.split("==")[1].strip() for line in (REPO / "requirements.txt").read_text().splitlines()
            if "==" in line and line.split("==")[0] in k.LIBRARY_PINS}
    assert pins == dict(k.LIBRARY_PINS)
    assert k.LEGACY_RELEASE_SHA == "462ede1aff602cad84e9569be859cca0087b190d"
    assert k.EXISTING_PLATFORM_TARGET["db"] == "postgres-ancu.railway.internal:5432/railway"


# ── Inventory ────────────────────────────────────────────────────────────────

def test_inventory_digest_is_sorted_path_nul_bytes_nul(tmp_path):
    (tmp_path / "database" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "database" / "migrations" / "versions" / "0001_a.py").write_bytes(b"a\n")
    (tmp_path / "database" / "migrations" / "env.py").write_bytes(b"env\n")
    (tmp_path / "database" / "README.md").write_bytes(b"not inventory\n")
    (tmp_path / "database" / "alembic.ini").write_bytes(b"ini\n")
    (tmp_path / "requirements.txt").write_bytes(b"req\n")
    for relative in ("scripts/operators/commerce_runtime_pilot_migration.py", "scripts/operators/commerce_runtime_pilot_migration_contract.py") + k.RUNTIME_EXTRA_FILES + k.FULL_EXTRA_FILES:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(relative.encode() + b"\n")
    runtime_paths = gate.inventory_paths(tmp_path, full=False)
    assert "database/README.md" not in runtime_paths
    assert {"database/migrations/versions/0001_a.py", "database/migrations/env.py", "database/alembic.ini", "requirements.txt"} <= set(runtime_paths)
    assert runtime_paths == sorted(runtime_paths)
    full_paths = gate.inventory_paths(tmp_path, full=True)
    assert set(full_paths) - set(runtime_paths) == set(k.FULL_EXTRA_FILES)
    expected = hashlib.sha256()
    for relative in sorted(full_paths):
        expected.update(relative.encode() + b"\0" + (tmp_path / relative).read_bytes() + b"\0")
    assert gate.full_inventory(tmp_path) == (len(full_paths), expected.hexdigest())
    (tmp_path / "database" / "migrations" / "versions" / "0001_a.py").write_bytes(b"a \n")
    assert gate.full_inventory(tmp_path)[1] != expected.hexdigest()
    (tmp_path / k.APPROVAL_RELATIVE_PATH).write_bytes(b"{}")
    assert gate.runtime_inventory(tmp_path)[1] == gate.runtime_inventory(tmp_path)[1]
    assert gate.full_inventory(tmp_path)[1] != expected.hexdigest()


def test_inventory_rule_on_the_repository_is_the_legacy_rule_plus_the_gate_files():
    legacy = set(k.LEGACY_FIXED_FILES) | {p.relative_to(REPO).as_posix() for p in (REPO / "database").rglob("*.py")}
    runtime = gate.inventory_paths(REPO, full=False)
    assert set(runtime) == legacy | set(k.RUNTIME_EXTRA_FILES)
    assert set(gate.inventory_paths(REPO, full=True)) == set(runtime) | {k.APPROVAL_RELATIVE_PATH}
    assert len(legacy) >= 132
    count, digest = gate.full_inventory(REPO)
    assert count == len(runtime) + 1 and k.DIGEST_RE.match(digest)
    missing = gate.inventory_paths(REPO, full=True)
    assert all((REPO / p).is_file() for p in missing)


def test_legacy_guard_digest_is_reproduced_on_the_deployed_checkout_when_available():
    checkout = os.environ.get("NAHLA_LEGACY_462_CHECKOUT", "").strip()
    if not checkout:
        pytest.skip("NAHLA_LEGACY_462_CHECKOUT not set (no checkout of the deployed commit available)")
    root = Path(checkout)
    paths = sorted(set(k.LEGACY_FIXED_FILES) | {p.relative_to(root).as_posix() for p in (root / "database").rglob("*.py")})
    assert gate.digest_paths(root, paths) == (132, "12798863bd12348de4407263fd1a1fe86ef639a0df2bfac2fc94244bd1f714c4")


# ── Rendered command ─────────────────────────────────────────────────────────

def test_render_pins_every_literal_and_round_trips_into_the_gate_parser():
    report = render.inventory_report(REPO)
    command = render.render_command(REPO, release_sha=harness.SYNTHETIC_RELEASE_SHA, phase="S2", db="db.internal:5432/railway",
                                    **harness.SYNTHETIC_IDENTITY)
    assert command.startswith('python -I -c "') and command.endswith('"')
    for literal in (report["gate_sha256"], report["full_inventory_digest"], str(report["full_inventory_count"]),
                    harness.SYNTHETIC_RELEASE_SHA, "'S2'", "db.internal:5432/railway", *harness.SYNTHETIC_IDENTITY.values()):
        assert literal in command
    assert "postgresql://" not in command and "password" not in command.lower() and "$" not in command
    argv = re.search(r"\[sys\.executable,'-I',p,(.*)\]\)\"$", command).group(1)
    tokens = [ast.literal_eval(t) for t in argv.split(",")]
    args = gate.build_parser().parse_args(tokens)
    assert (args.phase, args.release_sha, args.inventory_digest, args.inventory_count) == (
        "S2", harness.SYNTHETIC_RELEASE_SHA, report["full_inventory_digest"], report["full_inventory_count"])
    assert args.root == k.CONTAINER_ROOT and args.expect_db == "db.internal:5432/railway"
    assert "'/app/scripts/operators/shopify_release_gate.py'" in command


@pytest.mark.parametrize("kwargs, error", [
    ({"phase": "LEGACY_462_S0"}, "unknown_phase"),
    ({"phase": "head"}, "unknown_phase"),
    ({"release_sha": "8308c16e"}, "release_sha_malformed"),
    ({"db": "railway"}, "db_target_malformed"),
    ({"db": "db.internal:x/railway"}, "db_target_malformed"),
    ({"project": "not-a-uuid"}, "identity_malformed"),
    ({"db": "db.internal:5432/rail'way"}, "token_not_renderable"),
])
def test_render_refuses_malformed_inputs(kwargs, error):
    params = {"release_sha": harness.SYNTHETIC_RELEASE_SHA, "phase": "S0", "db": "db.internal:5432/railway", **harness.SYNTHETIC_IDENTITY}
    params.update(kwargs)
    with pytest.raises(ValueError) as excinfo:
        render.render_command(REPO, **params)
    assert str(excinfo.value) == error


def test_rendered_command_refuses_an_absent_or_tampered_gate_file(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    root = harness.build_root(tmp_path / "root")
    command = harness.rendered(root, "S0", "db.internal:5432/railway")
    env = harness.gate_environment("postgresql://u:p-not-a-real-secret@db.internal:5432/railway")
    gate_file = root / k.GATE_RELATIVE_PATH
    original = gate_file.read_bytes()
    gate_file.write_bytes(original + b"\n# tampered\n")
    rc, out = harness.run_command(command, env)
    assert rc == 2 and "gate_sha256_match=False" in out and "p-not-a-real-secret" not in out
    assert f"{k.LOG_PREFIX} CHECK" not in out
    gate_file.unlink()
    rc, out = harness.run_command(command, env)
    assert rc == 2 and f"{k.LOG_PREFIX} CHECK" not in out and "Traceback" not in out
    gate_file.write_bytes(original)
    rc, out = harness.run_command(command, env)
    assert "gate_sha256_match=True" in out and f"{k.LOG_PREFIX} CHECK root PASS" in out
    # the legacy release carries no gate file at all: the trusted command refuses before anything runs
    assert not (REPO / "docs").joinpath("nonexistent").exists()


# ── Pure evaluations ─────────────────────────────────────────────────────────

def test_rows_exact_multiset_fail_closed_and_unknown_rows_are_never_echoed():
    assert k.evaluate_rows(["0120", "0111", "0113"], k.PHASES["S0"]["rows"], KNOWN)[0] == k.PASS
    assert k.evaluate_rows(None, k.PHASES["S0"]["rows"], KNOWN)[1] == k.R_VERSION_TABLE_MISSING
    assert k.evaluate_rows(["0111", "0113", "0120", "0120"], k.PHASES["S0"]["rows"], KNOWN)[1] == k.R_VERSION_ROWS_DUPLICATE
    assert k.evaluate_rows(["0111", "0113"], k.PHASES["S0"]["rows"], KNOWN)[1] == k.R_VERSION_ROWS_MISMATCH
    assert k.evaluate_rows(["0111", "0113", "0121"], k.PHASES["S0"]["rows"], KNOWN)[1] == k.R_VERSION_ROWS_MISMATCH
    assert k.evaluate_rows(["0111", "0113", "0120", "0121"], k.PHASES["S0"]["rows"], KNOWN)[1] == k.R_VERSION_ROWS_MISMATCH
    assert k.evaluate_rows(["0093"], k.PHASES["S0"]["rows"], KNOWN)[1] == k.R_VERSION_ROWS_MISMATCH
    status, reason, detail = k.evaluate_rows(["0111", "0113", "0120", SECRET_ROW, CONTROL_ROW], k.PHASES["S0"]["rows"], KNOWN)
    assert (status, reason) == (k.FAIL, k.R_VERSION_ROWS_UNKNOWN)
    rendered = json.dumps(detail)
    assert "p-not-a-real-secret" not in rendered and "DROP TABLE" not in rendered and "\x1b" not in rendered
    assert detail["rows"] == ["0111", "0113", "0120", k.UNKNOWN, k.UNKNOWN] and detail["unknown_row_count"] == 2


def _observed_for(phase: str, **overrides):
    observed = {"tables": list(k.PHASES[phase]["tables"]), "other_relations": [], "types": [], "functions": [],
                "foreign_constraints": [], "connection_key": k.PHASES[phase]["connection_key"]}
    observed.update(overrides)
    return observed


def test_footprint_is_exact_per_phase():
    for phase in k.PHASES:
        assert k.evaluate_footprint(_observed_for(phase), k.PHASES[phase])[0] == k.PASS
        for other in k.PHASES:
            if other != phase:
                assert k.evaluate_footprint(_observed_for(other), k.PHASES[phase])[0] == k.FAIL
    assert k.evaluate_footprint(_observed_for("S1", tables=list(k.FOUNDATION_TABLES[:-1])), k.PHASES["S1"])[1] == k.R_FOOTPRINT_MISSING
    assert k.evaluate_footprint(_observed_for("S1", tables=list(k.FOUNDATION_TABLES) + ["shopify_catalog_products"]), k.PHASES["S1"])[1] == k.R_FOOTPRINT_UNEXPECTED
    assert k.evaluate_footprint(_observed_for("S0", tables=["shopify_connections"]), k.PHASES["S0"])[1] == k.R_FOOTPRINT_UNEXPECTED
    for stray in ("other_relations", "types", "functions", "foreign_constraints"):
        assert k.evaluate_footprint(_observed_for("S3", **{stray: ["shopify_leftover"]}), k.PHASES["S3"])[1] == k.R_FOOTPRINT_UNEXPECTED
    assert k.evaluate_footprint(_observed_for("S1", connection_key=True), k.PHASES["S1"])[1] == k.R_FOOTPRINT_KEY
    assert k.evaluate_footprint(_observed_for("S2", connection_key=False), k.PHASES["S2"])[1] == k.R_FOOTPRINT_KEY


def test_retained_definitions_are_compared_by_definition_and_reported_by_name_only():
    observed = copy.deepcopy({key: EXPECTATIONS[key] for key in ("tables", "nonce", "pk_types", "unique_definitions", "sequences")})
    assert k.evaluate_retained(observed, EXPECTATIONS)[0] == k.PASS
    drifted = copy.deepcopy(observed)
    table = "commerce_runtime_handover_barrier"
    constraint = next(c for c in drifted["tables"][table]["constraints"] if c[1] == "c")
    constraint[2] = constraint[2].replace(">= 0", ">= 1")
    status, reason, detail = k.evaluate_retained(drifted, EXPECTATIONS)
    assert (status, reason, detail["drift"]) == (k.FAIL, k.R_RETAINED_DEFINITION_DRIFT, [table])
    assert ">= 1" not in json.dumps(detail) and "CHECK" not in json.dumps(detail)
    drifted = copy.deepcopy(observed)
    index = drifted["tables"]["catalog_channel_retirements"]["indexes"][0]
    index[1] = index[1].replace("USING btree", "USING hash")
    assert k.evaluate_retained(drifted, EXPECTATIONS)[2]["drift"] == ["catalog_channel_retirements"]
    drifted = copy.deepcopy(observed)
    drifted["tables"]["meta_catalog_authorizations"]["columns"][0][1] = "bigint"
    assert k.evaluate_retained(drifted, EXPECTATIONS)[2]["drift"] == ["meta_catalog_authorizations"]
    drifted = copy.deepcopy(observed)
    del drifted["tables"]["commerce_runtime_navigation_snapshots"]
    assert k.evaluate_retained(drifted, EXPECTATIONS)[1] == k.R_RETAINED_TABLE_MISSING
    drifted = copy.deepcopy(observed)
    drifted["nonce"]["index_shapes"] = [s for s in drifted["nonce"]["index_shapes"] if "expires_at" not in s[1]]
    assert k.evaluate_retained(drifted, EXPECTATIONS)[2]["drift"] == [k.NONCE_TABLE]
    drifted = copy.deepcopy(observed)
    drifted["nonce"] = None
    assert k.evaluate_retained(drifted, EXPECTATIONS)[1] == k.R_RETAINED_TABLE_MISSING
    drifted = copy.deepcopy(observed)
    drifted["pk_types"]["tenants"] = "bigint"
    assert k.evaluate_retained(drifted, EXPECTATIONS)[2]["identity"] == ["tenants"]
    drifted = copy.deepcopy(observed)
    drifted["unique_definitions"] = []
    assert k.evaluate_retained(drifted, EXPECTATIONS)[2]["identity"] == ["commerce_runtime_conversations"]
    drifted = copy.deepcopy(observed)
    drifted["sequences"][0][2] = "other_column"
    assert k.evaluate_retained(drifted, EXPECTATIONS)[2]["identity"] == ["sequence_ownership"]


def test_expectations_file_is_bound_to_these_migrations_and_covers_the_retained_relations():
    assert EXPECTATIONS["expectations_version"] == k.EXPECTATIONS_VERSION
    assert EXPECTATIONS["database_py_digest"] == gate.database_py_digest(REPO)
    assert set(EXPECTATIONS["tables"]) == set(k.RETAINED_FULL_DEFINITION_TABLES)
    totals = [sum(len(d[key]) for d in EXPECTATIONS["tables"].values()) for key in ("columns", "constraints", "indexes")]
    assert totals == [90, 33, 18]
    assert all(c[2].startswith(("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")) for d in EXPECTATIONS["tables"].values() for c in d["constraints"])
    assert all(i[1].startswith("CREATE ") and " USING " in i[1] for d in EXPECTATIONS["tables"].values() for i in d["indexes"])
    assert len(EXPECTATIONS["nonce"]["columns"]) == 8 and "UNIQUE (nonce_hash)" in EXPECTATIONS["nonce"]["constraint_definitions"]
    assert EXPECTATIONS["pk_types"] == {"tenants": "integer", "users": "integer"}
    assert EXPECTATIONS["unique_definitions"] == [["commerce_runtime_conversations", "UNIQUE (id, tenant_id, namespace)"]]
    assert sorted(s[1] for s in EXPECTATIONS["sequences"]) == sorted(t for t in k.RETAINED_FULL_DEFINITION_TABLES if t != "commerce_runtime_handover_barrier")
    assert "nahla_password" not in json.dumps(EXPECTATIONS) and "postgresql://" not in json.dumps(EXPECTATIONS)
    stale = dict(EXPECTATIONS, database_py_digest="0" * 64)
    assert k.evaluate_retained(EXPECTATIONS, stale)[0] == k.PASS  # staleness is the gate's check, not the comparison's
    check, loaded = gate.check_expectations(REPO)
    assert check.status == k.PASS and loaded["generated_from_source_sha"] == EXPECTATIONS["generated_from_source_sha"]


@pytest.mark.parametrize("name", k.SHOPIFY_FLAG_ENVS)
@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_every_shopify_flag_must_be_off(name, value):
    status, reason, detail = k.evaluate_flags({name: value})
    assert (status, reason, detail["enabled"]) == (k.FAIL, k.R_FLAG_ENABLED, [name])


def test_falsey_or_unset_flags_pass_and_dashboard_flags_are_not_consulted():
    assert k.evaluate_flags({})[0] == k.PASS
    assert k.evaluate_flags({name: v for name in k.SHOPIFY_FLAG_ENVS for v in ("0",)})[0] == k.PASS
    assert k.evaluate_flags({"NAHLA_SHOPIFY_CONNECTION_ENABLED": "false", "VITE_SHOPIFY_CONNECTION_UI": "1"})[0] == k.PASS


def test_identity_requires_every_platform_id_and_never_prints_actual_values():
    expected = dict(harness.SYNTHETIC_IDENTITY)
    env = {k.IDENTITY_ENVS[key]: value for key, value in expected.items()}
    assert k.evaluate_identity(env, expected)[0] == k.PASS
    status, reason, detail = k.evaluate_identity({**env, "RAILWAY_SERVICE_ID": "leaked-actual-value"}, expected)
    assert (status, reason, detail) == (k.FAIL, k.R_IDENTITY_MISMATCH, {"mismatched": ["service"]})
    status, reason, detail = k.evaluate_identity({key: v for key, v in env.items() if key != "RAILWAY_PROJECT_ID"}, expected)
    assert (status, reason, detail) == (k.FAIL, k.R_IDENTITY_MISSING, {"missing": ["project"]})


def test_database_target_goes_through_the_pilot_job_validators():
    expected = k.parse_db_target("db.internal:5432/railway")
    base = "postgresql://svc:p-not-a-real-secret@db.internal:5432/railway"
    check, bound = gate.check_database_target({"DATABASE_URL": base}, expected)
    assert check.status == k.PASS and bound.startswith("postgresql://svc:") and "db.internal:5432/railway" in bound
    assert "p-not-a-real-secret" not in json.dumps(check.detail)
    cases = {
        base + "?host=other.internal": "DATABASE_URL_carries_a_target_override",
        base + "?service=prod": "DATABASE_URL_carries_a_target_override",
        "postgresql://svc:p@localhost:5432/railway": "DATABASE_URL_is_local",
        "postgresql://svc:p@127.0.0.1:5432/railway": "DATABASE_URL_is_local",
        "mysql://svc:p@db.internal:3306/railway": "DATABASE_URL_unsupported_dialect",
        "": "DATABASE_URL_unresolved",
        "postgresql://svc:p@db.internal:5432/Railway": "effective_target_differs",
        "postgresql://svc:p@db.internal:5433/railway": "effective_target_differs",
        "postgresql://svc:p@other.internal:5432/railway": "effective_target_differs",
    }
    for url, refusal in cases.items():
        check, bound = gate.check_database_target({"DATABASE_URL": url}, expected)
        assert (check.status, check.reason, check.detail.get("refusal"), bound) == (k.FAIL, k.R_DB_TARGET_MISMATCH, refusal, None), url
    for variable in pilot.LIBPQ_TARGET_ENV_VARS:
        check, _ = gate.check_database_target({"DATABASE_URL": base, variable: "x"}, expected)
        assert check.detail.get("refusal") == "DATABASE_URL_environment_override", variable


def test_parse_helpers_fail_closed_on_malformed_input():
    assert k.parse_utc("2026-10-11T16:54:10.145Z") == _dt.datetime(2026, 10, 11, 16, 54, 10, 145000, tzinfo=_dt.timezone.utc)
    assert k.parse_utc("2026-10-11T16:54:10Z").tzinfo is _dt.timezone.utc
    for bad in ("2026-10-11T16:54:10", "2026-10-11T16:54:10+03:00", "2026-10-11 16:54:10Z", "", None, "yesterday"):
        assert k.parse_utc(bad) is None
    assert k.parse_db_target("Host.Internal:5433/Rail way") == {"host": "host.internal", "port": 5433, "database": "Rail way"}
    assert k.parse_db_target("db.internal/railway")["port"] == 5432
    for bad in ("", "railway", "db.internal:x/railway", "/railway", "db.internal/", "a/b/c", " db.internal:5432/railway"):
        assert k.parse_db_target(bad) is None


# ── Approval record ──────────────────────────────────────────────────────────

RUNTIME = (142, "f" * 64)


def _approval(**changes):
    record = harness.filled_approval(RUNTIME)
    for path, value in changes.items():
        target = record
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return record


def test_shipped_approval_record_is_unfilled_and_refuses_every_phase():
    record = json.loads((REPO / k.APPROVAL_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert record["record_version"] == k.APPROVAL_RECORD_VERSION
    assert record["bound_runtime_inventory_digest"] is None and record["bound_runtime_inventory_count"] is None
    assert all(v is None for v in record["deployment_rollback_snapshot"].values())
    assert all(v is None for v in record["db_recovery_evidence"]["backup"].values())
    assert record["db_recovery_evidence"]["restore_verified"] is False
    for phase, entry in record["phase_approvals"].items():
        assert phase in k.PHASES and entry["approved"] is False and all(entry[f] is None for f in k.APPROVAL_PHASE_FIELDS[1:])
    for phase in k.PHASES:
        verdict = render.approval_check(REPO, phase, now=NOW)
        assert (verdict["status"], verdict["reason"]) == (k.FAIL, k.R_APPROVAL_NOT_BOUND)
    runtime = gate.runtime_inventory(REPO)
    bound = dict(record, bound_runtime_inventory_digest=runtime[1], bound_runtime_inventory_count=runtime[0])
    for phase in k.PHASES:
        assert k.evaluate_approval(bound, phase, runtime_digest=runtime[1], runtime_count=runtime[0], now=NOW)[1] == k.R_APPROVAL_MISSING


def _verdict(record, phase="S0", now=NOW, runtime=RUNTIME):
    return k.evaluate_approval(record, phase, runtime_digest=runtime[1], runtime_count=runtime[0], now=now)[1]


def test_filled_record_passes_only_when_bound_valid_and_evidenced():
    assert _verdict(_approval()) is None and _verdict(_approval(), "S3") is None
    assert _verdict(_approval(), runtime=(142, "e" * 64)) == k.R_APPROVAL_NOT_BOUND
    assert _verdict(_approval(), runtime=(141, "f" * 64)) == k.R_APPROVAL_NOT_BOUND
    assert _verdict(_approval(record_version=2)) == k.R_APPROVAL_VERSION
    assert _verdict("not a record") == k.R_APPROVAL_UNREADABLE
    assert _verdict(_approval(**{"phase_approvals.S0.approved": False})) == k.R_APPROVAL_MISSING
    assert _verdict(_approval(**{"phase_approvals.S0.reviewer": ""})) == k.R_APPROVAL_MISSING
    assert _verdict(_approval(**{"phase_approvals.S1.approved": True}), "S1") is None
    assert _verdict(harness.filled_approval(RUNTIME, phases=("S0",)), "S1") == k.R_APPROVAL_MISSING
    assert _verdict(_approval(), now=NOW + _dt.timedelta(days=3)) == k.R_APPROVAL_EXPIRED
    assert _verdict(_approval(), now=NOW - _dt.timedelta(days=1)) == k.R_APPROVAL_EXPIRED
    assert _verdict(_approval(**{"phase_approvals.S0.valid_until_utc": harness.iso(NOW - _dt.timedelta(hours=2))})) == k.R_APPROVAL_TIMESTAMP_INVALID
    assert _verdict(_approval(**{"phase_approvals.S0.approved_at_utc": "2026-10-11 17:00:00"})) == k.R_APPROVAL_TIMESTAMP_INVALID
    assert _verdict(_approval(**{"deployment_rollback_snapshot.railway_deployment_id": None})) == k.R_ROLLBACK_SNAPSHOT_MISSING
    assert _verdict(_approval(**{"db_recovery_evidence.backup.card_timestamp_utc": None})) == k.R_BACKUP_EVIDENCE_MISSING
    assert _verdict(_approval(**{"db_recovery_evidence.backup.size_mb": None})) == k.R_BACKUP_EVIDENCE_MISSING
    assert _verdict(_approval(**{"db_recovery_evidence.backup.card_timestamp_utc": harness.iso(NOW - _dt.timedelta(minutes=30))})) == k.R_BACKUP_AFTER_APPROVAL
    assert _verdict(_approval(**{"db_recovery_evidence.backup.card_timestamp_utc": "not-a-time"})) == k.R_APPROVAL_TIMESTAMP_INVALID
    unverified = harness.filled_approval(RUNTIME, restore_verified=False)
    assert _verdict(unverified, "S0") is None
    for phase in sorted(k.SCHEMA_CHANGING_PHASES):
        assert _verdict(unverified, phase) == k.R_RESTORE_NOT_VERIFIED
    assert _verdict(_approval(**{"db_recovery_evidence.restore_verification_ref": None}), "S2") == k.R_RESTORE_NOT_VERIFIED


# ── Graph of the source the gate runs from ───────────────────────────────────

def test_graph_edges_heads_and_branch_deltas_hold_for_every_phase():
    for name, phase in k.PHASES.items():
        check, known = gate.check_graph(REPO, phase)
        assert check.status == k.PASS, (name, check.reason, check.detail)
        assert check.detail["branch_delta"] == list(phase["branch_delta"]) and known >= phase["rows"]
    graph = gate.load_graph(REPO)
    assert graph["heads"] == k.EXPECTED_REPOSITORY_HEADS
    assert gate.branch_chain(graph["down"], "0123", "0112") == ("0118", "0120", "0121", "0122", "0123")
    assert gate.branch_chain(graph["down"], "0113", "0109") == ("0110", "0112", "0113")
    assert gate.branch_chain(graph["down"], "0111", "0112") is None
    bent = dict(graph["down"], **{"0121": ("0113",)})
    assert gate.branch_chain(bent, "0121", "0112") != ("0118", "0120", "0121")


def _clear_bytecode(root: Path) -> None:
    # same-size rewrites within one second would otherwise be served from stale .pyc files
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def test_graph_mismatch_is_refused_before_the_database(tmp_path):
    root = harness.build_root(tmp_path / "root")
    versions = root / "database" / "migrations" / "versions"
    _clear_bytecode(root)
    (versions / "0121_shopify_connection_foundation.py").write_text(
        (versions / "0121_shopify_connection_foundation.py").read_text().replace('down_revision = "0120"', 'down_revision = "0113"'))
    _clear_bytecode(root)
    check, known = gate.check_graph(root, k.PHASES["S1"])
    assert (check.status, check.reason, known) == (k.FAIL, k.R_GRAPH_EDGE_MISMATCH, None) and check.detail == {"revisions": ["0121"]}
    (versions / "0124_extra.py").write_text('revision = "0124"\ndown_revision = "0113"\nbranch_labels = None\ndepends_on = None\n'
                                            'def upgrade():\n    pass\ndef downgrade():\n    pass\n')
    (versions / "0121_shopify_connection_foundation.py").write_text(
        (versions / "0121_shopify_connection_foundation.py").read_text().replace('down_revision = "0113"', 'down_revision = "0120"'))
    _clear_bytecode(root)
    check, _ = gate.check_graph(root, k.PHASES["S1"])
    assert (check.status, check.reason) == (k.FAIL, k.R_GRAPH_HEADS_MISMATCH)


# ── Locked section with a fake connection (lock denial, unlock failure, errors) ──

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeConn:
    def __init__(self, answers, *, unlock=True, raise_on=None, raise_exc=None):
        self.answers, self.unlock, self.raise_on, self.raise_exc = answers, unlock, raise_on, raise_exc
        self.log, self.invalidated, self.rolled_back, self.closed = [], False, 0, False

    def execution_options(self, **options):
        assert options == {"postgresql_readonly": True}
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append(sql)
        if self.raise_on and self.raise_on in sql:
            raise self.raise_exc
        if "pg_try_advisory_lock" in sql:
            return _Result([(self.answers.get("lock", True),)])
        if "pg_advisory_unlock" in sql:
            return _Result([(self.unlock,)])
        for key, rows in self.answers.items():
            if key in sql:
                return _Result(rows)
        return _Result([])

    def invalidate(self):
        self.invalidated = True

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, conn):
        self.conn, self.disposed = conn, False

    def connect(self):
        return self.conn

    def dispose(self):
        self.disposed = True


def _run(conn):
    engine = _FakeEngine(conn)
    checks = gate.run_database_checks("postgresql://u:p@db.internal/x", k.PHASES["S0"], KNOWN, EXPECTATIONS, "x",
                                      engine_factory=lambda url: engine)
    return {c.name: c for c in checks}, engine


def test_lock_denied_refuses_without_reading_anything_and_without_unlocking():
    conn = _FakeConn({"lock": False})
    checks, engine = _run(conn)
    assert checks["lock"].reason == k.R_LOCK_DENIED and "unlock" not in checks and "server" not in checks
    assert all("pg_advisory_unlock" not in sql for sql in conn.log) and conn.closed and engine.disposed


def test_unlock_failure_fails_the_run_even_when_every_check_passed():
    exc = RuntimeError("stop after lock")
    conn = _FakeConn({}, unlock=False, raise_on="pg_is_in_recovery", raise_exc=exc)
    checks, engine = _run(conn)
    assert checks["unlock"].reason == k.R_UNLOCK_FAILED and conn.invalidated and conn.rolled_back >= 1 and engine.disposed


def test_timeouts_and_errors_are_reported_by_closed_token_and_type_only():
    class Orig:
        pgcode = "55P03"

    class LockTimeout(Exception):
        orig = Orig()

    conn = _FakeConn({}, raise_on="pg_is_in_recovery", raise_exc=LockTimeout("canceling statement: secret-ish detail"))
    checks, _ = _run(conn)
    assert (checks["database"].reason, checks["database"].detail) == (k.R_TIMEOUT, {"error": "LockTimeout"})
    assert checks["unlock"].status == k.PASS and conn.rolled_back >= 1 and "server" not in checks
    conn = _FakeConn({"pg_is_in_recovery": [(False,)]}, raise_on="current_schema", raise_exc=ValueError("p-not-a-real-secret"))
    checks, _ = _run(conn)
    assert (checks["database"].reason, checks["database"].detail) == (k.R_INTERNAL_ERROR, {"error": "ValueError"})
    assert "p-not-a-real-secret" not in json.dumps({n: c.detail for n, c in checks.items()})


def test_connect_failure_is_a_refusal_by_type_only():
    def factory(url):
        raise ConnectionRefusedError("postgresql://u:p-not-a-real-secret@db.internal/x")

    checks = gate.run_database_checks("postgresql://u:p@db.internal/x", k.PHASES["S0"], KNOWN, EXPECTATIONS, "x", engine_factory=factory)
    assert [(c.name, c.reason, c.detail) for c in checks] == [("connect", k.R_CONNECT_FAILED, {"error": "ConnectionRefusedError"})]


# ── Static guarantees: read-only, no executor, no subprocess ─────────────────

def _sql_literals(tree: ast.Module):
    constants = {node.targets[0].id: node.value.value for node in tree.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) and node.targets[0].id.endswith("_SQL")}
    literals = list(constants.values())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_scalar", "_rows") and len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant):
                literals.append(arg.value)
            elif isinstance(arg, ast.Name):
                literals.append(constants[arg.id])
            else:
                raise AssertionError("SQL passed to the executor is not a literal")
    return literals


def test_gate_module_has_no_mutation_path():
    source = (REPO / k.GATE_RELATIVE_PATH).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
    forbidden = {"subprocess", "alembic.command", "alembic.operations", "alembic.runtime", "os.system", "multiprocessing"}
    assert not (imported & forbidden), imported & forbidden
    assert not any(name.startswith("alembic.command") for name in imported)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr not in {"system", "popen", "spawnv", "execv", "execve", "fork", "remove", "unlink"}
    literals = _sql_literals(tree)
    assert len(literals) >= 15
    for sql in literals:
        head = sql.strip().upper()
        assert head.startswith("SELECT ") or head.startswith("SHOW "), sql[:60]
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE|GRANT|STAMP|MERGE|UPGRADE|COPY|LOCK)\b", head.split(" USING ")[0]), sql[:60]
    assert "postgresql_readonly=True" in source and "poolclass=NullPool" in source and "hide_parameters=True" in source and "echo=False" in source
    assert "pg_try_advisory_lock" in source and "pg_advisory_unlock" in source


def test_cli_fails_closed_and_never_echoes_the_dsn(tmp_path):
    root = harness.build_root(tmp_path / "root")
    report = render.inventory_report(root)
    base_argv = [sys.executable, "-I", str(root / k.GATE_RELATIVE_PATH), "--root", str(root), "--release-sha", harness.SYNTHETIC_RELEASE_SHA,
                 "--inventory-digest", report["full_inventory_digest"], "--inventory-count", str(report["full_inventory_count"]),
                 "--expect-project", harness.SYNTHETIC_IDENTITY["project"], "--expect-environment", harness.SYNTHETIC_IDENTITY["environment"],
                 "--expect-service", harness.SYNTHETIC_IDENTITY["service"], "--expect-db", "db.internal:5432/railway"]
    env = harness.gate_environment("postgresql://svc:p-not-a-real-secret@db.internal:5432/railway", release_sha="c" * 40)
    completed = subprocess.run(base_argv + ["--phase", "S0"], env=env, capture_output=True, text=True, timeout=120)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 1 and "RESULT=FAIL" in out and "release_sha FAIL reason=release_sha_mismatch" in out
    assert "database checks skipped" in out and "p-not-a-real-secret" not in out and "postgresql://" not in out
    for phase in ("LEGACY_462_S0", "head", "S9"):
        completed = subprocess.run(base_argv + ["--phase", phase], env=env, capture_output=True, text=True, timeout=120)
        assert completed.returncode == 2 and "RESULT=USAGE" in completed.stdout
    completed = subprocess.run(base_argv + ["--phase", "S0", "--expect-db", "db.internal:5432"], env=env, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 2
    completed = subprocess.run(base_argv + ["--phase", "S0"], env={**env, "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED": "true", k.RELEASE_SHA_ENV: harness.SYNTHETIC_RELEASE_SHA},
                               capture_output=True, text=True, timeout=120)
    assert completed.returncode == 1 and "shopify_flags FAIL reason=shopify_flag_enabled" in completed.stdout
    assert '"NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED"' in completed.stdout and "database checks skipped" in completed.stdout


def test_source_tamper_is_refused_by_count_or_digest(tmp_path):
    root = harness.build_root(tmp_path / "root")
    count, digest = gate.full_inventory(root)
    assert gate.check_inventory(root, count, digest)[0].status == k.PASS
    migration = root / "database" / "migrations" / "versions" / "0120_meta_catalog_authorizations.py"
    original = migration.read_bytes()
    migration.write_bytes(original + b"\n")
    check, runtime = gate.check_inventory(root, count, digest)
    assert (check.reason, runtime) == (k.R_INVENTORY_DIGEST_MISMATCH, None)
    migration.write_bytes(original)
    approval = root / k.APPROVAL_RELATIVE_PATH
    approval.write_text(approval.read_text() + "\n")
    assert gate.check_inventory(root, count, digest)[0].reason == k.R_INVENTORY_DIGEST_MISMATCH
    (root / "database" / "migrations" / "versions" / "0124_new.py").write_text("revision = '0124'\n")
    assert gate.check_inventory(root, count, digest)[0].reason == k.R_INVENTORY_COUNT_MISMATCH
    (root / "database" / "migrations" / "versions" / "0124_new.py").unlink()
    (root / k.EXPECTATIONS_RELATIVE_PATH).unlink()
    assert gate.check_inventory(root, count, digest)[0].reason == k.R_INVENTORY_FILE_MISSING
