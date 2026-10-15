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


# ── S4_DORMANT: explicitly pinned prior dormant rollback (first deploy of a schema-preserving source) ──

DEPLOYED_BASELINE = "59216725836bdb0d104e5b8f5107369999efad63"
ALL_FLAGS_OFF = {name: "false" for name in k.SHOPIFY_FLAG_ENVS}


def _verdict(root, path, record, now, phase):
    return render.approval_verdict(root, approval_file=path, release_sha=record["release_sha"], phase=phase,
                                   identity=harness.SYNTHETIC_IDENTITY, db="db.internal:5432/fixture",
                                   backup_service=harness.SYNTHETIC_BACKUP["service"],
                                   backup_volume=harness.SYNTHETIC_BACKUP["volume"], now=now)


@pytest.fixture
def prior_approval(tmp_path):
    root = harness.build_root(tmp_path / "root")
    now = dt.datetime.now(dt.timezone.utc)
    path = harness.prior_rollback_approval_for(root, "S4_DORMANT", "db.internal:5432/fixture", now=now)
    record = json.loads(path.read_text(encoding="utf-8"))
    kwargs = {"phase_name": "S4_DORMANT", "release_sha": record["release_sha"],
              "runtime_digest": record["runtime_inventory_digest"], "runtime_count": record["runtime_inventory_count"],
              "target": record["target"], "backup_service": harness.SYNTHETIC_BACKUP["service"],
              "backup_volume": harness.SYNTHETIC_BACKUP["volume"], "now": now,
              "rollback_bindings": gate.schema_rollback_bindings(root)}
    return root, path, record, kwargs


def test_manifest_pins_exactly_one_full_prior_with_this_sources_s4_definitions():
    artifact = json.loads((ROOT / k.INSTALL_ROLLBACK_RELATIVE_PATH).read_text(encoding="utf-8"))
    prior = artifact["dormant_prior_rollback"]
    assert set(prior) == set(k.DORMANT_PRIOR_FIELDS)
    assert prior["rollback_mode"] == "pinned_prior_flags_off"
    assert prior["source_sha"] == DEPLOYED_BASELINE
    assert (prior["runtime_inventory_count"], prior["runtime_inventory_digest"]) == (
        157, "f6cc722de7c20383060ba2ff478247e729f46d28ea7793560d47b0cbec1e1c92")
    assert prior["schema_expectations_sha256"] == gate.file_sha256(ROOT / k.INSTALL_EXPECTATIONS_RELATIVE_PATH)
    # The same-source pilot contract and every flags-OFF value are unchanged.
    assert artifact["rollback_mode"] == "same_source_flags_off" and artifact["flags"] == ALL_FLAGS_OFF
    assert artifact["shopify_table_count"] == 17 and artifact["database_heads"] == ["0111", "0113", "0124"]


def test_first_dormant_deploy_accepts_the_pinned_prior_dormant_deployment(prior_approval):
    root, path, record, kwargs = prior_approval
    snapshot, rollback = record["deployment_rollback_snapshot"], record["schema_rollback"]
    assert record["release_sha"] != DEPLOYED_BASELINE == snapshot["source_sha"] == rollback["source_sha"]
    assert rollback["runtime_inventory_digest"] != record["runtime_inventory_digest"]   # the prior's own inventory
    assert k.evaluate_approval(record, **kwargs)[:2] == (k.PASS, None)
    assert _verdict(root, path, record, kwargs["now"], "S4_DORMANT")["status"] == k.PASS


def test_same_source_dormant_rollback_is_still_accepted(schema_approval):
    root, _path, _record, kwargs = schema_approval
    now = kwargs["now"]
    path = harness.approval_for(root, "S4_DORMANT", "db.internal:5432/fixture", now=now)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema_rollback"]["source_sha"] == record["release_sha"]
    assert k.evaluate_approval(record, **{**kwargs, "phase_name": "S4_DORMANT"})[:2] == (k.PASS, None)


def test_pilot_still_requires_a_dormant_rollback_of_the_same_source(tmp_path):
    root = harness.build_root(tmp_path / "root")
    now = dt.datetime.now(dt.timezone.utc)
    path = harness.prior_rollback_approval_for(root, "S4_PILOT", "db.internal:5432/fixture", now=now)
    record = json.loads(path.read_text(encoding="utf-8"))
    kwargs = {"phase_name": "S4_PILOT", "release_sha": record["release_sha"],
              "runtime_digest": record["runtime_inventory_digest"], "runtime_count": record["runtime_inventory_count"],
              "target": record["target"], "backup_service": harness.SYNTHETIC_BACKUP["service"],
              "backup_volume": harness.SYNTHETIC_BACKUP["volume"], "now": now,
              "rollback_bindings": gate.schema_rollback_bindings(root)}
    assert k.evaluate_approval(record, **kwargs)[:2] == (k.FAIL, k.R_SCHEMA_ROLLBACK_INVALID)
    verdict = _verdict(root, path, record, now, "S4_PILOT")
    assert (verdict["status"], verdict["reason"]) == (k.FAIL, k.R_SCHEMA_ROLLBACK_INVALID)
    # Every other S4_PILOT field and the pin itself were valid: only the source decides.
    record["schema_rollback"]["source_sha"] = record["deployment_rollback_snapshot"]["source_sha"] = record["release_sha"]
    record["schema_rollback"]["runtime_inventory_digest"] = record["runtime_inventory_digest"]
    record["schema_rollback"]["runtime_inventory_count"] = record["runtime_inventory_count"]
    assert k.evaluate_approval(record, **kwargs)[:2] == (k.PASS, None)


@pytest.mark.parametrize("mutation", [
    "unpinned_full_sha", "old_s3", "legacy_462", "abbreviated", "uppercase", "trailing_newline",
    "snapshot_is_candidate", "snapshot_other", "evidence_is_snapshot_mismatch",
    "candidate_inventory", "inventory_digest", "count_minus_one", "count_bool", "count_string",
    "flags_missing", "flag_enabled", "flag_bool", "flag_extra", "flag_missing_one", "flags_not_dict",
    "deployment", "definitions", "artifact", "tables_15", "heads_0123", "target", "review", "evidence_phase_pilot",
    "evidence_phase_s3", "record_version"])
def test_pinned_prior_rejections(prior_approval, mutation):
    _root, _path, record, kwargs = prior_approval
    record = copy.deepcopy(record)
    snapshot, rollback = record["deployment_rollback_snapshot"], record["schema_rollback"]

    def both(sha):
        snapshot["source_sha"] = rollback["source_sha"] = sha

    if mutation == "unpinned_full_sha":
        both("c" * 40)
    elif mutation == "old_s3":
        both("a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec")
    elif mutation == "legacy_462":
        both(k.LEGACY_RELEASE_SHA)
    elif mutation == "abbreviated":
        rollback["source_sha"] = DEPLOYED_BASELINE[:8]
    elif mutation == "uppercase":
        both(DEPLOYED_BASELINE.upper())
    elif mutation == "trailing_newline":
        rollback["source_sha"] = DEPLOYED_BASELINE + "\n"
    elif mutation == "snapshot_is_candidate":
        snapshot["source_sha"] = record["release_sha"]
    elif mutation == "snapshot_other":
        snapshot["source_sha"] = "d" * 40
    elif mutation == "evidence_is_snapshot_mismatch":
        rollback["source_sha"] = record["release_sha"]   # same-source claim against a prior snapshot
    elif mutation == "candidate_inventory":
        rollback["runtime_inventory_digest"] = record["runtime_inventory_digest"]
        rollback["runtime_inventory_count"] = record["runtime_inventory_count"]
    elif mutation == "inventory_digest":
        rollback["runtime_inventory_digest"] = "b" * 64
    elif mutation == "count_minus_one":
        rollback["runtime_inventory_count"] -= 1
    elif mutation == "count_bool":
        rollback["runtime_inventory_count"] = True
    elif mutation == "count_string":
        rollback["runtime_inventory_count"] = str(rollback["runtime_inventory_count"])
    elif mutation == "flags_missing":
        del rollback["flags"]
    elif mutation == "flag_enabled":
        rollback["flags"]["NAHLA_SHOPIFY_CONNECTION_ENABLED"] = "true"
    elif mutation == "flag_bool":
        rollback["flags"]["NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED"] = False
    elif mutation == "flag_extra":
        rollback["flags"]["NAHLA_SHOPIFY_UNKNOWN_ENABLED"] = "false"
    elif mutation == "flag_missing_one":
        rollback["flags"].pop("VITE_SHOPIFY_INSTALL_FIRST_ENABLED")
    elif mutation == "flags_not_dict":
        rollback["flags"] = "false"
    elif mutation == "deployment":
        rollback["railway_deployment_id"] = "99999999-9999-4999-8999-999999999999"
    elif mutation == "definitions":
        rollback["schema_expectations_sha256"] = "b" * 64
    elif mutation == "artifact":
        rollback["artifact_sha256"] = "b" * 64
    elif mutation == "tables_15":
        rollback["table_names"] = sorted(k.SHOPIFY_TABLES)
    elif mutation == "heads_0123":
        rollback["database_heads"] = ["0111", "0113", "0123"]
    elif mutation == "target":
        rollback["target"] = {**rollback["target"], "service": "77777777-7777-4777-8777-777777777777"}
    elif mutation == "review":
        rollback["independent_review_ref"] = " "
    elif mutation == "evidence_phase_pilot":
        rollback["phase"] = "S4_PILOT"
    elif mutation == "evidence_phase_s3":
        rollback["phase"] = "S3"
    else:
        rollback["record_version"] = 2
    status, reason, _detail = k.evaluate_approval(record, **kwargs)
    assert status == k.FAIL
    assert reason in {k.R_SCHEMA_ROLLBACK_INVALID, k.R_ROLLBACK_SNAPSHOT_MISSING}


@pytest.mark.parametrize("mutation", [
    "missing", "not_dict", "extra_field", "missing_field", "mode", "sha_abbreviated", "sha_uppercase",
    "sha_newline", "digest_malformed", "count_zero", "count_bool", "count_string", "definitions_differ",
    "definitions_malformed"])
def test_tampered_or_incompatible_prior_pin_refuses_only_the_prior_path(prior_approval, mutation):
    root, _path, record, kwargs = prior_approval
    bindings = copy.deepcopy(kwargs["rollback_bindings"])
    prior = bindings["artifact"]["dormant_prior_rollback"]
    if mutation == "missing":
        del bindings["artifact"]["dormant_prior_rollback"]
    elif mutation == "not_dict":
        bindings["artifact"]["dormant_prior_rollback"] = DEPLOYED_BASELINE
    elif mutation == "extra_field":
        prior["note"] = "x"
    elif mutation == "missing_field":
        del prior["runtime_inventory_count"]
    elif mutation == "mode":
        prior["rollback_mode"] = "same_source_flags_off"
    elif mutation == "sha_abbreviated":
        prior["source_sha"] = DEPLOYED_BASELINE[:12]
    elif mutation == "sha_uppercase":
        prior["source_sha"] = DEPLOYED_BASELINE.upper()
    elif mutation == "sha_newline":
        prior["source_sha"] = DEPLOYED_BASELINE + "\n"
    elif mutation == "digest_malformed":
        prior["runtime_inventory_digest"] = "f" * 63
    elif mutation == "count_zero":
        prior["runtime_inventory_count"] = 0
    elif mutation == "count_bool":
        prior["runtime_inventory_count"] = True
    elif mutation == "count_string":
        prior["runtime_inventory_count"] = "157"
    elif mutation == "definitions_differ":
        prior["schema_expectations_sha256"] = "c" * 64   # the prior's S4 definitions are not this source's
    else:
        prior["schema_expectations_sha256"] = "not-a-digest"
    # The evidence agrees with whatever the tampered pin now says, so only the
    # pin's own validation can refuse it.
    record = copy.deepcopy(record)
    if isinstance(bindings["artifact"].get("dormant_prior_rollback"), dict):
        rollback = record["schema_rollback"]
        for field in ("source_sha", "runtime_inventory_digest", "runtime_inventory_count"):
            if field in prior:
                rollback[field] = prior[field]
        record["deployment_rollback_snapshot"]["source_sha"] = rollback["source_sha"]
    tampered = {**kwargs, "rollback_bindings": bindings}
    assert k.evaluate_approval(record, **tampered)[0] == k.FAIL
    assert k.evaluate_approval(record, **tampered)[1] in {k.R_SCHEMA_ROLLBACK_INVALID, k.R_ROLLBACK_SNAPSHOT_MISSING}
    # The same-source path never depends on the pin.
    same = json.loads(harness.approval_for(root, "S4_DORMANT", "db.internal:5432/fixture",
                                           now=kwargs["now"]).read_text(encoding="utf-8"))
    assert k.evaluate_approval(same, **tampered)[:2] == (k.PASS, None)


def test_candidate_sources_definitions_must_still_equal_the_prior_pin(prior_approval, tmp_path):
    # A candidate whose S4 expectations file differs from the pinned prior's
    # can never name that prior as its rollback, even with matching evidence.
    root, _path, record, kwargs = prior_approval
    bindings = copy.deepcopy(kwargs["rollback_bindings"])
    bindings["schema_expectations_sha256"] = "e" * 64
    record = copy.deepcopy(record)
    record["schema_rollback"]["schema_expectations_sha256"] = "e" * 64
    assert k.evaluate_approval(record, **{**kwargs, "rollback_bindings": bindings})[:2] == (
        k.FAIL, k.R_SCHEMA_ROLLBACK_INVALID)


@pytest.mark.parametrize("mutation", ["old", "expired", "overlong", "future", "mismatched_time"])
def test_pinned_prior_keeps_rollback_evidence_freshness(prior_approval, mutation):
    _root, _path, record, kwargs = prior_approval
    record = copy.deepcopy(record)
    rollback, now = record["schema_rollback"], kwargs["now"]
    if mutation == "old":
        timestamp = harness.iso(now - dt.timedelta(hours=25))
        rollback["verified_at_utc"] = record["deployment_rollback_snapshot"]["verified_redeployable_at_utc"] = timestamp
        rollback["valid_until_utc"] = harness.iso(now + dt.timedelta(hours=1))
    elif mutation == "expired":
        rollback["valid_until_utc"] = harness.iso(now - dt.timedelta(seconds=1))
    elif mutation == "overlong":
        rollback["valid_until_utc"] = harness.iso(now + dt.timedelta(days=2))
    elif mutation == "future":
        timestamp = harness.iso(now + dt.timedelta(hours=1))
        rollback["verified_at_utc"] = record["deployment_rollback_snapshot"]["verified_redeployable_at_utc"] = timestamp
    else:
        rollback["verified_at_utc"] = harness.iso(now - dt.timedelta(hours=2))
    result = k.evaluate_approval(record, **kwargs)
    assert result[0] == k.FAIL
    assert result[1] in {k.R_SCHEMA_ROLLBACK_INVALID, k.R_SCHEMA_ROLLBACK_STALE, k.R_APPROVAL_TIMESTAMP_ORDER}


@pytest.mark.parametrize("mutation,reason", [
    ("restore_unverified", k.R_RESTORE_NOT_VERIFIED), ("backup_old", k.R_BACKUP_TOO_OLD),
    ("backup_volume", k.R_BACKUP_IDENTITY_MISMATCH), ("not_restorable", k.R_BACKUP_NOT_RESTORABLE),
    ("release", k.R_APPROVAL_RELEASE_MISMATCH), ("inventory", k.R_APPROVAL_NOT_BOUND),
    ("approval_target", k.R_APPROVAL_TARGET_MISMATCH), ("approval_phase", k.R_APPROVAL_PHASE_MISMATCH)])
def test_pinned_prior_keeps_backup_restore_and_release_bindings(prior_approval, mutation, reason):
    _root, _path, record, kwargs = prior_approval
    record = copy.deepcopy(record)
    recovery = record["db_recovery_evidence"]
    if mutation == "restore_unverified":
        recovery["restore_verified"] = False
    elif mutation == "backup_old":
        old = kwargs["now"] - dt.timedelta(hours=k.BACKUP_MAX_AGE_HOURS + 2)
        recovery["backup"]["card_timestamp_utc"] = harness.iso(old)
        recovery["backup"]["observed_at_utc"] = harness.iso(old + dt.timedelta(minutes=5))
        recovery["restore_verified_at_utc"] = harness.iso(old + dt.timedelta(minutes=10))
    elif mutation == "backup_volume":
        recovery["backup"]["volume_id"] = "77777777-7777-4777-8777-777777777777"
    elif mutation == "not_restorable":
        recovery["backup"]["restore_available"] = False
    elif mutation == "release":
        record["release_sha"] = DEPLOYED_BASELINE   # the approval is for the candidate, never the prior
    elif mutation == "inventory":
        record["runtime_inventory_digest"] = record["schema_rollback"]["runtime_inventory_digest"]
    elif mutation == "approval_target":
        record["target"] = {**record["target"], "db": "other.internal:5432/fixture"}
    else:
        record["phase"] = "S4_PILOT"
    assert k.evaluate_approval(record, **kwargs)[:2] == (k.FAIL, reason)
