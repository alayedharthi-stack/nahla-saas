"""Capture a review candidate from the workflow's disposable PostgreSQL service.

Test-only utility: never substitutes the candidate for the committed release
expectations. Normal gate tests must still validate the committed file. The
fixed synthetic loopback target and GitHub runner checks prevent accidental
reuse against a configured application database.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_ADMIN_DSN = "postgresql://nahla:nahla_password@127.0.0.1:5433/postgres"


def _git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main() -> None:
    if (os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("NAHLA_RELIABILITY_PG_ADMIN_DSN") != SYNTHETIC_ADMIN_DSN
            or os.environ.get("NAHLA_RELEASE_GATE_PG_HOST_ALIAS") != "shopify-gate-disposable"
            or os.environ.get("NAHLA_RELIABILITY_REQUIRE_PG") != "1"):
        raise RuntimeError("capture_requires_the_fixed_disposable_ci_service")
    if ROOT != Path(os.environ["GITHUB_WORKSPACE"]).resolve() / "source":
        raise RuntimeError("capture_requires_the_workflow_source_checkout")
    source_sha = _git("rev-parse", "HEAD").decode("ascii").strip()
    if source_sha != os.environ.get("GITHUB_SHA") or source_sha != os.environ.get("SOURCE_SHA"):
        raise RuntimeError("capture_source_does_not_match_trigger_commit")
    _git("diff", "--exit-code", "HEAD", "--")
    output = Path(os.environ["RUNNER_TEMP"]).resolve() / "shopify-gate-evidence"
    if output.is_relative_to(ROOT):
        raise RuntimeError("capture_output_must_be_outside_source")
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))

    # Existing test helpers create/drop only generated fixture databases.
    from backend.tests import test_shopify_release_gate_pg as fixture
    from scripts.operators import shopify_release_gate as gate
    from scripts.operators import shopify_release_gate_contract as contract
    from scripts.operators import shopify_release_gate_render as render

    committed = json.loads((ROOT / contract.EXPECTATIONS_RELATIVE_PATH).read_text(encoding="utf-8"))
    inventory_files = []
    for relative in gate.inventory_paths(ROOT):
        content = (ROOT / relative).read_bytes()
        if content != _git("show", f"HEAD:{relative}"):
            raise RuntimeError("capture_inventory_is_not_canonical_git_bytes")
        inventory_files.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
    inventory = {
        **render.inventory_report(ROOT),
        "source_sha": source_sha,
        "source_tree": _git("rev-parse", "HEAD^{tree}").decode("ascii").strip(),
        "files": inventory_files,
    }
    (output / "source-inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    database = fixture._fresh_db("gate_capture")
    try:
        fixture._build(database, "0121", "0122", "0123")
        rows = {row[0] for row in fixture._sql(database, "SELECT version_num FROM alembic_version")}
        if rows != {"0111", "0113", "0123"}:
            raise RuntimeError("capture_database_is_not_the_unchanged_s3_topology")
        candidate = render.snapshot(fixture._alias_url(database), source_sha=source_sha, root=ROOT)
        for key in ("retained", "shopify_s3", "expectations_version"):
            if candidate[key] != committed[key]:
                raise RuntimeError("capture_existing_schema_definitions_changed_" + key)
        if candidate["database_py_digest"] != gate.database_py_digest(ROOT):
            raise RuntimeError("capture_database_source_digest_mismatch")
        if set(candidate["shopify_s3"]["tables"]) != set(contract.SHOPIFY_TABLES) or len(contract.SHOPIFY_TABLES) != 15:
            raise RuntimeError("capture_s3_must_retain_its_fifteen_tables")
        (output / "shopify-release-schema-expectations.candidate.json").write_text(
            json.dumps(candidate, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"capture": "PASS", "source_sha": source_sha, "database_rows": sorted(rows),
                          "shopify_table_count": 15, "database_py_digest": candidate["database_py_digest"],
                          "inventory_count": inventory["inventory_count"],
                          "inventory_digest": inventory["inventory_digest"],
                          "committed_expectations_current": committed["database_py_digest"] == candidate["database_py_digest"]},
                         sort_keys=True))
    finally:
        fixture._drop(database)
    _git("diff", "--exit-code", "HEAD", "--")


if __name__ == "__main__":
    main()
