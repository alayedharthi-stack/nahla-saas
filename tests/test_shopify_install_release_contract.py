"""Post-0124 release states are exact and never authorize Google or a downgrade."""
import datetime as dt
import copy
import json
from pathlib import Path

import pytest

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import bootstrap_migration_contract as bootstrap
from scripts.operators import shopify_release_gate_render as render
from backend.tests import _shopify_release_gate_harness as harness

ROOT = Path(__file__).resolve().parents[1]


def pilot_env():
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    return {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "true",
            "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps({k.PILOT_SHOP: {
                "sha256": "a" * 64, "expires_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")}})}


def test_post_0124_states_are_separate_and_bootstrap_stays_0093():
    assert set(k.POST_INSTALL_PHASES) == {"S4_DORMANT", "S4_PILOT"}
    assert set(k.DEPLOY_PHASES) == {"S0", "S3"}
    assert bootstrap.INTEGRATION_BOOTSTRAP_TARGET == "0093"
    for name, definition in k.POST_INSTALL_PHASES.items():
        assert definition["rows"] == {"0111", "0113", "0124"}
        assert len(definition["tables"]) == 17
        assert definition["expectations_key"] == "shopify_s4"
        assert k.phase(name) == definition
        check, known = gate.check_graph(ROOT, definition)
        assert check.status == k.PASS
        for rows in (["0111", "0113", "0125"], ["0111", "0113", "0124", "0125"],
                     ["0111", "0113", "0123"], ["0111", "0113", "0124", "0124"]):
            assert k.evaluate_rows(rows, definition["rows"], known)[0] == k.FAIL


def test_dormant_rollback_refuses_every_enabled_shopify_feature():
    assert k.evaluate_flags({}, "S4_DORMANT")[0] == k.PASS
    for flag in k.SHOPIFY_FLAG_ENVS:
        assert k.evaluate_flags({flag: "true"}, "S4_DORMANT")[0] == k.FAIL


def test_pilot_requires_exact_connection_and_install_flags_and_one_shop():
    env = pilot_env()
    assert k.evaluate_flags(env, "S4_PILOT")[0] == k.PASS
    for flag in k.SHOPIFY_FLAG_ENVS:
        changed = dict(env)
        changed[flag] = "false" if flag in env else "true"
        assert k.evaluate_flags(changed, "S4_PILOT")[0] == k.FAIL

    entries = json.loads(env["SHOPIFY_INSTALL_PILOT_ADMISSION"])
    for shops in ({}, {"other.myshopify.com": entries[k.PILOT_SHOP]},
                  {**entries, "other.myshopify.com": entries[k.PILOT_SHOP]}):
        changed = {**env, "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps(shops)}
        assert k.evaluate_flags(changed, "S4_PILOT")[0] == k.FAIL


@pytest.mark.parametrize("expiry", ["2026-10-15T12:00:00.123Z", " 2026-10-15T12:00:00Z ", 0, None])
def test_pilot_expiry_uses_runtime_exact_whole_second_utc(expiry):
    env = pilot_env()
    entries = json.loads(env["SHOPIFY_INSTALL_PILOT_ADMISSION"])
    entries[k.PILOT_SHOP]["expires_at"] = expiry
    env["SHOPIFY_INSTALL_PILOT_ADMISSION"] = json.dumps(entries)
    assert k.evaluate_flags(env, "S4_PILOT")[0] == k.FAIL


@pytest.mark.parametrize("raw", ["", "null", "[]", "{", "x" * 4097,
                                   '{"nahlah-dev.myshopify.com":{},"nahlah-dev.myshopify.com":{}}'])
def test_pilot_malformed_admission_fails_closed_without_echoing_hashes(raw):
    result = k.evaluate_flags({**pilot_env(), "SHOPIFY_INSTALL_PILOT_ADMISSION": raw}, "S4_PILOT")
    assert result[0] == k.FAIL
    assert "sha256" not in json.dumps(result[2])


def test_post_0124_expectations_cannot_fall_back_to_15_table_s3(tmp_path):
    (tmp_path / "scripts/operators").mkdir(parents=True)
    (tmp_path / k.INSTALL_EXPECTATIONS_RELATIVE_PATH).write_text(
        (ROOT / k.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"), encoding="utf-8")
    assert gate.check_expectations(tmp_path, "S4_DORMANT")[0].status == k.FAIL


def test_snapshot_refuses_duplicate_revision_rows_even_with_complete_definitions(monkeypatch):
    from unittest.mock import MagicMock
    for name in k.LIBPQ_TARGET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    pinned = json.loads((ROOT / k.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))
    connection = MagicMock()
    connection.execution_options.return_value = connection
    connection.__enter__.return_value = connection
    engine = MagicMock()
    engine.connect.return_value = connection
    monkeypatch.setattr(gate, "_engine", lambda url: engine)
    monkeypatch.setattr(gate, "observe_retained", lambda conn: pinned["retained"])
    monkeypatch.setattr(gate, "observe_footprint", lambda conn, phase: {
        "definitions": pinned["shopify_s3"]["tables"], "sequences": pinned["shopify_s3"]["sequences"], "stray_count": 0})
    monkeypatch.setattr(gate, "read_version_rows", lambda conn: ["0111", "0113", "0123", "0123"])
    monkeypatch.setattr(gate, "_scalar", lambda conn, query: "16.0")
    with pytest.raises(render.RenderRefusal) as error:
        render.snapshot("postgresql://fixture:synthetic@disposable.internal:5432/fixture", source_sha="a" * 40)
    assert error.value.token == "database_is_not_a_complete_s3_build"


@pytest.fixture
def schema_approval(tmp_path):
    root = harness.build_root(tmp_path / "root")
    now = dt.datetime.now(dt.timezone.utc)
    path = harness.approval_for(root, "S4_PILOT", "db.internal:5432/fixture", now=now)
    record = json.loads(path.read_text(encoding="utf-8"))
    bindings = gate.schema_rollback_bindings(root)
    kwargs = {"phase_name": "S4_PILOT", "release_sha": record["release_sha"],
              "runtime_digest": record["runtime_inventory_digest"], "runtime_count": record["runtime_inventory_count"],
              "target": record["target"], "backup_service": harness.SYNTHETIC_BACKUP["service"],
              "backup_volume": harness.SYNTHETIC_BACKUP["volume"], "now": now, "rollback_bindings": bindings}
    return root, path, record, kwargs


def test_s4_approval_consumes_pinned_dormant_rollback_artifact(schema_approval):
    root, path, record, kwargs = schema_approval
    assert k.INSTALL_ROLLBACK_RELATIVE_PATH in gate.inventory_paths(root)
    assert k.evaluate_approval(record, **kwargs)[0] == k.PASS
    verdict = render.approval_verdict(root, approval_file=path, release_sha=record["release_sha"], phase="S4_PILOT",
                                      identity=harness.SYNTHETIC_IDENTITY, db="db.internal:5432/fixture",
                                      backup_service=harness.SYNTHETIC_BACKUP["service"],
                                      backup_volume=harness.SYNTHETIC_BACKUP["volume"], now=kwargs["now"])
    assert verdict["status"] == k.PASS


@pytest.mark.parametrize("mutation", ["missing", "old_s3", "heads", "duplicate_heads", "tables", "definitions", "source",
                                     "inventory", "count", "phase", "artifact", "target", "deployment", "review"])
def test_s4_refuses_incompatible_or_unbound_rollback(schema_approval, mutation):
    _root, _path, record, kwargs = schema_approval
    record = copy.deepcopy(record)
    rollback = record["schema_rollback"]
    if mutation == "missing":
        del record["schema_rollback"]
    elif mutation == "old_s3":
        record["deployment_rollback_snapshot"]["source_sha"] = "a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec"
        rollback["source_sha"] = record["deployment_rollback_snapshot"]["source_sha"]
    else:
        changes = {"heads": ("database_heads", ["0111", "0113", "0123"]),
                   "duplicate_heads": ("database_heads", ["0111", "0113", "0124", "0124"]),
                   "tables": ("table_names", sorted(k.SHOPIFY_TABLES)),
                   "definitions": ("schema_expectations_sha256", "b" * 64),
                   "source": ("source_sha", "b" * 40), "inventory": ("runtime_inventory_digest", "b" * 64),
                   "count": ("runtime_inventory_count", kwargs["runtime_count"] - 1),
                   "phase": ("phase", "S3"), "artifact": ("artifact_sha256", "b" * 64),
                   "target": ("target", {}), "deployment": ("railway_deployment_id", "99999999-9999-4999-8999-999999999999"),
                   "review": ("independent_review_ref", "")}
        key, value = changes[mutation]
        rollback[key] = value
    result = k.evaluate_approval(record, **kwargs)
    assert result[:2] == (k.FAIL, k.R_SCHEMA_ROLLBACK_INVALID)


@pytest.mark.parametrize("mutation", ["old", "expired", "overlong", "future", "mismatched_time"])
def test_s4_refuses_stale_rollback_verification(schema_approval, mutation):
    _root, _path, record, kwargs = schema_approval
    record = copy.deepcopy(record)
    rollback = record["schema_rollback"]
    now = kwargs["now"]
    if mutation == "old":
        timestamp = harness.iso(now - dt.timedelta(hours=25))
        rollback["verified_at_utc"] = timestamp
        record["deployment_rollback_snapshot"]["verified_redeployable_at_utc"] = timestamp
        rollback["valid_until_utc"] = harness.iso(now + dt.timedelta(hours=1))
    elif mutation == "expired":
        rollback["valid_until_utc"] = harness.iso(now - dt.timedelta(seconds=1))
    elif mutation == "overlong":
        rollback["valid_until_utc"] = harness.iso(now + dt.timedelta(days=2))
    elif mutation == "future":
        timestamp = harness.iso(now + dt.timedelta(hours=1))
        rollback["verified_at_utc"] = timestamp
        record["deployment_rollback_snapshot"]["verified_redeployable_at_utc"] = timestamp
    else:
        rollback["verified_at_utc"] = harness.iso(now - dt.timedelta(hours=2))
    result = k.evaluate_approval(record, **kwargs)
    assert result[0] == k.FAIL
    assert result[1] in {k.R_SCHEMA_ROLLBACK_INVALID, k.R_SCHEMA_ROLLBACK_STALE, k.R_APPROVAL_TIMESTAMP_ORDER}


@pytest.mark.parametrize("field,value", [("phase", "S3"), ("database_heads", ["0111", "0113", "0123"]),
                                          ("shopify_table_count", 15), ("rollback_mode", "downgrade"),
                                          ("flags", {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true"})])
def test_s4_refuses_incompatible_consumed_manifest(schema_approval, field, value):
    _root, _path, record, kwargs = schema_approval
    bindings = copy.deepcopy(kwargs["rollback_bindings"])
    bindings["artifact"][field] = value
    kwargs = {**kwargs, "rollback_bindings": bindings}
    assert k.evaluate_approval(record, **kwargs)[:2] == (k.FAIL, k.R_SCHEMA_ROLLBACK_INVALID)
