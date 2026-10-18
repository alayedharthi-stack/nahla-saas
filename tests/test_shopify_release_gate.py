"""DB-free contract of the Shopify release gate: trust order, exact phases, external pins, fail-closed evaluation, secret-safe output.

Every function the gate decides with is pure over what it reads; these cases pin
that each refusal path exists, that the rendered bootstrap refuses a tampered
source **before any repository module executes**, that only S0 and S3 are
deployment phases, and that nothing but closed tokens, allow-listed names,
counts, known revision ids and expected platform ids is ever reported. The live
PostgreSQL exercise of the same gate lives in
``backend/tests/test_shopify_release_gate_pg.py``.
"""
from __future__ import annotations

import ast
import copy
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import py_compile
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
KNOWN = frozenset({"0093", "0111", "0113", "0114", "0115", "0118", "0120", "0121", "0122", "0123", "0124"})
EXPECTATIONS = json.loads((REPO / k.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))
DB = "db.internal:5432/railway"
UNROUTABLE_DB = "192.0.2.1:5432/railway"
TARGET = harness.target_for(DB)
RUNTIME = (143, "f" * 64)


def _migration(name: str):
    path = REPO / "database" / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(name[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clear_bytecode(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


# ── Phases and contract constants ─────────────────────────────────────────────

def test_only_s0_and_s3_are_deployment_phases():
    assert list(k.DEPLOY_PHASES) == ["S0", "S3"] and list(k.INTERMEDIATE_STATES) == ["S1", "S2"]
    assert k.DEPLOY_PHASES["S0"]["rows"] == {"0111", "0113", "0120"} and k.DEPLOY_PHASES["S0"]["tables"] == ()
    assert k.DEPLOY_PHASES["S3"]["rows"] == {"0111", "0113", "0123"} and len(k.DEPLOY_PHASES["S3"]["tables"]) == 15
    assert k.INTERMEDIATE_STATES["S1"] == {"0111", "0113", "0121"} and k.INTERMEDIATE_STATES["S2"] == {"0111", "0113", "0122"}
    for name in ("S1", "S2", "LEGACY_462_S0", "head", "heads", "", "S4", "s0", "ANY"):
        assert k.phase(name) is None
    assert k.SCHEMA_CHANGING_PHASES == {"S3", "S4_DORMANT", "S4_PILOT", "S4_COUPON_PRE_DORMANT", "S4_COUPON_PRE_PILOT", "S4_COUPON_POST_DORMANT", "S4_COUPON_POST_PILOT"}
    for phase in k.DEPLOY_PHASES.values():
        assert k.RETAINED_ROWS <= phase["rows"] and len(phase["rows"]) == 3


def test_contract_constants_match_the_frozen_sources():
    m121, m122, m123 = (_migration(n) for n in ("0121_shopify_connection_foundation.py", "0122_shopify_catalog_mirror.py",
                                                "0123_shopify_privacy_requests.py"))
    assert tuple(m121.TABLES) + tuple(m122.TABLES) + tuple(m123.TABLES) == k.SHOPIFY_TABLES
    assert m122.CONNECTION_KEY == m123.CONNECTION_KEY == k.CONNECTION_KEY_CONSTRAINT
    assert (m121.down_revision, m122.down_revision, m123.down_revision) == ("0120", "0121", "0122")
    assert bootstrap.REPOSITORY_ALEMBIC_HEADS == k.EXPECTED_REPOSITORY_HEADS and bootstrap.INTEGRATION_BOOTSTRAP_TARGET == "0093"
    from services.shopify_catalog import config as catalog_config
    from services.shopify_connection import config as connection_config
    from services.shopify_privacy import config as privacy_config
    assert set(k.SHOPIFY_FLAG_ENVS) == {connection_config.ENABLED_ENV, catalog_config.IMPORT_ENABLED_ENV, privacy_config.INTAKE_ENABLED_ENV,
                                        privacy_config.WORKER_ENABLED_ENV, privacy_config.DESTRUCTIVE_ENABLED_ENV,
                                        "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED", "VITE_SHOPIFY_INSTALL_FIRST_ENABLED",
                                        "VITE_SHOPIFY_CONNECTION_UI", "VITE_SHOPIFY_CATALOG_IMPORT_UI"}
    flags_ts = (REPO / "dashboard" / "src" / "lib" / "platformFeatureFlags.ts").read_text()
    assert "VITE_SHOPIFY_CONNECTION_UI" in flags_ts and "VITE_SHOPIFY_CATALOG_IMPORT_UI" in flags_ts
    pins = {line.split("==")[0]: line.split("==")[1].strip() for line in (REPO / "requirements.txt").read_text().splitlines()
            if "==" in line and line.split("==")[0] in k.LIBRARY_PINS}
    assert pins == dict(k.LIBRARY_PINS)
    assert tuple(k.LIBPQ_TARGET_ENV_VARS) == tuple(pilot.LIBPQ_TARGET_ENV_VARS)
    assert k.ADVISORY_LOCK_KEY == struct.unpack(">q", hashlib.sha256(k.ADVISORY_LOCK_KEY_SEED).digest()[:8])[0]
    assert k.ADVISORY_LOCK_KEY_SEED == b"nahla:primary:catalog-schema:0120" and k.SEARCH_PATH == "public,pg_catalog"
    assert k.LEGACY_RELEASE_SHA == "462ede1aff602cad84e9569be859cca0087b190d"
    assert k.EXISTING_PLATFORM_TARGET["db"] == "postgres-ancu.railway.internal:5432/railway"
    assert "scripts/operators/__init__.py" in k.RUNTIME_EXTRA_FILES and k.MUST_BE_ABSENT == ("scripts/__init__.py",)
    assert not (REPO / "scripts" / "__init__.py").exists()


# ── Inventory ────────────────────────────────────────────────────────────────

def test_inventory_digest_is_sorted_path_nul_bytes_nul(tmp_path):
    (tmp_path / "database" / "migrations" / "versions").mkdir(parents=True)
    (tmp_path / "database" / "migrations" / "versions" / "0001_a.py").write_bytes(b"a\n")
    (tmp_path / "database" / "migrations" / "env.py").write_bytes(b"env\n")
    (tmp_path / "database" / "README.md").write_bytes(b"not inventory\n")
    (tmp_path / "database" / "alembic.ini").write_bytes(b"ini\n")
    (tmp_path / "requirements.txt").write_bytes(b"req\n")
    for relative in ("scripts/operators/commerce_runtime_pilot_migration.py", "scripts/operators/commerce_runtime_pilot_migration_contract.py") + k.RUNTIME_EXTRA_FILES:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_bytes(relative.encode() + b"\n")
    paths = gate.inventory_paths(tmp_path)
    assert "database/README.md" not in paths and paths == sorted(paths)
    assert {"database/migrations/versions/0001_a.py", "database/migrations/env.py", "database/alembic.ini", "requirements.txt",
            "scripts/operators/__init__.py"} <= set(paths)
    expected = hashlib.sha256()
    for relative in sorted(paths):
        expected.update(relative.encode() + b"\0" + (tmp_path / relative).read_bytes() + b"\0")
    assert gate.inventory(tmp_path) == (len(paths), expected.hexdigest())
    (tmp_path / "database" / "migrations" / "versions" / "0001_a.py").write_bytes(b"a \n")
    assert gate.inventory(tmp_path)[1] != expected.hexdigest()


def test_inventory_rule_on_the_repository_is_the_legacy_rule_plus_the_gate_files():
    legacy = set(k.LEGACY_FIXED_FILES) | {p.relative_to(REPO).as_posix() for p in (REPO / "database").rglob("*.py")}
    assert set(gate.inventory_paths(REPO)) == legacy | set(k.RUNTIME_EXTRA_FILES)
    assert len(legacy) >= 132 and all((REPO / p).is_file() for p in gate.inventory_paths(REPO))
    assert gate.unexpected_initializers(REPO) == []
    count, digest = gate.inventory(REPO)
    assert count == len(legacy) + len(k.RUNTIME_EXTRA_FILES) and k.DIGEST_RE.match(digest)


def test_dormant_flag_guard_matches_backend_and_dashboard_source_flags():
    configs = ["shopify_connection/config.py", "shopify_connection/install_config.py",
               "shopify_catalog/config.py", "shopify_privacy/config.py"]
    backend_flags = set()
    for relative in configs:
        path = REPO / "backend/services" / relative
        assert path.relative_to(REPO).as_posix() in k.RUNTIME_EXTRA_FILES
        backend_flags.update(node.value for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                             if isinstance(node, ast.Constant) and isinstance(node.value, str)
                             and re.fullmatch(r"NAHLA_SHOPIFY_[A-Z_]+_ENABLED", node.value))
    frontend_flags = set()
    for path in (REPO / "dashboard/src").rglob("*"):
        if path.suffix in {".ts", ".tsx"}:
            frontend_flags.update(re.findall(r"import\.meta\.env\.(VITE_SHOPIFY_[A-Z_]+)", path.read_text(encoding="utf-8")))
    assert backend_flags | frontend_flags == set(k.SHOPIFY_FLAG_ENVS)
    assert "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED" in backend_flags
    assert "VITE_SHOPIFY_INSTALL_FIRST_ENABLED" in frontend_flags


def test_install_configuration_change_invalidates_inventory_and_previous_approval(tmp_path):
    root = harness.build_root(tmp_path / "root")
    before = gate.inventory(root)
    record = harness.filled_approval(release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=before,
                                     target=TARGET, phase="S3", now=NOW)
    config = root / "backend/services/shopify_connection/install_config.py"
    config.write_bytes(config.read_bytes() + b"\n# changed configuration\n")
    assert gate.check_inventory(root, *before)[0].reason == k.R_INVENTORY_DIGEST_MISMATCH
    assert _verdict(record, phase="S3", runtime=gate.inventory(root)) == k.R_APPROVAL_NOT_BOUND


def test_graph_dependency_pins_are_exact_sources_without_backend_wildcard(tmp_path):
    closure = {
        "backend/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "backend/payments/__init__.py": "e9405f349df7e7078d53d10552a647912b8d395777df0606b6a7bff600aecbf4",
        "backend/payments/models.py": "af5efddc8ba52f7f27ac8c5255a3add10bc7ea6e82b3321d83bcd2854a798b88",
    }
    assert all(k.RUNTIME_EXTRA_FILES.count(path) == 1 for path in closure)
    assert all(gate.file_sha256(REPO / path) == digest for path, digest in closure.items())
    revision = ast.parse((REPO / "database/migrations/versions/0115_merchant_payments_foundation.py").read_text())
    assert any(isinstance(n, ast.ImportFrom) and n.module == "backend.payments.models" for n in revision.body)
    root = harness.build_root(tmp_path / "root")
    before = gate.inventory(root)
    planted = root / "backend/payments/unreviewed_guard_probe.py"
    planted.write_text("raise RuntimeError('must never execute')\n")
    assert planted.relative_to(root).as_posix() not in gate.inventory_paths(root)
    assert gate.inventory(root) == before


def test_legacy_guard_digest_is_reproduced_on_the_deployed_checkout_when_available():
    checkout = os.environ.get("NAHLA_LEGACY_462_CHECKOUT", "").strip()
    if not checkout:
        pytest.skip("NAHLA_LEGACY_462_CHECKOUT not set (no checkout of the deployed commit available)")
    root = Path(checkout)
    paths = sorted(set(k.LEGACY_FIXED_FILES) | {p.relative_to(root).as_posix() for p in (root / "database").rglob("*.py")})
    assert gate.digest_paths(root, paths) == (132, "12798863bd12348de4407263fd1a1fe86ef639a0df2bfac2fc94244bd1f714c4")
    assert not (root / k.GATE_RELATIVE_PATH).exists()


def test_source_tamper_and_planted_initializer_are_refused(tmp_path):
    root = harness.build_root(tmp_path / "root")
    count, digest = gate.inventory(root)
    assert gate.check_inventory(root, count, digest)[0].status == k.PASS
    migration = root / "database" / "migrations" / "versions" / "0120_meta_catalog_authorizations.py"
    original = migration.read_bytes()
    migration.write_bytes(original + b"\n")
    check, runtime = gate.check_inventory(root, count, digest)
    assert (check.reason, runtime) == (k.R_INVENTORY_DIGEST_MISMATCH, None)
    migration.write_bytes(original)
    (root / "database" / "migrations" / "versions" / "0124_new.py").write_text("revision = '0124'\n")
    assert gate.check_inventory(root, count, digest)[0].reason == k.R_INVENTORY_COUNT_MISMATCH
    (root / "database" / "migrations" / "versions" / "0124_new.py").unlink()
    (root / "scripts" / "__init__.py").write_text("")
    assert gate.check_inventory(root, count, digest)[0].reason == k.R_INITIALIZER_UNEXPECTED
    (root / "scripts" / "__init__.py").unlink()
    (root / k.EXPECTATIONS_RELATIVE_PATH).unlink()
    assert gate.check_inventory(root, count, digest)[0].reason == k.R_INVENTORY_FILE_MISSING


# ── Rendered bootstrap: trust order ──────────────────────────────────────────

@pytest.fixture
def rendered_root(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    root = harness.build_root(tmp_path / "root")
    approval = harness.approval_for(root, "S0", DB)
    command = harness.rendered(root, "S0", DB, approval)
    env = harness.gate_environment("postgresql://svc:p-not-a-real-secret@db.internal:5432/railway", approval=approval)
    return root, command, env, tmp_path / "marker"


TAMPER_TARGETS = ("scripts/operators/shopify_release_gate_contract.py", "scripts/operators/commerce_runtime_pilot_migration.py",
                  "backend/services/shopify_connection/install_config.py",
                  "database/migrations/versions/0121_shopify_connection_foundation.py", "scripts/operators/__init__.py",
                  "scripts/operators/shopify_release_gate.py", "database/migrations/env.py")


@pytest.mark.parametrize("relative", TAMPER_TARGETS)
def test_bootstrap_refuses_a_tampered_module_before_it_can_execute(rendered_root, relative):
    root, command, env, marker = rendered_root
    original = harness.plant_marker(root / relative, marker)
    try:
        rc, out = harness.run_command(command, env)
    finally:
        (root / relative).write_bytes(original)
    assert rc == 2 and harness.bootstrap(out) == k.R_INVENTORY_DIGEST_MISMATCH, out[-800:]
    assert not marker.exists() and f"{k.LOG_PREFIX} CHECK" not in out and "Traceback" not in out
    assert "p-not-a-real-secret" not in out


def test_bootstrap_refuses_a_planted_initializer_and_an_added_migration(rendered_root):
    root, command, env, marker = rendered_root
    (root / "scripts" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    rc, out = harness.run_command(command, env)
    (root / "scripts" / "__init__.py").unlink()
    assert rc == 2 and harness.bootstrap(out) == k.R_INITIALIZER_UNEXPECTED and not marker.exists() and "CHECK" not in out
    extra = root / "database" / "migrations" / "versions" / "0124_planted.py"
    extra.write_text(f"open({str(marker)!r}, 'w').close()\nrevision = '0124'\ndown_revision = '0123'\n")
    rc, out = harness.run_command(command, env)
    extra.unlink()
    assert rc == 2 and harness.bootstrap(out) == k.R_INVENTORY_COUNT_MISMATCH and not marker.exists() and "CHECK" not in out


def _source_bootstrap(root, gate_source):
    """Run the exact rendered Python payload directly, without asserting Bash compatibility."""
    (root / k.GATE_RELATIVE_PATH).write_text(gate_source, encoding="utf-8")
    approval = harness.approval_for(root, "S0", DB)
    source = render.render_source(
        root, release_sha=harness.SYNTHETIC_RELEASE_SHA, phase="S0", approval_sha256=harness.payload_sha(approval),
        db=DB, backup_service=harness.SYNTHETIC_BACKUP["service"], backup_volume=harness.SYNTHETIC_BACKUP["volume"],
        container_root=root.as_posix(), **harness.SYNTHETIC_IDENTITY)
    env = harness.gate_environment("postgresql://u:p@db.internal/railway", approval=approval)
    # The installed Windows interpreter needs its normal OS directory; this
    # adds no database target or deployment identity to the synthetic process.
    if os.name == "nt" and os.environ.get("SystemRoot"):
        env["SystemRoot"] = os.environ["SystemRoot"]
    return source, env


def _run_source_bootstrap(source, env):
    completed = subprocess.run([sys.executable, "-I", "-c", source], env=env, capture_output=True, text=True, timeout=30)
    return completed.returncode, completed.stdout + completed.stderr


@pytest.mark.parametrize("shadow", ["scripts.py", "scripts.pyc", "sqlalchemy.py"])
def test_direct_bootstrap_refuses_uninventoried_import_shadow_before_its_marker(tmp_path, shadow):
    root = harness.build_root(tmp_path / "root")
    marker = tmp_path / "unverified-import-ran"
    gate_source = "import sqlalchemy\n" if shadow == "sqlalchemy.py" else "from scripts.operators import shopify_release_gate_contract\n"
    source, env = _source_bootstrap(root, gate_source)
    payload = f"open({str(marker)!r}, 'w').close()\n"
    if shadow.endswith(".pyc"):
        temporary = tmp_path / "shadow-source.py"
        temporary.write_text(payload, encoding="utf-8")
        py_compile.compile(str(temporary), cfile=str(root / shadow), doraise=True)
    else:
        (root / shadow).write_text(payload, encoding="utf-8")
    rc, out = _run_source_bootstrap(source, env)
    assert rc == 2 and "BOOTSTRAP PASS" in out and "reason=repository_import_unverified" in out, out[-1500:]
    assert not marker.exists() and "Traceback" not in out


@pytest.mark.parametrize("direct_loader", [False, True])
def test_direct_bootstrap_uses_verified_source_instead_of_unchecked_hash_bytecode(tmp_path, direct_loader):
    root = harness.build_root(tmp_path / "root")
    marker = tmp_path / "unverified-bytecode-ran"
    if direct_loader:
        target = root / "database/migrations/versions/0121_shopify_connection_foundation.py"
        # A synthetic pinned migration isolates the direct-loader control
        # from installed Alembic/SQLAlchemy availability in python -I.
        target.write_text("revision = '0121'\n", encoding="utf-8")
        gate_source = (
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('verified_migration', {str(target)!r})\n"
            "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
            "assert module.revision == '0121'\nprint('VERIFIED_SOURCE_RAN')\n")
        payload = f"open({str(marker)!r}, 'w').close()\nrevision = '0121'\n"
    else:
        target = root / k.CONTRACT_RELATIVE_PATH
        gate_source = "from scripts.operators import shopify_release_gate_contract as k\nassert k.GATE_VERSION == 2\nprint('VERIFIED_SOURCE_RAN')\n"
        payload = f"open({str(marker)!r}, 'w').close()\nGATE_VERSION = 2\n"
    source, env = _source_bootstrap(root, gate_source)
    temporary = tmp_path / "bytecode-source.py"
    temporary.write_text(payload, encoding="utf-8")
    cache = Path(importlib.util.cache_from_source(str(target)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(temporary), cfile=str(cache), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    rc, out = _run_source_bootstrap(source, env)
    assert rc == 0 and "BOOTSTRAP PASS" in out and "VERIFIED_SOURCE_RAN" in out, out[-1500:]
    assert not marker.exists() and "Traceback" not in out


def test_direct_bootstrap_refuses_native_package_initializer_before_loader(tmp_path):
    from importlib.machinery import EXTENSION_SUFFIXES

    root = harness.build_root(tmp_path / "root")
    source, env = _source_bootstrap(root, "from scripts.operators import shopify_release_gate_contract\n")
    (root / "scripts/operators" / ("__init__" + EXTENSION_SUFFIXES[0])).write_bytes(b"unverified native candidate")
    rc, out = _run_source_bootstrap(source, env)
    assert rc == 2 and "BOOTSTRAP PASS" in out and "reason=repository_import_unverified" in out, out[-1500:]
    assert "Traceback" not in out and "DLL load failed" not in out


def test_bootstrap_refuses_wrong_release_identity_libpq_and_payload_before_any_import(rendered_root):
    root, command, env, marker = rendered_root
    # release, identity and libpq are refused before the inventory is even read: tampered modules stay unexecuted
    originals = {relative: harness.plant_marker(root / relative, marker) for relative in TAMPER_TARGETS[:4]}
    try:
        cases = [
            ({**env, k.RELEASE_SHA_ENV: "c" * 40}, k.R_RELEASE_SHA_MISMATCH),
            ({key: value for key, value in env.items() if key != "RAILWAY_PROJECT_ID"}, k.R_IDENTITY_MISMATCH),
            ({**env, "RAILWAY_SERVICE_ID": "99999999-9999-4999-8999-999999999999"}, k.R_IDENTITY_MISMATCH),
            ({**env, "PGHOST": "other.internal"}, k.R_LIBPQ_OVERRIDE),
            ({**env, "PGSERVICE": "prod"}, k.R_LIBPQ_OVERRIDE),
        ]
        for case_env, expected in cases:
            rc, out = harness.run_command(command, case_env)
            assert (rc, harness.bootstrap(out)) == (2, expected), (expected, out[-600:])
            assert not marker.exists() and "CHECK" not in out and "p-not-a-real-secret" not in out
    finally:
        for relative, original in originals.items():
            (root / relative).write_bytes(original)
    # the payload pin is verified after the inventory and before the gate (and its imports) runs
    cases = [
        ({key: value for key, value in env.items() if key != k.APPROVAL_PAYLOAD_ENV}, k.R_APPROVAL_PAYLOAD_MISSING),
        ({**env, k.APPROVAL_PAYLOAD_ENV: "not*base64"}, k.R_APPROVAL_PAYLOAD_MALFORMED),
        ({**env, k.APPROVAL_PAYLOAD_ENV: env[k.APPROVAL_PAYLOAD_ENV][:-4] + "AAA="}, k.R_APPROVAL_PAYLOAD_PIN_MISMATCH),
    ]
    for case_env, expected in cases:
        rc, out = harness.run_command(command, case_env)
        assert (rc, harness.bootstrap(out)) == (2, expected), (expected, out[-600:])
        assert "CHECK" not in out and "p-not-a-real-secret" not in out


def test_bootstrap_hands_over_to_the_gate_only_when_every_pin_holds(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    root = harness.build_root(tmp_path / "root")
    approval = harness.approval_for(root, "S0", UNROUTABLE_DB)
    command = harness.rendered(root, "S0", UNROUTABLE_DB, approval)
    env = harness.gate_environment("postgresql://svc:p-not-a-real-secret@192.0.2.1:5432/railway", approval=approval)
    rc, out = harness.run_command(command, env)
    checks = harness.checks(out)
    assert harness.bootstrap(out) == "PASS" and rc == 1 and harness.result(out) == k.FAIL
    for name in ("root", "release_sha", "inventory", "libraries", "migration_preflight", "graph", "identity", "database_target", "approval",
                 "shopify_flags", "expectations"):
        assert checks[name][0] == k.PASS, (name, checks)
    assert checks["connect"] == (k.FAIL, k.R_CONNECT_FAILED) and "p-not-a-real-secret" not in out and "192.0.2.1" in out
    assert "non-default" in out and str(root) not in out.split("CHECK root")[1].split("\n")[0]


def test_render_round_trips_and_pins_every_literal(tmp_path):
    root = harness.build_root(tmp_path / "root")
    approval = harness.approval_for(root, "S3", DB)
    report = render.inventory_report(root)
    # This is a static production-command round trip, independent of the
    # operator host's executable spelling and filesystem separators.
    command = render.render_command(root, python="python", release_sha=harness.SYNTHETIC_RELEASE_SHA, phase="S3",
                                    approval_sha256=harness.payload_sha(approval), db=DB,
                                    backup_service=harness.SYNTHETIC_BACKUP["service"], backup_volume=harness.SYNTHETIC_BACKUP["volume"],
                                    container_root=k.CONTAINER_ROOT, **harness.SYNTHETIC_IDENTITY)
    source = render.decode_command(command)
    assert source == render.render_source(root, release_sha=harness.SYNTHETIC_RELEASE_SHA, phase="S3", approval_sha256=harness.payload_sha(approval),
                                          db=DB, backup_service=harness.SYNTHETIC_BACKUP["service"], backup_volume=harness.SYNTHETIC_BACKUP["volume"],
                                          container_root=k.CONTAINER_ROOT, **harness.SYNTHETIC_IDENTITY)
    for literal in (report["gate_sha256"], report["inventory_digest"], str(report["inventory_count"]), harness.SYNTHETIC_RELEASE_SHA,
                    harness.payload_sha(approval), '"S3"', DB, *harness.SYNTHETIC_IDENTITY.values(), *harness.SYNTHETIC_BACKUP.values(),
                    '"scripts/__init__.py"', '"scripts/operators/__init__.py"', '"PGHOST"'):
        assert literal in source, literal
    assert "postgresql://" not in source and "password" not in source.lower() and "import scripts" not in source
    imports = {alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Import) for alias in node.names}
    assert imports == {"base64", "hashlib", "os", "sys", "importlib.machinery"}
    assert not any(isinstance(n, ast.ImportFrom) for n in ast.walk(ast.parse(source)))
    argv_match = re.search(r"^ARGV = (\[.*\])$", source, re.M)
    args = gate.build_parser().parse_args(json.loads(argv_match.group(1)))
    assert (args.phase, args.release_sha, args.inventory_digest, args.inventory_count, args.approval_sha256) == (
        "S3", harness.SYNTHETIC_RELEASE_SHA, report["inventory_digest"], str(report["inventory_count"]), harness.payload_sha(approval))
    assert args.root == k.CONTAINER_ROOT
    assert command.startswith('python -I -c "import base64,sys;exec(') and "$" not in command


@pytest.mark.parametrize("kwargs, error", [
    ({"phase": "S1"}, "phase_not_deployable"),
    ({"phase": "S2"}, "phase_not_deployable"),
    ({"phase": "LEGACY_462_S0"}, "unknown_phase"),
    ({"phase": "head"}, "unknown_phase"),
    ({"release_sha": "8308c16e"}, "release_sha_malformed"),
    ({"approval_sha256": "abc"}, "approval_sha_malformed"),
    ({"db": "railway"}, "db_target_malformed"),
    ({"project": "not-a-uuid"}, "identity_malformed"),
    ({"backup_volume": "not-a-uuid"}, "identity_malformed"),
    ({"db": "db.internal:5432/rail'way"}, "token_not_renderable"),
])
def test_render_refuses_malformed_inputs(kwargs, error):
    params = {"release_sha": harness.SYNTHETIC_RELEASE_SHA, "phase": "S0", "approval_sha256": "e" * 64, "db": DB,
              "backup_service": harness.SYNTHETIC_BACKUP["service"], "backup_volume": harness.SYNTHETIC_BACKUP["volume"], **harness.SYNTHETIC_IDENTITY}
    params.update(kwargs)
    with pytest.raises(render.RenderRefusal) as excinfo:
        render.render_source(REPO, **params)
    assert excinfo.value.token == error


def test_render_cli_refuses_an_unapproved_or_unbound_payload_with_closed_tokens(tmp_path):
    root = harness.build_root(tmp_path / "root")
    common = ["--root", str(root), "--release-sha", harness.SYNTHETIC_RELEASE_SHA, "--db", DB, "--project", harness.SYNTHETIC_IDENTITY["project"],
              "--environment", harness.SYNTHETIC_IDENTITY["environment"], "--service", harness.SYNTHETIC_IDENTITY["service"],
              "--backup-service", harness.SYNTHETIC_BACKUP["service"], "--backup-volume", harness.SYNTHETIC_BACKUP["volume"]]

    def run(*args):
        completed = subprocess.run([sys.executable, str(REPO / "scripts" / "operators" / "shopify_release_gate_render.py"), *args],
                                   capture_output=True, text=True, timeout=120)
        return completed.returncode, completed.stdout + completed.stderr

    template = REPO / k.APPROVAL_TEMPLATE_RELATIVE_PATH
    rc, out = run("render", "--phase", "S0", "--approval-file", str(template), *common)
    assert rc == 2 and json.loads(out) == {"error": "approval_" + k.R_APPROVAL_RELEASE_MISMATCH}
    unbound = harness.approval_file(tmp_path / "a", harness.filled_approval(release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=(1, "0" * 64),
                                                                            target=TARGET, phase="S0"))
    rc, out = run("render", "--phase", "S0", "--approval-file", str(unbound), *common)
    assert rc == 2 and json.loads(out) == {"error": "approval_" + k.R_APPROVAL_NOT_BOUND}
    rc, out = run("approval-check", "--phase", "S0", "--approval-file", str(unbound), *common)
    assert rc == 0 and json.loads(out)["reason"] == k.R_APPROVAL_NOT_BOUND and "approval_b64" not in out
    good = harness.approval_for(root, "S0", DB)
    rc, out = run("render", "--phase", "S3", "--approval-file", str(good), *common)
    assert rc == 2 and json.loads(out) == {"error": "approval_" + k.R_APPROVAL_PHASE_MISMATCH}
    rc, out = run("render", "--phase", "S1", "--approval-file", str(good), *common)
    assert rc == 2 and json.loads(out) == {"error": "approval_" + k.R_APPROVAL_PHASE_MISMATCH}
    rc, out = run("render", "--phase", "S0", "--approval-file", str(good), *common, "--format", "source")
    assert rc == 0 and out.startswith("import base64, hashlib, os, sys")
    rc, out = run("render", "--phase", "S0", "--approval-file", str(tmp_path / "missing-p-not-a-real-secret.json"), *common)
    assert rc == 2 and json.loads(out) == {"error": "approval_file_unreadable"} and "p-not-a-real-secret" not in out
    rc, out = run("snapshot", "--database-url", "postgresql://u:p-not-a-real-secret@h:notaport/db", "--out", str(tmp_path / "x.json"),
                  "--source-sha", harness.SYNTHETIC_RELEASE_SHA)
    assert rc == 2 and json.loads(out) == {"error": "database_url_malformed"} and "p-not-a-real-secret" not in out
    rc, out = run("render", "--phase", "S0", "--approval-file", str(good), "--release-sha", "p-not-a-real-secret")
    assert rc == 2 and "p-not-a-real-secret" not in out and json.loads(out)["error"] in ("release_sha_malformed", "invalid_arguments")
    rc, out = run("nonsense")
    assert rc == 2 and json.loads(out) == {"error": "invalid_arguments"}


def test_renderer_never_prints_a_third_party_exception_body(monkeypatch, capsys, tmp_path):
    root = harness.build_root(tmp_path / "root")
    good = harness.approval_for(root, "S0", DB)
    common = ["--root", str(root), "--release-sha", harness.SYNTHETIC_RELEASE_SHA, "--db", DB, "--project", harness.SYNTHETIC_IDENTITY["project"],
              "--environment", harness.SYNTHETIC_IDENTITY["environment"], "--service", harness.SYNTHETIC_IDENTITY["service"],
              "--backup-service", harness.SYNTHETIC_BACKUP["service"], "--backup-volume", harness.SYNTHETIC_BACKUP["volume"]]

    def raiser(*_args, **_kwargs):
        raise ValueError("sentinelcredentiallowercase")

    monkeypatch.setattr(render, "inventory_report", raiser)
    rc = render.main(["render", "--phase", "S0", "--approval-file", str(good), *common])
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out) == {"error": "ValueError"} and "sentinelcredential" not in out and "Traceback" not in out
    rc = render.main(["inventory", "--root", str(root)])
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out) == {"error": "ValueError"} and "sentinelcredential" not in out
    monkeypatch.setattr(render, "load_approval", raiser)
    rc = render.main(["approval-check", "--phase", "S0", "--approval-file", str(good), *common])
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out) == {"error": "ValueError"} and "sentinelcredential" not in out

    class Weird(Exception):
        def __str__(self):
            return "sentinelcredentiallowercase"

    monkeypatch.setattr(render, "load_approval", lambda *_a, **_k: (_ for _ in ()).throw(Weird()))
    rc = render.main(["approval-check", "--phase", "S0", "--approval-file", str(good), *common])
    out = capsys.readouterr().out
    assert rc == 2 and json.loads(out) == {"error": "Weird"} and "sentinelcredential" not in out
    assert render.RenderRefusal("not a token!").token == "refusal"
    with pytest.raises(render.RenderRefusal) as excinfo:
        render.snapshot("postgresql://u:sentinelcredentiallowercase@h:notaport/db", source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert excinfo.value.token == "database_url_malformed"


@pytest.mark.parametrize("query", ["host=postgres-ancu.railway.internal", "hostaddr=192.0.2.1", "dbname=railway", "port=5432", "service=hidden"])
def test_snapshot_refuses_target_query_before_connection(monkeypatch, capsys, query):
    for name in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    calls = []
    monkeypatch.setattr(gate, "_engine", lambda url: calls.append(url))
    with pytest.raises(render.RenderRefusal) as failure:
        render.snapshot("postgresql://u:sentinelcredentiallowercase@disposable.internal/test?" + query,
                        source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert failure.value.token == "database_url_malformed" and calls == []
    assert "sentinelcredential" not in capsys.readouterr().out


@pytest.mark.parametrize("name", k.LIBPQ_TARGET_ENV_VARS)
def test_snapshot_refuses_inherited_target_before_connection(monkeypatch, name):
    for variable in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv(name, "sentinel-target")
    calls = []
    monkeypatch.setattr(gate, "_engine", lambda url: calls.append(url))
    with pytest.raises(render.RenderRefusal) as failure:
        render.snapshot("postgresql://u:p@disposable.internal/test", source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert failure.value.token == "snapshot_environment_override" and calls == []


@pytest.mark.parametrize("host", ["unreachable.invalid,postgres-ancu.railway.internal",
                                  "postgres-ancu.railway.internal,unreachable.invalid",
                                  "disposable-a.invalid,disposable-b.invalid", ",disposable.invalid", "disposable.invalid,"])
def test_snapshot_refuses_every_host_list_before_read_engine_construction(monkeypatch, host):
    for variable in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    calls = []
    monkeypatch.setattr(gate, "_engine", lambda url: calls.append(url))
    with pytest.raises(render.RenderRefusal, match="snapshot_multiple_hosts"):
        render.snapshot("postgresql://u:p@" + host + "/test", source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert calls == []


def test_snapshot_refuses_existing_host_with_dns_root_dot_without_connection(monkeypatch):
    for variable in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(variable, raising=False)
    calls = []
    monkeypatch.setattr(gate, "_engine", lambda url: calls.append(url))
    with pytest.raises(render.RenderRefusal, match="refusing_existing_platform_database"):
        render.snapshot("postgresql://u:p@postgres-ancu.railway.internal./railway", source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert calls == []


def _distribution_metadata(site, name, version, module=None):
    info = site / (name.replace("-", "_") + "-" + version + ".dist-info")
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    if module:
        origin = site / module / "__init__.py"
        origin.parent.mkdir()
        origin.write_text(f"__version__ = {version!r}\n", encoding="utf-8")
        (info / "RECORD").write_text(f"{module}/__init__.py,,\n", encoding="utf-8")


def test_repository_distribution_metadata_cannot_mask_mismatched_installed_pins(tmp_path, monkeypatch):
    root, site = tmp_path / "repository", tmp_path / "installed-site"
    for name, pinned in k.LIBRARY_PINS.items():
        _distribution_metadata(root, name, pinned)
        _distribution_metadata(site, name, "0.0")
    monkeypatch.syspath_prepend(str(root))
    # Demonstrate the vulnerable unrestricted discovery sees the advertised
    # repository versions, while the guarded discovery must refuse them.
    assert {name: gate.importlib.metadata.version(name) for name in k.LIBRARY_PINS} == dict(k.LIBRARY_PINS)
    monkeypatch.setattr(gate.sysconfig, "get_paths", lambda: {"purelib": str(site), "platlib": str(site)})
    check = gate.check_libraries(root)
    assert check.reason == k.R_LIBRARY_PIN_MISMATCH
    assert check.detail == {"mismatched": sorted(k.LIBRARY_PINS)}


@pytest.mark.parametrize("defect", ["foreign_origin", "wrong_imported_version", "missing_record"])
def test_library_pin_is_bound_to_recorded_import_origin_and_actual_version(tmp_path, monkeypatch, defect):
    root, site = tmp_path / "repository", tmp_path / "installed-site"
    names = {"alembic": "alembic", "sqlalchemy": "sqlalchemy", "psycopg2-binary": "psycopg2"}
    for name, pinned in k.LIBRARY_PINS.items():
        _distribution_metadata(site, name, pinned, names[name])
    monkeypatch.setattr(gate.sysconfig, "get_paths", lambda: {"purelib": str(site), "platlib": str(site)})
    origins = {name: site / name / "__init__.py" for name in names.values()}
    versions = {names[name]: pinned for name, pinned in k.LIBRARY_PINS.items()}
    imported = []
    monkeypatch.setattr(gate.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origins[name])))

    def load(name):
        imported.append(name)
        return SimpleNamespace(__file__=str(origins[name]), __version__=versions[name])

    monkeypatch.setattr(gate.importlib, "import_module", load)
    assert gate.check_libraries(root).status == k.PASS
    imported.clear()
    if defect == "foreign_origin":
        origins["sqlalchemy"] = root / "sqlalchemy.py"
    elif defect == "wrong_imported_version":
        versions["sqlalchemy"] = "0.0"
    else:
        next(site.glob("sqlalchemy-*.dist-info/RECORD")).unlink()
    check = gate.check_libraries(root)
    assert check.reason == k.R_LIBRARY_PIN_MISMATCH and check.detail == {"mismatched": ["sqlalchemy"]}
    if defect != "wrong_imported_version":
        assert "sqlalchemy" not in imported


def test_snapshot_uses_only_the_validated_explicit_target_and_refuses_current_host(monkeypatch):
    for name in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    calls = []

    def stop(url):
        calls.append(url)
        raise RuntimeError("no connection in this control")

    monkeypatch.setattr(gate, "_engine", stop)
    with pytest.raises(render.RenderRefusal, match="refusing_existing_platform_database"):
        render.snapshot("postgresql://u:p@postgres-ancu.railway.internal/railway", source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert calls == []
    with pytest.raises(render.RenderRefusal, match="snapshot_runtimeerror"):
        render.snapshot("postgresql://u:p@DISPOSABLE.internal/test", source_sha=harness.SYNTHETIC_RELEASE_SHA)
    assert calls == ["postgresql://u:p@disposable.internal:5432/test"]


# ── Pure evaluations ─────────────────────────────────────────────────────────

def test_rows_exact_multiset_fail_closed_and_unknown_rows_are_never_echoed():
    s0 = k.DEPLOY_PHASES["S0"]["rows"]
    assert k.evaluate_rows(["0120", "0111", "0113"], s0, KNOWN)[0] == k.PASS
    assert k.evaluate_rows(None, s0, KNOWN)[1] == k.R_VERSION_TABLE_MISSING
    assert k.evaluate_rows(["0111", "0113", "0120", "0120"], s0, KNOWN)[1] == k.R_VERSION_ROWS_DUPLICATE
    for rows in (["0111", "0113"], ["0111", "0113", "0121"], ["0111", "0113", "0122"], ["0111", "0113", "0120", "0121"], ["0093"], []):
        assert k.evaluate_rows(rows, s0, KNOWN)[1] == k.R_VERSION_ROWS_MISMATCH, rows
    for rows in (["0111", "0113", "0121"], ["0111", "0113", "0122"], ["0111", "0113", "0120"]):
        assert k.evaluate_rows(rows, k.DEPLOY_PHASES["S3"]["rows"], KNOWN)[1] == k.R_VERSION_ROWS_MISMATCH, rows
    status, reason, detail = k.evaluate_rows(["0111", "0113", "0120", SECRET_ROW, CONTROL_ROW], s0, KNOWN)
    assert (status, reason) == (k.FAIL, k.R_VERSION_ROWS_UNKNOWN)
    rendered = json.dumps(detail)
    assert "p-not-a-real-secret" not in rendered and "DROP TABLE" not in rendered and "\x1b" not in rendered
    assert detail["rows"] == ["0111", "0113", "0120", k.UNKNOWN, k.UNKNOWN] and detail["unknown_row_count"] == 2


@pytest.mark.parametrize("rows", [("0111", "0113", "0124"), ("0111", "0113", "0125"),
                                 ("0111", "0113", "0124", "0125")])
def test_future_shopify_and_google_database_states_never_become_deployment_states(rows):
    for phase in k.DEPLOY_PHASES.values():
        # Even if all future revision IDs are known, the exact approved state remains closed.
        assert k.evaluate_rows(list(rows), phase["rows"], KNOWN | {"0125"})[:2] == (k.FAIL, k.R_VERSION_ROWS_MISMATCH)


def _observed_s3(**overrides):
    observed = {"tables": [f"public.{t}" for t in k.SHOPIFY_TABLES], "stray_count": 0,
                "definitions": copy.deepcopy(EXPECTATIONS["shopify_s3"]["tables"]),
                "sequences": copy.deepcopy(EXPECTATIONS["shopify_s3"]["sequences"])}
    observed.update(overrides)
    return observed


def test_footprint_pins_the_exact_shopify_sequence_set_with_ownership():
    s0, s3, pinned = k.DEPLOY_PHASES["S0"], k.DEPLOY_PHASES["S3"], EXPECTATIONS["shopify_s3"]
    # fourteen serial keys: every Shopify table except shopify_shop_leases, which is keyed without a sequence
    owners = {row[1].split(".", 1)[1] for row in pinned["sequences"]}
    assert len(pinned["sequences"]) == 14 and owners == set(k.SHOPIFY_TABLES) - {"shopify_shop_leases"}
    assert {tuple(s) for s in pinned["sequences"]} == {(f"public.{t}_id_seq", f"public.{t}", "id") for t in owners}
    assert k.evaluate_footprint(_observed_s3(), s3, pinned)[0] == k.PASS
    status, reason, detail = k.evaluate_footprint({"tables": [], "stray_count": 0, "definitions": {}, "sequences": [["public.shopify_planted_seq", None, None]]}, s0, pinned)
    assert (reason, detail["unexpected_count"]) == (k.R_FOOTPRINT_UNEXPECTED, 1) and "planted" not in json.dumps(detail)
    extra_owned = _observed_s3(sequences=pinned["sequences"] + [["public.shopify_planted", "public.shopify_connections", "id"]])
    status, reason, detail = k.evaluate_footprint(extra_owned, s3, pinned)
    assert (reason, detail["unexpected_count"]) == (k.R_FOOTPRINT_UNEXPECTED, 1) and "planted" not in json.dumps(detail)
    extra_unnamed = _observed_s3(sequences=pinned["sequences"] + [["public.planted_seq", "public.shopify_connections", "generation"]])
    assert k.evaluate_footprint(extra_unnamed, s3, pinned)[1] == k.R_FOOTPRINT_UNEXPECTED
    other_schema = _observed_s3(sequences=pinned["sequences"] + [["other.shopify_connections_id_seq", None, None]])
    assert k.evaluate_footprint(other_schema, s3, pinned)[1] == k.R_FOOTPRINT_UNEXPECTED
    re_owned = _observed_s3(sequences=[[n, "public.shopify_shop_leases" if n == "public.shopify_connections_id_seq" else t, c] for n, t, c in pinned["sequences"]])
    status, reason, detail = k.evaluate_footprint(re_owned, s3, pinned)
    assert (reason, detail["drift"]) == (k.R_SHOPIFY_DEFINITION_DRIFT, ["public.shopify_connections_id_seq"])
    unowned = _observed_s3(sequences=[[n, None, None] if n == "public.shopify_connections_id_seq" else [n, t, c] for n, t, c in pinned["sequences"]])
    assert k.evaluate_footprint(unowned, s3, pinned)[2]["drift"] == ["public.shopify_connections_id_seq"]
    missing = _observed_s3(sequences=pinned["sequences"][1:])
    status, reason, detail = k.evaluate_footprint(missing, s3, pinned)
    assert reason == k.R_FOOTPRINT_MISSING and detail["missing"] == [pinned["sequences"][0][0]]


def test_footprint_is_exact_by_canonical_definition_and_reports_only_expected_names():
    s0, s3, pinned = k.DEPLOY_PHASES["S0"], k.DEPLOY_PHASES["S3"], EXPECTATIONS["shopify_s3"]
    assert k.evaluate_footprint({"tables": [], "stray_count": 0, "definitions": {}, "sequences": []}, s0, pinned)[0] == k.PASS
    assert k.evaluate_footprint(_observed_s3(), s3, pinned)[0] == k.PASS
    assert k.evaluate_footprint(_observed_s3(), s0, pinned)[1] == k.R_FOOTPRINT_UNEXPECTED
    assert k.evaluate_footprint({"tables": [], "stray_count": 0, "definitions": {}, "sequences": []}, s3, pinned)[1] == k.R_FOOTPRINT_MISSING
    status, reason, detail = k.evaluate_footprint({"tables": ["public.shopify_connections"], "stray_count": 0, "definitions": {}, "sequences": []}, s0, pinned)
    assert (reason, detail["unexpected_count"]) == (k.R_FOOTPRINT_UNEXPECTED, 1)
    status, reason, detail = k.evaluate_footprint(_observed_s3(tables=[f"public.{t}" for t in k.SHOPIFY_TABLES] + ["other.shopify_p-not-a-real-secret"]), s3, pinned)
    assert (reason, detail["unexpected_count"]) == (k.R_FOOTPRINT_UNEXPECTED, 1) and "secret" not in json.dumps(detail)
    status, reason, detail = k.evaluate_footprint(_observed_s3(stray_count=2), s3, pinned)
    assert (reason, detail["stray_count"]) == (k.R_FOOTPRINT_UNEXPECTED, 2)
    partial = _observed_s3(tables=[f"public.{t}" for t in k.SHOPIFY_TABLES[:-1]])
    status, reason, detail = k.evaluate_footprint(partial, s3, pinned)
    assert (reason, detail["missing"]) == (k.R_FOOTPRINT_MISSING, [k.SHOPIFY_TABLES[-1]])
    wrong_shape = _observed_s3()
    wrong_shape["definitions"]["shopify_privacy_request_events"] = {"columns": [], "constraints": [], "indexes": []}
    status, reason, detail = k.evaluate_footprint(wrong_shape, s3, pinned)
    assert (reason, detail["drift"]) == (k.R_SHOPIFY_DEFINITION_DRIFT, ["shopify_privacy_request_events"])
    wrong_key = _observed_s3()
    for constraint in wrong_key["definitions"]["shopify_connections"]["constraints"]:
        if constraint[0] == k.CONNECTION_KEY_CONSTRAINT:
            constraint[2] = "UNIQUE (id, generation)"
    status, reason, detail = k.evaluate_footprint(wrong_key, s3, pinned)
    assert (reason, detail["drift"]) == (k.R_SHOPIFY_DEFINITION_DRIFT, ["shopify_connections"]) and "generation" not in json.dumps(detail)
    invalid_index = _observed_s3()
    invalid_index["definitions"]["shopify_oauth_states"]["indexes"][0][2] = False
    assert k.evaluate_footprint(invalid_index, s3, pinned)[2]["drift"] == ["shopify_oauth_states"]


def test_retained_definitions_are_compared_by_definition_and_reported_by_name_only():
    retained = EXPECTATIONS["retained"]
    observed = copy.deepcopy(retained)
    assert k.evaluate_retained(observed, retained)[0] == k.PASS
    drifted = copy.deepcopy(observed)
    table = "commerce_runtime_handover_barrier"
    constraint = next(c for c in drifted["tables"][table]["constraints"] if c[1] == "c")
    constraint[2] = constraint[2].replace(">= 0", ">= 1")
    status, reason, detail = k.evaluate_retained(drifted, retained)
    assert (status, reason, detail["drift"]) == (k.FAIL, k.R_RETAINED_DEFINITION_DRIFT, [table]) and ">= 1" not in json.dumps(detail)
    drifted = copy.deepcopy(observed)
    drifted["tables"]["catalog_channel_retirements"]["indexes"][0][1] = drifted["tables"]["catalog_channel_retirements"]["indexes"][0][1].replace("USING btree", "USING hash")
    assert k.evaluate_retained(drifted, retained)[2]["drift"] == ["catalog_channel_retirements"]
    drifted = copy.deepcopy(observed)
    drifted["tables"]["catalog_channel_retirements"]["indexes"][0][2] = False  # indisvalid
    assert k.evaluate_retained(drifted, retained)[2]["drift"] == ["catalog_channel_retirements"]
    drifted = copy.deepcopy(observed)
    drifted["tables"]["meta_catalog_authorizations"]["columns"][0][1] = "bigint"
    assert k.evaluate_retained(drifted, retained)[2]["drift"] == ["meta_catalog_authorizations"]
    drifted = copy.deepcopy(observed)
    del drifted["tables"]["commerce_runtime_navigation_snapshots"]
    assert k.evaluate_retained(drifted, retained)[1] == k.R_RETAINED_TABLE_MISSING
    drifted = copy.deepcopy(observed)
    drifted["nonce"]["indexes"] = [s for s in drifted["nonce"]["indexes"] if "expires_at" not in s[1]]
    assert k.evaluate_retained(drifted, retained)[2]["drift"] == [k.NONCE_TABLE]
    assert observed["nonce"]["columns"][-1][0] == "created_at"
    assert observed["nonce"]["columns"][-1][3] is None
    for added_default in ("CURRENT_TIMESTAMP", "now()", "'2000-01-01'::timestamp with time zone"):
        drifted = copy.deepcopy(observed)
        drifted["nonce"]["columns"][-1][3] = added_default
        status, reason, detail = k.evaluate_retained(drifted, retained)
        assert (status, reason, detail["drift"]) == (k.FAIL, k.R_RETAINED_DEFINITION_DRIFT, [k.NONCE_TABLE])
    drifted = copy.deepcopy(observed)
    drifted["nonce"]["constraints"][0][4] = False  # convalidated
    assert k.evaluate_retained(drifted, retained)[2]["drift"] == [k.NONCE_TABLE]
    drifted = copy.deepcopy(observed)
    drifted["nonce"] = None
    assert k.evaluate_retained(drifted, retained)[1] == k.R_RETAINED_TABLE_MISSING
    drifted = copy.deepcopy(observed)
    drifted["pk_types"]["tenants"] = "bigint"
    assert k.evaluate_retained(drifted, retained)[2]["identity"] == ["tenants"]
    drifted = copy.deepcopy(observed)
    drifted["unique_definitions"] = []
    assert k.evaluate_retained(drifted, retained)[2]["identity"] == ["commerce_runtime_conversations"]
    drifted = copy.deepcopy(observed)
    drifted["sequences"][0][2] = "other_column"
    assert k.evaluate_retained(drifted, retained)[2]["identity"] == ["sequence_ownership"]


def test_relation_identity_must_hold_across_the_run_and_names_only_from_the_allow_list():
    before = {"tenants": 16400, "whatsapp_oauth_nonces": 16500, "shopify_connections": 16600}
    assert k.evaluate_relation_identity(before, dict(before))[0] == k.PASS
    status, reason, detail = k.evaluate_relation_identity(before, {**before, "whatsapp_oauth_nonces": 16999})
    assert (status, reason, detail) == (k.FAIL, k.R_RELATION_REPLACED, {"relations": ["whatsapp_oauth_nonces"]})
    status, reason, detail = k.evaluate_relation_identity(before, {**before, "p-not-a-real-secret": 1})
    assert status == k.FAIL and "secret" not in json.dumps(detail)


def test_expectations_file_is_bound_to_these_migrations_and_covers_retained_and_s3_relations():
    assert EXPECTATIONS["expectations_version"] == k.EXPECTATIONS_VERSION
    assert EXPECTATIONS["database_py_digest"] == gate.database_py_digest(REPO)
    retained, shopify = EXPECTATIONS["retained"], EXPECTATIONS["shopify_s3"]
    assert set(retained["tables"]) == set(k.RETAINED_FULL_DEFINITION_TABLES)
    assert [sum(len(d[key]) for d in retained["tables"].values()) for key in ("columns", "constraints", "indexes")] == [90, 33, 18]
    assert set(shopify["tables"]) == set(k.SHOPIFY_TABLES) and len(shopify["sequences"]) == 14
    assert all(len(row) == 3 and row[0].startswith("public.shopify_") and row[1].startswith("public.shopify_") and row[2] == "id" for row in shopify["sequences"])
    all_tables = list(retained["tables"].values()) + list(shopify["tables"].values())
    assert all(len(c) == 6 and c[1] in "pufc" and c[2].startswith(("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY")) for d in all_tables for c in d["constraints"])
    assert all(len(i) == 5 and i[1].startswith("CREATE ") and " USING " in i[1] and i[2] is True and i[3] is True and i[4] is True for d in all_tables for i in d["indexes"])
    assert all(len(col) == 6 for d in all_tables for col in d["columns"])
    assert any(c[0] == k.CONNECTION_KEY_CONSTRAINT and c[2] == "UNIQUE (id, tenant_id)" for c in shopify["tables"]["shopify_connections"]["constraints"])
    nonce = retained["nonce"]
    assert len(nonce["columns"]) == 8 and all(len(c) == 6 for c in nonce["columns"])
    assert nonce["columns"][-1] == ["created_at", "timestamp with time zone", True, None, "", ""]
    assert all(len(c) == 5 and c[0] in "pufc" for c in nonce["constraints"]) and any(c[1] == "UNIQUE (nonce_hash)" for c in nonce["constraints"])
    assert all(len(i) == 5 and i[2] is True and i[3] is True for i in nonce["indexes"])
    assert retained["pk_types"] == {"tenants": "integer", "users": "integer"}
    assert retained["unique_definitions"] == [["commerce_runtime_conversations", "UNIQUE (id, tenant_id, namespace)"]]
    assert sorted(s[1] for s in retained["sequences"]) == sorted(t for t in k.RETAINED_FULL_DEFINITION_TABLES if t != "commerce_runtime_handover_barrier")
    assert "nahla_password" not in json.dumps(EXPECTATIONS) and "postgresql://" not in json.dumps(EXPECTATIONS)
    check, loaded = gate.check_expectations(REPO)
    assert check.status == k.PASS and loaded is not None


@pytest.mark.parametrize("name", k.SHOPIFY_FLAG_ENVS)
@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_every_shopify_flag_including_the_dashboard_ones_must_be_off(name, value):
    status, reason, detail = k.evaluate_flags({name: value})
    assert (status, reason, detail["enabled"]) == (k.FAIL, k.R_FLAG_ENABLED, [name])


def test_falsey_or_unset_flags_pass():
    assert k.evaluate_flags({})[0] == k.PASS
    assert k.evaluate_flags({name: "0" for name in k.SHOPIFY_FLAG_ENVS})[0] == k.PASS
    assert k.evaluate_flags({"NAHLA_SHOPIFY_CONNECTION_ENABLED": "false", "VITE_SHOPIFY_CONNECTION_UI": "no"})[0] == k.PASS


def test_identity_requires_every_platform_id_and_never_prints_actual_values():
    expected = dict(harness.SYNTHETIC_IDENTITY)
    env = {k.IDENTITY_ENVS[key]: value for key, value in expected.items()}
    assert k.evaluate_identity(env, expected)[0] == k.PASS
    status, reason, detail = k.evaluate_identity({**env, "RAILWAY_SERVICE_ID": "leaked-actual-value"}, expected)
    assert (status, reason, detail) == (k.FAIL, k.R_IDENTITY_MISMATCH, {"mismatched": ["service"]})
    status, reason, detail = k.evaluate_identity({key: v for key, v in env.items() if key != "RAILWAY_PROJECT_ID"}, expected)
    assert (status, reason, detail) == (k.FAIL, k.R_IDENTITY_MISSING, {"missing": ["project"]})


def test_database_target_goes_through_the_pilot_job_validators():
    expected = k.parse_db_target(DB)
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
        assert (check.reason, check.detail) == (k.R_LIBPQ_OVERRIDE, {"variables": [variable]}), variable


def test_parse_helpers_fail_closed_on_malformed_input():
    assert k.parse_utc("2026-10-11T16:54:10.145Z") == _dt.datetime(2026, 10, 11, 16, 54, 10, 145000, tzinfo=_dt.timezone.utc)
    assert k.parse_utc("2026-10-11T16:54:10Z").tzinfo is _dt.timezone.utc
    for bad in ("2026-10-11T16:54:10", "2026-10-11T16:54:10+03:00", "2026-10-11 16:54:10Z", "", None, "yesterday", "2026-13-11T16:54:10Z"):
        assert k.parse_utc(bad) is None
    assert k.parse_db_target("Host.Internal:5433/Rail way") == {"host": "host.internal", "port": 5433, "database": "Rail way"}
    assert k.parse_db_target("db.internal/railway")["port"] == 5432
    for bad in ("", "railway", "db.internal:x/railway", "/railway", "db.internal/", "a/b/c", " db.internal:5432/railway"):
        assert k.parse_db_target(bad) is None


# ── Approval payload ─────────────────────────────────────────────────────────

def _record(record_phase="S0", **changes):
    record = harness.filled_approval(release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=RUNTIME, target=TARGET, phase=record_phase, now=NOW)
    for path, value in changes.items():
        target = record
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    return record


def _verdict(record, phase="S0", *, now=NOW, runtime=RUNTIME, release=harness.SYNTHETIC_RELEASE_SHA, target=TARGET,
             backup=harness.SYNTHETIC_BACKUP):
    return k.evaluate_approval(record, phase_name=phase, release_sha=release, runtime_digest=runtime[1], runtime_count=runtime[0],
                               target=target, backup_service=backup["service"], backup_volume=backup["volume"], now=now)[1]


def test_shipped_template_is_unfilled_and_is_not_part_of_the_inventory():
    template = json.loads((REPO / k.APPROVAL_TEMPLATE_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert template["record_version"] == k.APPROVAL_RECORD_VERSION and template["release_sha"] is None and template["phase"] is None
    assert all(v is None for v in template["target"].values())
    assert template["approval"]["approved"] is False and all(template["approval"][f] is None for f in k.APPROVAL_FIELDS[1:])
    assert all(v is None for v in template["deployment_rollback_snapshot"].values())
    backup = template["db_recovery_evidence"]["backup"]
    assert backup["completed"] is False and backup["restore_available"] is False and all(backup[f] is None for f in k.BACKUP_FIELDS if f not in ("completed", "restore_available"))
    assert template["db_recovery_evidence"]["restore_verified"] is False
    assert k.APPROVAL_TEMPLATE_RELATIVE_PATH not in gate.inventory_paths(REPO)
    assert not (REPO / "scripts" / "operators" / "shopify_release_approval.json").exists()
    assert _verdict(template) == k.R_APPROVAL_RELEASE_MISMATCH
    filled = dict(template, release_sha=harness.SYNTHETIC_RELEASE_SHA, runtime_inventory_digest=RUNTIME[1], runtime_inventory_count=RUNTIME[0],
                  target=dict(TARGET), phase="S0")
    assert _verdict(filled) == k.R_APPROVAL_MISSING


def test_valid_payload_passes_for_its_phase_only():
    assert _verdict(_record("S0")) is None and _verdict(_record("S3"), "S3") is None
    assert _verdict(_record("S0"), "S3") == k.R_APPROVAL_PHASE_MISMATCH and _verdict(_record("S3"), "S0") == k.R_APPROVAL_PHASE_MISMATCH


@pytest.mark.parametrize("changes, expected", [
    ({"record_version": 1}, k.R_APPROVAL_VERSION),
    ({"release_sha": "c" * 40}, k.R_APPROVAL_RELEASE_MISMATCH),
    ({"runtime_inventory_digest": "e" * 64}, k.R_APPROVAL_NOT_BOUND),
    ({"runtime_inventory_count": 142}, k.R_APPROVAL_NOT_BOUND),
    ({"target.db": "other.internal:5432/railway"}, k.R_APPROVAL_TARGET_MISMATCH),
    ({"target.service": "99999999-9999-4999-8999-999999999999"}, k.R_APPROVAL_TARGET_MISMATCH),
    ({"target": None}, k.R_APPROVAL_TARGET_MISMATCH),
    ({"phase": "S1"}, k.R_APPROVAL_PHASE_MISMATCH),
    ({"approval.approved": False}, k.R_APPROVAL_MISSING),
    ({"approval.reviewer": " "}, k.R_APPROVAL_MISSING),
    ({"approval.independent_review_ref": None}, k.R_APPROVAL_MISSING),
    ({"approval.approved_at_utc": "2026-10-11 17:00:00"}, k.R_APPROVAL_TIMESTAMP_INVALID),
    ({"approval.valid_until_utc": harness.iso(NOW - _dt.timedelta(hours=2))}, k.R_APPROVAL_TIMESTAMP_ORDER),
    ({"deployment_rollback_snapshot.railway_deployment_id": None}, k.R_ROLLBACK_SNAPSHOT_MISSING),
    ({"deployment_rollback_snapshot.railway_deployment_id": "deployment-12"}, k.R_ROLLBACK_SNAPSHOT_MISSING),
    ({"deployment_rollback_snapshot.source_sha": "462ede1a"}, k.R_ROLLBACK_SNAPSHOT_MISSING),
    ({"deployment_rollback_snapshot.verified_by": " "}, k.R_ROLLBACK_SNAPSHOT_MISSING),
    ({"deployment_rollback_snapshot.verified_redeployable_at_utc": "soon"}, k.R_APPROVAL_TIMESTAMP_INVALID),
    ({"deployment_rollback_snapshot.verified_redeployable_at_utc": harness.iso(NOW - _dt.timedelta(minutes=30))}, k.R_APPROVAL_TIMESTAMP_ORDER),
    ({"deployment_rollback_snapshot.verified_redeployable_at_utc": harness.iso(NOW + _dt.timedelta(hours=5))}, k.R_APPROVAL_TIMESTAMP_ORDER),
    ({"db_recovery_evidence.backup.card_timestamp_utc": None}, k.R_BACKUP_EVIDENCE_MISSING),
    ({"db_recovery_evidence.backup.observed_by": ""}, k.R_BACKUP_EVIDENCE_MISSING),
    ({"db_recovery_evidence.backup.service_id": "77777777-7777-4777-8777-777777777777"}, k.R_BACKUP_IDENTITY_MISMATCH),
    ({"db_recovery_evidence.backup.volume_id": "77777777-7777-4777-8777-777777777777"}, k.R_BACKUP_IDENTITY_MISMATCH),
    ({"db_recovery_evidence.backup.size_mb": 0}, k.R_BACKUP_SIZE_INVALID),
    ({"db_recovery_evidence.backup.size_mb": -5}, k.R_BACKUP_SIZE_INVALID),
    ({"db_recovery_evidence.backup.size_mb": "647"}, k.R_BACKUP_SIZE_INVALID),
    ({"db_recovery_evidence.backup.size_mb": True}, k.R_BACKUP_SIZE_INVALID),
    ({"db_recovery_evidence.backup.size_mb": 10 ** 9}, k.R_BACKUP_SIZE_INVALID),
    ({"db_recovery_evidence.backup.completed": False}, k.R_BACKUP_NOT_COMPLETED),
    ({"db_recovery_evidence.backup.completed": "true"}, k.R_BACKUP_NOT_COMPLETED),
    ({"db_recovery_evidence.backup.restore_available": False}, k.R_BACKUP_NOT_RESTORABLE),
    ({"db_recovery_evidence.backup.card_timestamp_utc": "2026-10-11T16:54:10.145"}, k.R_APPROVAL_TIMESTAMP_INVALID),
    ({"db_recovery_evidence.backup.observed_at_utc": "later"}, k.R_APPROVAL_TIMESTAMP_INVALID),
    ({"db_recovery_evidence.backup.card_timestamp_utc": harness.iso(NOW - _dt.timedelta(minutes=70))}, k.R_APPROVAL_TIMESTAMP_ORDER),
    ({"db_recovery_evidence.backup.observed_at_utc": harness.iso(NOW - _dt.timedelta(minutes=30))}, k.R_APPROVAL_TIMESTAMP_ORDER),
    ({"db_recovery_evidence.backup.observed_at_utc": harness.iso(NOW + _dt.timedelta(hours=1))}, k.R_APPROVAL_TIMESTAMP_ORDER),
    ({"db_recovery_evidence.backup.card_timestamp_utc": harness.iso(NOW - _dt.timedelta(days=30))}, k.R_BACKUP_TOO_OLD),
    ({"db_recovery_evidence.backup.card_timestamp_utc": harness.iso(NOW - _dt.timedelta(hours=73))}, k.R_BACKUP_TOO_OLD),
])
def test_payload_refusals(changes, expected):
    assert _verdict(_record("S0", **changes)) == expected


def test_payload_time_windows_and_freshness_at_run_time():
    assert _verdict(_record("S0"), now=NOW + _dt.timedelta(days=3)) == k.R_APPROVAL_EXPIRED
    assert _verdict(_record("S0"), now=NOW - _dt.timedelta(days=1)) == k.R_APPROVAL_EXPIRED
    # fresh at approval, stale at run time: the backup window is bounded at both moments
    record = _record("S0", **{"approval.valid_until_utc": harness.iso(NOW + _dt.timedelta(days=10))})
    assert _verdict(record, now=NOW + _dt.timedelta(hours=71)) == k.R_BACKUP_TOO_OLD
    assert _verdict(record, now=NOW + _dt.timedelta(hours=60)) is None
    future = _record("S0", **{"approval.approved_at_utc": harness.iso(NOW + _dt.timedelta(hours=2)),
                             "db_recovery_evidence.backup.observed_at_utc": harness.iso(NOW + _dt.timedelta(hours=1)),
                             "db_recovery_evidence.restore_verified_at_utc": harness.iso(NOW + _dt.timedelta(hours=1))})
    assert _verdict(future) == k.R_APPROVAL_EXPIRED
    assert _verdict("not a record") == k.R_APPROVAL_UNREADABLE and _verdict([]) == k.R_APPROVAL_UNREADABLE


def test_schema_changing_phase_requires_a_verified_restore_between_backup_and_approval():
    unverified = harness.filled_approval(release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=RUNTIME, target=TARGET, phase="S3", now=NOW, restore_verified=False)
    assert _verdict(unverified, "S3") == k.R_RESTORE_NOT_VERIFIED
    unverified_s0 = harness.filled_approval(release_sha=harness.SYNTHETIC_RELEASE_SHA, inventory=RUNTIME, target=TARGET, phase="S0", now=NOW, restore_verified=False)
    assert _verdict(unverified_s0, "S0") is None
    assert _verdict(_record("S3", **{"db_recovery_evidence.restore_verification_ref": None}), "S3") == k.R_RESTORE_NOT_VERIFIED
    assert _verdict(_record("S3", **{"db_recovery_evidence.restore_verified_at_utc": "yesterday"}), "S3") == k.R_APPROVAL_TIMESTAMP_INVALID
    assert _verdict(_record("S3", **{"db_recovery_evidence.restore_verified_at_utc": harness.iso(NOW - _dt.timedelta(hours=3))}), "S3") == k.R_APPROVAL_TIMESTAMP_ORDER
    assert _verdict(_record("S3", **{"db_recovery_evidence.restore_verified_at_utc": harness.iso(NOW - _dt.timedelta(minutes=10))}), "S3") == k.R_APPROVAL_TIMESTAMP_ORDER
    assert _verdict(_record("S3", **{"db_recovery_evidence.restore_verified_at_utc": harness.iso(NOW + _dt.timedelta(hours=1))}), "S3") == k.R_APPROVAL_TIMESTAMP_ORDER


def test_approval_payload_reading_is_pinned_and_closed():
    record = _record("S0")
    raw = json.dumps(record).encode()
    pin = hashlib.sha256(raw).hexdigest()
    b64 = __import__("base64").b64encode(raw).decode()
    assert gate.read_approval_payload({k.APPROVAL_PAYLOAD_ENV: b64}, pin) == (record, None)
    assert gate.read_approval_payload({}, pin) == (None, k.R_APPROVAL_PAYLOAD_MISSING)
    assert gate.read_approval_payload({k.APPROVAL_PAYLOAD_ENV: "%%%"}, pin) == (None, k.R_APPROVAL_PAYLOAD_MALFORMED)
    assert gate.read_approval_payload({k.APPROVAL_PAYLOAD_ENV: b64}, "0" * 64) == (None, k.R_APPROVAL_PAYLOAD_PIN_MISMATCH)
    junk = __import__("base64").b64encode(b"not json").decode()
    assert gate.read_approval_payload({k.APPROVAL_PAYLOAD_ENV: junk}, hashlib.sha256(b"not json").hexdigest()) == (None, k.R_APPROVAL_UNREADABLE)


# ── Migration preflight and graph ────────────────────────────────────────────

def test_migration_preflight_passes_on_the_repository_and_refuses_duplicates_dependencies_and_malformed_files(tmp_path):
    check = gate.preflight_migration_files(REPO)
    assert check.status == k.PASS and check.detail["files"] == check.detail["revisions"] >= 119
    root = harness.build_root(tmp_path / "root")
    versions = root / "database" / "migrations" / "versions"
    marker = tmp_path / "marker"
    duplicate = versions / "0121_duplicate.py"
    duplicate.write_text(f"open({str(marker)!r}, 'w').close()\nrevision = '0121'\ndown_revision = '0120'\n")
    check = gate.preflight_migration_files(root)
    assert (check.status, check.reason, check.detail["duplicate_revisions"], check.detail["duplicate_count"]) == (k.FAIL, k.R_GRAPH_UNLOADABLE, ["0121"], 1)
    assert not marker.exists()
    duplicate.unlink()
    cases = {
        "depends": "revision = '0124'\ndown_revision = '0123'\ndepends_on = '0111'\n",
        "labels": "revision = '0124'\ndown_revision = '0123'\nbranch_labels = ('x',)\n",
        "merge": "revision = '0124'\ndown_revision = ('0123', '0111')\n",
        "unknown_parent": "revision = '0124'\ndown_revision = '9999'\n",
        "non_literal": "revision = str(124)\ndown_revision = '0123'\n",
        "bad_id": "revision = 'p-not a real id'\ndown_revision = '0123'\n",
        "syntax": "revision = '0124\n",
        "twice": "revision = '0124'\nrevision = '0125'\ndown_revision = '0123'\n",
    }
    for name, body in cases.items():
        path = versions / f"0124_{name}.py"
        path.write_text(body)
        check = gate.preflight_migration_files(root)
        path.unlink()
        assert check.status == k.FAIL and check.reason == k.R_GRAPH_UNLOADABLE, name
        assert "p-not" not in json.dumps(check.detail), name
        assert (check.detail["malformed_count"] + check.detail["unknown_parent_count"]) >= 1, (name, check.detail)
    (versions / "9998_a.py").write_text("revision: str = '9998'\ndown_revision: str = '0123'\n")
    assert gate.preflight_migration_files(root).status == k.PASS
    (versions / "9998_a.py").unlink()


def test_graph_edges_heads_and_branch_deltas_hold_for_every_phase():
    for name, phase in k.DEPLOY_PHASES.items():
        check, known = gate.check_graph(REPO, phase)
        assert check.status == k.PASS, (name, check.reason, check.detail)
        assert check.detail["branch_delta"] == list(phase["branch_delta"]) and known >= phase["rows"]
    graph = gate.load_graph(REPO)
    assert graph["heads"] == k.EXPECTED_REPOSITORY_HEADS
    assert gate.branch_chain(graph["down"], "0123", "0112") == ("0118", "0120", "0121", "0122", "0123")
    assert gate.branch_chain(graph["down"], "0111", "0112") is None
    assert graph["down"]["0124"] == ("0123",)
    assert "0124" not in k.DEPLOY_PHASES["S3"]["branch_delta"]


def test_renaming_a_duplicate_0124_file_does_not_repair_its_revision_id(tmp_path):
    root = harness.build_root(tmp_path / "root")
    path = root / "database/migrations/versions/0125_google_merchant_ledger.py"
    path.write_text('revision = "0124"\ndown_revision = "0123"\n')
    check = gate.preflight_migration_files(root)
    assert (check.status, check.reason, check.detail["duplicate_revisions"]) == (k.FAIL, k.R_GRAPH_UNLOADABLE, ["0124"])


def test_stale_schema_expectations_fail_for_new_source(tmp_path):
    root = harness.build_root(tmp_path / "root")
    path = root / k.EXPECTATIONS_RELATIVE_PATH
    record = json.loads(path.read_text(encoding="utf-8"))
    record["database_py_digest"] = "0" * 64
    path.write_text(json.dumps(record), encoding="utf-8")
    check, loaded = gate.check_expectations(root)
    assert (check.status, check.reason, loaded) == (k.FAIL, k.R_EXPECTATIONS_STALE, None)


def test_graph_mismatch_is_refused_before_the_database(tmp_path):
    root = harness.build_root(tmp_path / "root")
    versions = root / "database" / "migrations" / "versions"
    _clear_bytecode(root)
    path = versions / "0121_shopify_connection_foundation.py"
    path.write_text(path.read_text().replace('down_revision = "0120"', 'down_revision = "0113"'))
    _clear_bytecode(root)
    check, known = gate.check_graph(root, k.DEPLOY_PHASES["S3"])
    assert (check.status, check.reason, known) == (k.FAIL, k.R_GRAPH_EDGE_MISMATCH, None) and check.detail == {"revisions": ["0121"]}
    path.write_text(path.read_text().replace('down_revision = "0113"', 'down_revision = "0120"'))
    (versions / "9998_extra.py").write_text('revision = "9998"\ndown_revision = "0113"\nbranch_labels = None\ndepends_on = None\n'
                                            'def upgrade():\n    pass\ndef downgrade():\n    pass\n')
    _clear_bytecode(root)
    check, _ = gate.check_graph(root, k.DEPLOY_PHASES["S3"])
    assert (check.status, check.reason) == (k.FAIL, k.R_GRAPH_HEADS_MISMATCH)


def test_main_stops_at_the_first_refusal_and_never_loads_migrations_after_an_inventory_failure(tmp_path):
    root = harness.build_root(tmp_path / "root")
    marker = tmp_path / "marker"
    count, digest = gate.inventory(root)
    harness.plant_marker(root / "database" / "migrations" / "versions" / "0121_shopify_connection_foundation.py", marker)
    _clear_bytecode(root)
    argv = ["--root", str(root), "--phase", "S0", "--release-sha", harness.SYNTHETIC_RELEASE_SHA, "--inventory-digest", digest,
            "--inventory-count", str(count), "--approval-sha256", "e" * 64, "--expect-project", harness.SYNTHETIC_IDENTITY["project"],
            "--expect-environment", harness.SYNTHETIC_IDENTITY["environment"], "--expect-service", harness.SYNTHETIC_IDENTITY["service"],
            "--expect-db", DB, "--expect-backup-service", harness.SYNTHETIC_BACKUP["service"], "--expect-backup-volume", harness.SYNTHETIC_BACKUP["volume"]]
    env = harness.gate_environment("postgresql://svc:p@db.internal:5432/railway")
    rc = gate.main(argv, environ=env, now=NOW)
    assert rc == 1 and not marker.exists()
    # the same tampered tree with a digest that admits it: the AST preflight refuses a duplicate id before the graph loads
    (root / "database" / "migrations" / "versions" / "0121_dup.py").write_text(f"open({str(marker)!r}, 'w').close()\nrevision = '0121'\ndown_revision = '0120'\n")
    _clear_bytecode(root)
    count2, digest2 = gate.inventory(root)
    argv[argv.index("--inventory-digest") + 1] = digest2
    argv[argv.index("--inventory-count") + 1] = str(count2)
    rc = gate.main(argv, environ=env, now=NOW)
    assert rc == 1 and not marker.exists()


# ── Locked section with a fake connection ────────────────────────────────────

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar(self):
        return self._rows[0][0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _FakeConn:
    def __init__(self, answers, *, unlock=True, raise_on=None, raise_exc=None, oid_sequence=None):
        self.answers, self.unlock, self.raise_on, self.raise_exc = answers, unlock, raise_on, raise_exc
        self.oid_sequence = list(oid_sequence or [])
        self.log, self.invalidated, self.rolled_back, self.closed = [], False, 0, False

    def execution_options(self, **options):
        assert options == {"postgresql_readonly": True}
        return self

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append((sql, params))
        if self.raise_on and self.raise_on in sql:
            raise self.raise_exc
        if "pg_try_advisory_lock" in sql:
            assert params == {"key": k.ADVISORY_LOCK_KEY}
            return _Result([(self.answers.get("lock", True),)])
        if "pg_advisory_unlock" in sql:
            assert params == {"key": k.ADVISORY_LOCK_KEY}
            return _Result([(self.unlock,)])
        if "AS oid" in sql and self.oid_sequence:
            return _Result([(self.oid_sequence.pop(0),)])
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


def _run(conn, phase="S0"):
    engine = _FakeEngine(conn)
    checks = gate.run_database_checks("postgresql://u:p@db.internal/x", k.DEPLOY_PHASES[phase], KNOWN, EXPECTATIONS, "x",
                                      engine_factory=lambda url: engine)
    return {c.name: c for c in checks}, engine


SERVER_OK = {"search_path": [("public, pg_catalog",)], "pg_is_in_recovery": [(False,)], "current_schema": [("public",)],
             "current_database": [("x",)], "default_transaction_read_only": [("off",)]}


def test_effective_global_read_only_default_is_refused_and_observation_read_only_is_distinct():
    # There are no pg_db_role_setting rows in this control: SHOW must see the
    # effective server-wide default. The observation transaction remains read
    # only even when the application's default is writable.
    for value in ("on", "", None):
        conn = _FakeConn({**SERVER_OK, "default_transaction_read_only": [(value,)]})
        checks, engine = _run(conn)
        assert checks["server"].reason == k.R_SERVER_READ_ONLY
        assert checks["unlock"].status == k.PASS and conn.closed and engine.disposed
        assert any(sql == "SHOW default_transaction_read_only" for sql, _ in conn.log)
        assert not any("pg_db_role_setting" in sql for sql, _ in conn.log)
    conn = _FakeConn({**SERVER_OK, "transaction_read_only": [("on",)]})
    checks, _ = _run(conn)
    assert checks["server"].status == k.PASS
    assert not any(sql == "SHOW transaction_read_only" for sql, _ in conn.log)


def test_lock_denied_on_the_legacy_key_refuses_without_reading_anything_and_without_unlocking():
    conn = _FakeConn({"lock": False})
    checks, engine = _run(conn)
    assert checks["lock"].reason == k.R_LOCK_DENIED and "unlock" not in checks and "server" not in checks
    assert all("pg_advisory_unlock" not in sql for sql, _ in conn.log) and conn.closed and engine.disposed


def test_unlock_failure_fails_the_run_even_when_every_check_passed():
    conn = _FakeConn({}, unlock=False, raise_on="pg_is_in_recovery", raise_exc=RuntimeError("stop after lock"))
    checks, engine = _run(conn)
    assert checks["unlock"].reason == k.R_UNLOCK_FAILED and conn.invalidated and conn.rolled_back >= 1 and engine.disposed


def test_search_path_mismatch_and_relation_replacement_are_refused():
    conn = _FakeConn({**SERVER_OK, "search_path": [("\"$user\", public",)]})
    checks, _ = _run(conn)
    assert checks["server"].reason == k.R_SEARCH_PATH_MISMATCH
    names = len(k.IDENTITY_TABLES)
    conn = _FakeConn(dict(SERVER_OK), oid_sequence=list(range(100, 100 + names)) + list(range(100, 100 + names - 1)) + [999])
    checks, _ = _run(conn)
    assert checks["server"].status == k.PASS and checks["relation_identity"].reason == k.R_RELATION_REPLACED
    assert checks["relation_identity"].detail == {"relations": [k.IDENTITY_TABLES[-1]]} and checks["unlock"].status == k.PASS


def test_timeouts_and_errors_are_reported_by_closed_token_and_type_only():
    class Orig:
        pgcode = "55P03"

    class LockTimeout(Exception):
        orig = Orig()

    conn = _FakeConn({}, raise_on="pg_is_in_recovery", raise_exc=LockTimeout("canceling statement: secret-ish detail"))
    checks, _ = _run(conn)
    assert (checks["database"].reason, checks["database"].detail) == (k.R_TIMEOUT, {"error": "LockTimeout"})
    assert checks["unlock"].status == k.PASS and conn.rolled_back >= 1 and "server" not in checks
    conn = _FakeConn(SERVER_OK, raise_on="current_schema", raise_exc=ValueError("p-not-a-real-secret"))
    checks, _ = _run(conn)
    assert (checks["database"].reason, checks["database"].detail) == (k.R_INTERNAL_ERROR, {"error": "ValueError"})
    assert "p-not-a-real-secret" not in json.dumps({n: c.detail for n, c in checks.items()})


def test_connect_failure_is_a_refusal_by_type_only():
    def factory(url):
        raise ConnectionRefusedError("postgresql://u:p-not-a-real-secret@db.internal/x")

    checks = gate.run_database_checks("postgresql://u:p@db.internal/x", k.DEPLOY_PHASES["S0"], KNOWN, EXPECTATIONS, "x", engine_factory=factory)
    assert [(c.name, c.reason, c.detail) for c in checks] == [("connect", k.R_CONNECT_FAILED, {"error": "ConnectionRefusedError"})]


# ── Static guarantees: read-only, no executor, no subprocess ─────────────────

def _sql_literals(tree: ast.Module):
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.endswith("_SQL"):
            value = node.value
            if isinstance(value, ast.Constant):
                constants[node.targets[0].id] = value.value
            else:
                constants[node.targets[0].id] = "".join(part.value if isinstance(part, ast.Constant) else "<_USER_SCHEMA>" for part in _flatten(value))
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


def _flatten(node):
    if isinstance(node, ast.BinOp):
        return _flatten(node.left) + _flatten(node.right)
    return [node]


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
    forbidden = {"subprocess", "alembic.command", "alembic.operations", "alembic.runtime", "os.system", "multiprocessing", "shutil"}
    assert not (imported & forbidden) and not any(name.startswith("alembic.command") for name in imported)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            assert node.attr not in {"system", "popen", "spawnv", "execv", "execve", "fork", "remove", "unlink", "rename"}
    literals = _sql_literals(tree)
    assert len(literals) >= 18
    for sql in literals:
        head = sql.strip().upper()
        assert head.startswith("SELECT ") or head.startswith("SHOW "), sql[:60]
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE|GRANT|STAMP|MERGE|UPGRADE|COPY|LOCK)\b", head.split(" USING ")[0]), sql[:60]
    for needle in ("postgresql_readonly=True", "poolclass=NullPool", "hide_parameters=True", "echo=False", "logging.disable(logging.CRITICAL)",
                   "pg_try_advisory_lock(:key)", "pg_advisory_unlock(:key)", "search_path={k.SEARCH_PATH}", "contype <> 'n'", "i.indisvalid, i.indisready, i.indislive"):
        assert needle in source, needle


def test_cli_fails_closed_and_never_echoes_arguments_or_the_dsn(tmp_path):
    root = harness.build_root(tmp_path / "root")
    report = render.inventory_report(root)
    base_argv = [sys.executable, "-I", str(root / k.GATE_RELATIVE_PATH), "--root", str(root), "--release-sha", harness.SYNTHETIC_RELEASE_SHA,
                 "--inventory-digest", report["inventory_digest"], "--inventory-count", str(report["inventory_count"]), "--approval-sha256", "e" * 64,
                 "--expect-project", harness.SYNTHETIC_IDENTITY["project"], "--expect-environment", harness.SYNTHETIC_IDENTITY["environment"],
                 "--expect-service", harness.SYNTHETIC_IDENTITY["service"], "--expect-db", DB,
                 "--expect-backup-service", harness.SYNTHETIC_BACKUP["service"], "--expect-backup-volume", harness.SYNTHETIC_BACKUP["volume"]]
    env = harness.gate_environment("postgresql://svc:p-not-a-real-secret@db.internal:5432/railway", release_sha="c" * 40)

    def run(argv, environment=env):
        completed = subprocess.run(argv, env=environment, capture_output=True, text=True, timeout=120)
        return completed.returncode, completed.stdout + completed.stderr

    rc, out = run(base_argv + ["--phase", "S0"])
    assert rc == 1 and "RESULT=FAIL" in out and "release_sha FAIL reason=release_sha_mismatch" in out
    assert "database checks skipped" in out and "CHECK inventory" not in out and "p-not-a-real-secret" not in out and "postgresql://" not in out
    for phase in ("S1", "S2"):
        rc, out = run(base_argv + ["--phase", phase])
        assert rc == 2 and f"RESULT=USAGE reason={k.R_PHASE_NOT_DEPLOYABLE}" in out
    for phase in ("LEGACY_462_S0", "head", "S9"):
        rc, out = run(base_argv + ["--phase", phase])
        assert rc == 2 and "RESULT=USAGE reason=invalid_arguments" in out and phase not in out.replace("usage", "")
    rc, out = run(base_argv + ["--phase", "S0", "--inventory-count", "p-not-a-real-count"])
    assert rc == 2 and "RESULT=USAGE reason=invalid_arguments" in out and "p-not-a-real-count" not in out
    rc, out = run(base_argv + ["--phase", "S0", "--unknown-p-not-a-real-flag"])
    assert rc == 2 and "p-not-a-real-flag" not in out
    marker_root = str(tmp_path / "p-not-a-real-path")
    argv = list(base_argv)
    argv[argv.index("--root") + 1] = marker_root
    rc, out = run(argv + ["--phase", "S0"], {**env, k.RELEASE_SHA_ENV: harness.SYNTHETIC_RELEASE_SHA})
    assert rc == 1 and "root FAIL reason=root_missing" in out and "p-not-a-real-path" not in out
    rc, out = run(base_argv + ["--phase", "S0"], {**env, "VITE_SHOPIFY_CATALOG_IMPORT_UI": "true", k.RELEASE_SHA_ENV: harness.SYNTHETIC_RELEASE_SHA})
    assert "approval FAIL" in out and "shopify_flags" not in out  # approval (unfilled pin) refuses first; flags never reached
