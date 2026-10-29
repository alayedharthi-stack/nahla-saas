"""Named 0124 and schema-retaining rollback on disposable PostgreSQL only.

The initial capture is a candidate, never automatic approval. The candidate's
15 existing Shopify definitions and retained definitions must equal committed
S3 pins before either additional phase is exercised.
"""
import json
import copy
import os
from datetime import datetime, timedelta, timezone
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.tests import test_shopify_release_gate_pg as h
from backend.tests import _shopify_release_gate_harness as harness
from backend.tests import _alembic_topology_probe as topo
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import shopify_release_gate_render as render

pytestmark = h.pytestmark


def test_source_0125_preserves_0124_dormant_schema(tmp_path):
    """Capture a fresh, isolated 0124 schema before refreshing a source pin."""
    db = h._fresh_db("install_source_pin")
    try:
        h._build(db, "0124")
        root = harness.build_root(tmp_path / "root")
        source = os.environ.get("SOURCE_SHA", harness.SYNTHETIC_RELEASE_SHA)
        candidate = render.snapshot(h._alias_url(db), source_sha=source, root=root, phase_name="S4_DORMANT")
        committed = json.loads((root / k.INSTALL_EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))
        for key in ("expectations_version", "retained", "shopify_s4"):
            assert candidate[key] == committed[key], key
        capture_dir = os.environ.get("NAHLA_INSTALL_RELEASE_CAPTURE_DIR")
        if capture_dir:
            path = Path(capture_dir) / "shopify-install-source-pin.candidate.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(candidate, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    finally:
        h._drop(db)


def test_named_0124_dormant_pilot_and_evidence_retaining_rollback(tmp_path):
    db = h._fresh_db("install_release")
    try:
        h._build(db, "0124")
        root = harness.build_root(tmp_path / "root")
        source = os.environ.get("SOURCE_SHA", harness.SYNTHETIC_RELEASE_SHA)
        candidate = render.snapshot(h._alias_url(db), source_sha=source, root=root, phase_name="S4_DORMANT")
        committed = json.loads((root / k.INSTALL_EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))
        assert gate.check_expectations(root, "S4_DORMANT")[0].status == k.PASS
        for key in ("retained", "shopify_s4", "expectations_version", "database_py_digest"):
            assert candidate[key] == committed[key], key
        assert len(candidate["shopify_s4"]["tables"]) == 17
        assert candidate["retained"] == h.EXPECTATIONS["retained"]
        for name, definition in h.EXPECTATIONS["shopify_s3"]["tables"].items():
            assert candidate["shopify_s4"]["tables"][name] == definition
        capture_dir = os.environ.get("NAHLA_INSTALL_RELEASE_CAPTURE_DIR")
        if capture_dir:
            path = Path(capture_dir) / "shopify-install-schema-expectations.candidate.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(candidate, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        baseline = topo.schema_snapshot(h._loopback_url(db))
        h._sql(db, "INSERT INTO tenants(id,name) VALUES (99001,'rollback-fixture')",
               "INSERT INTO users(id,username,email,tenant_id) VALUES (99001,'rollback-fixture','rollback@example.test',99001)",
               """INSERT INTO shopify_connections(id,tenant_id,shop_domain,shop_gid,status,generation,credential_version)
                  VALUES (99001,99001,'nahlah-dev.myshopify.com','gid://shopify/Shop/99001','disconnected',1,1)""",
               """INSERT INTO shopify_owner_identities(connection_id,shop_gid,shopify_user_id,tenant_id,user_id,created_at)
                  VALUES (99001,'gid://shopify/Shop/99001',99001,99001,99001,now())""")
        # Evidence-bearing rows survive rollback; no SQL downgrade is issued.
        h._sql(db, """INSERT INTO shopify_install_flows
                   (id,state_hash,browser_hash,shop_domain,redirect_hash,phase,generation,expires_at,created_at)
                   VALUES ('retained-proof','state-proof','browser-proof','nahlah-dev.myshopify.com',
                           'redirect-proof','failed',1,now(),now())""")
        h._assert_pass(root, "S4_DORMANT", db, "post0124 dormant")
        deadline = datetime.now(timezone.utc) + timedelta(days=1)
        env = {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "true",
               "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps({k.PILOT_SHOP: {
                   "sha256": "a" * 64, "expires_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")}})}
        h._assert_pass(root, "S4_PILOT", db, "restricted owner pilot", extra_env=env)
        h._assert_fail(root, "S4_DORMANT", db, "rollback refuses active flags", extra_env=env,
                       check="shopify_flags", reason=k.R_FLAG_ENABLED)
        h._assert_pass(root, "S4_DORMANT", db, "schema-retaining flags-OFF rollback")
        valid_path = harness.approval_for(root, "S4_DORMANT", h._target(db))
        valid_record = json.loads(valid_path.read_text(encoding="utf-8"))
        for field, value in (("source_sha", "a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec"),
                             ("database_heads", ["0111", "0113", "0123"]),
                             ("schema_expectations_sha256", "b" * 64),
                             ("runtime_inventory_digest", "b" * 64), ("phase", "S3")):
            invalid = copy.deepcopy(valid_record)
            invalid["schema_rollback"][field] = value
            if field == "source_sha":
                invalid["deployment_rollback_snapshot"]["source_sha"] = value
            path = harness.approval_file(root.parent / "invalid", invalid, field + ".json")
            out = h._assert_fail(root, "S4_DORMANT", db, "unbound rollback refused before DB", approval=path,
                                 check="approval", reason=k.R_SCHEMA_ROLLBACK_INVALID)
            assert "connect" not in harness.checks(out)
        assert h._sql(db, "SELECT id FROM shopify_install_flows") == [("retained-proof",)]
        assert h._sql(db, "SELECT shop_gid,shopify_user_id,tenant_id,user_id FROM shopify_owner_identities") == [
            ("gid://shopify/Shop/99001", 99001, 99001, 99001)]
        assert topo.schema_diff(topo.schema_snapshot(h._loopback_url(db)), baseline) == []
        h._assert_fail(root, "S3", db, "old 15-table contract cannot roll back 0124",
                       check="version_rows", reason=k.R_VERSION_ROWS_MISMATCH)
        h._sql(db, "ALTER TABLE shopify_owner_identities DROP CONSTRAINT ck_shopify_owner_positive")
        h._assert_fail(root, "S4_DORMANT", db, "owner definition drift refused",
                       check="shopify_footprint", reason=k.R_SHOPIFY_DEFINITION_DRIFT)
        for rows in (("0111", "0113", "0125"), ("0111", "0113", "0124", "0125")):
            h._sql(db, "DELETE FROM alembic_version")
            for revision in rows:
                h._sql(db, "INSERT INTO alembic_version(version_num) VALUES ('" + revision + "')")
            h._assert_fail(root, "S4_DORMANT", db, "Google/combined states refused",
                           check="version_rows", reason=k.R_VERSION_ROWS_UNKNOWN)
    finally:
        h._drop(db)


def test_first_dormant_deploy_runs_with_the_pinned_prior_rollback_and_pilot_still_refuses_it(tmp_path):
    db = h._fresh_db("install_prior")
    try:
        h._build(db, "0124")
        root = harness.build_root(tmp_path / "root")
        baseline = topo.schema_snapshot(h._loopback_url(db))
        prior = harness.prior_rollback_approval_for(root, "S4_DORMANT", h._target(db))
        record = json.loads(prior.read_text(encoding="utf-8"))
        assert record["schema_rollback"]["source_sha"] == record["deployment_rollback_snapshot"]["source_sha"] \
            == "efb8f7a7d7c2fabe876f71cfff17bdaa8a87ccd6" != record["release_sha"]
        h._assert_pass(root, "S4_DORMANT", db, "first dormant deploy, pinned prior dormant rollback", approval=prior)
        deadline = datetime.now(timezone.utc) + timedelta(days=1)
        env = {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "true",
               "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps({k.PILOT_SHOP: {
                   "sha256": "a" * 64, "expires_at": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")}})}
        pilot = harness.prior_rollback_approval_for(root, "S4_PILOT", h._target(db))
        out = h._assert_fail(root, "S4_PILOT", db, "pilot never rolls back to another source", approval=pilot,
                             extra_env=env, check="approval", reason=k.R_SCHEMA_ROLLBACK_INVALID)
        assert "connect" not in harness.checks(out)
        flags_on = harness.prior_rollback_record(root, json.loads(harness.approval_for(
            root, "S4_DORMANT", h._target(db)).read_text(encoding="utf-8")))
        flags_on["schema_rollback"]["flags"]["NAHLA_SHOPIFY_CONNECTION_ENABLED"] = "true"
        path = harness.approval_file(root.parent / "invalid", flags_on, "prior_flags_on.json")
        out = h._assert_fail(root, "S4_DORMANT", db, "prior rollback with a flag ON refused before DB", approval=path,
                             check="approval", reason=k.R_SCHEMA_ROLLBACK_INVALID)
        assert "connect" not in harness.checks(out)
        h._assert_fail(root, "S4_DORMANT", db, "runtime flags stay OFF for the dormant release", approval=prior,
                       extra_env={"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true"},
                       check="shopify_flags", reason=k.R_FLAG_ENABLED)
        assert topo.schema_diff(topo.schema_snapshot(h._loopback_url(db)), baseline) == []
    finally:
        h._drop(db)


def test_pilot_runs_with_the_bounded_catalog_import_and_refuses_any_wider_one(tmp_path):
    db = h._fresh_db("install_import")
    try:
        h._build(db, "0124")
        root = harness.build_root(tmp_path / "root")
        deadline = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pilot = {"NAHLA_SHOPIFY_CONNECTION_ENABLED": "true", "NAHLA_SHOPIFY_INSTALL_FIRST_ENABLED": "true",
                 "SHOPIFY_INSTALL_PILOT_ADMISSION": json.dumps({k.PILOT_SHOP: {"sha256": "a" * 64, "expires_at": deadline}})}
        bounded = {**pilot, k.CATALOG_IMPORT_FLAG: "true",
                   k.CATALOG_IMPORT_SHOPS_ENV: json.dumps({k.PILOT_SHOP: {"expires_at": deadline}})}
        baseline = topo.schema_snapshot(h._loopback_url(db))
        h._assert_pass(root, "S4_PILOT", db, "pilot with the import bounded to the pilot shop", extra_env=bounded)
        wider = {**bounded, k.CATALOG_IMPORT_SHOPS_ENV: json.dumps({
            k.PILOT_SHOP: {"expires_at": deadline}, "other-store.myshopify.com": {"expires_at": deadline}})}
        for env, reason, step in ((wider, k.R_CATALOG_IMPORT_BOUND_INVALID, "a second shop in the import bound"),
                                  ({**bounded, k.CATALOG_IMPORT_SHOPS_ENV: ""}, k.R_CATALOG_IMPORT_BOUND_INVALID,
                                   "import without a bound"),
                                  ({**bounded, "NAHLA_SHOPIFY_PRIVACY_WORKER_ENABLED": "true"}, k.R_FLAG_ENABLED,
                                   "import next to another write feature")):
            out = h._assert_fail(root, "S4_PILOT", db, step, extra_env=env, check="shopify_flags", reason=reason)
            assert "connect" not in harness.checks(out)
        h._assert_fail(root, "S4_DORMANT", db, "dormant rollback keeps the import OFF", extra_env=bounded,
                       check="shopify_flags", reason=k.R_FLAG_ENABLED)
        h._assert_pass(root, "S4_DORMANT", db, "dormant rollback with every flag OFF")
        assert topo.schema_diff(topo.schema_snapshot(h._loopback_url(db)), baseline) == []
    finally:
        h._drop(db)
