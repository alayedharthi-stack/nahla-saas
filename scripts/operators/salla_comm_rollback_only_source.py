"""Source and read-only DB checks for the terminal 0125 rollback action.

No deployment or migration writer is exposed. The operator must bind the
verified bootstrap bytes and inventory before this command is installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as existing
from scripts.operators import salla_comm_rollback_only_contract as terminal


ACTION_FILES = (
    "scripts/operators/shopify_release_gate_render.py",
    "scripts/operators/salla_comm_rollback_only_contract.py",
    "scripts/operators/salla_comm_rollback_only_source.py",
    "scripts/operators/salla_comm_rollback_only_gate.py",
    "scripts/operators/salla_comm_rollback_only_render.py",
)
PHASE_NAME = "S4_COMM_DORMANT"
REQUIRED_DB_CHECKS = frozenset({
    "connect", "lock", "server", "version_rows", "communication_schema",
    "shopify_footprint", "retained_schema", "relation_identity", "unlock",
})


def candidate_inventory(root: Path) -> tuple[int, str]:
    """Pin the old full inventory plus both new runtime modules."""
    paths = sorted(set(gate.inventory_paths(root)) | set(ACTION_FILES))
    return gate.digest_paths(root, paths)


def source_preflight(root: Path, environ: Mapping[str, str], release_sha: str,
                     expected_target: terminal.Target,
                     expected_count: int, expected_digest: str) -> None:
    """Reuse every source/graph/expectation check without changing the guard."""
    terminal.require(gate.check_root(root).ok, "root_mismatch")
    terminal.require(gate.check_release_sha(environ, release_sha).ok, "source_mismatch")
    terminal.require(not gate.unexpected_initializers(root), "initializer_unexpected")
    identity_status, _reason, _detail = existing.evaluate_identity(environ, {
        "project": expected_target.project,
        "environment": expected_target.environment,
        "service": expected_target.service,
    })
    terminal.require(identity_status == existing.PASS, "identity_mismatch")
    terminal.require(gate.check_libraries(root).ok, "library_mismatch")
    terminal.require(gate.preflight_migration_files(root).ok, "migration_preflight_failed")
    count, digest = candidate_inventory(root)
    terminal.require(count == expected_count and digest == expected_digest,
                     "source_inventory_mismatch")
    phase = existing.phase(PHASE_NAME)
    terminal.require(phase is not None and phase["rows"] == terminal.HEADS,
                     "phase_mismatch")
    terminal.require(set(existing.SHOPIFY_FLAG_ENVS + (existing.COMM_SEND_FLAG_ENV,))
                     == terminal.FLAG_NAMES, "flag_contract_mismatch")
    graph, known = gate.check_graph(root, phase)
    terminal.require(graph.ok and known is not None, "graph_mismatch")
    expectations, body = gate.check_expectations(root, PHASE_NAME)
    terminal.require(expectations.ok and body is not None, "expectations_mismatch")


class GateDatabaseReadPort:
    """Run the existing exact-schema checks in a read-only transaction."""

    def __init__(self, root: Path, environ: Mapping[str, str], expected_database: str):
        self.root = root
        self.environ = environ
        self.expected_database = expected_database

    def state_read_only(self) -> terminal.DatabaseState:
        phase = existing.phase(PHASE_NAME)
        terminal.require(phase is not None and phase["rows"] == terminal.HEADS,
                         "phase_mismatch")
        graph, known = gate.check_graph(self.root, phase)
        terminal.require(graph.ok and known is not None, "graph_mismatch")
        expectations, body = gate.check_expectations(self.root, PHASE_NAME)
        terminal.require(expectations.ok and body is not None, "expectations_mismatch")
        parsed = existing.parse_db_target(self.expected_database)
        terminal.require(parsed is not None, "database_target_invalid")
        target_check, bound_url = gate.check_database_target(self.environ, parsed)
        terminal.require(target_check.ok and bound_url is not None, "database_target_mismatch")
        checks = gate.run_database_checks(bound_url, phase, known, body, parsed["database"])
        terminal.require(REQUIRED_DB_CHECKS <= {check.name for check in checks}
                         and all(check.ok for check in checks), "database_checks_failed")
        return terminal.DatabaseState(
            heads=phase["rows"],
            shopify_schema_sha256=gate.file_sha256(
                self.root / existing.INSTALL_EXPECTATIONS_RELATIVE_PATH),
            communication_schema_sha256=gate.file_sha256(
                self.root / existing.COMM_EXPECTATIONS_RELATIVE_PATH),
        )
