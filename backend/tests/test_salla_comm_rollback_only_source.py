"""Source-bound, DB-free checks for the proposed rollback-only adapter."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as existing
from scripts.operators import salla_comm_rollback_only_contract as terminal
from scripts.operators import salla_comm_rollback_only_source as source


ROOT = Path(__file__).resolve().parents[2]


class SourceBindingTests(unittest.TestCase):
    def test_action_uses_exact_existing_0125_schema_and_flag_names(self):
        self.assertEqual(existing.phase(source.PHASE_NAME)["rows"], terminal.HEADS)
        self.assertEqual(set(existing.SHOPIFY_FLAG_ENVS + (existing.COMM_SEND_FLAG_ENV,)),
                         terminal.FLAG_NAMES)
        self.assertEqual(source.PHASE_NAME, "S4_COMM_DORMANT")

    def test_new_runtime_files_are_in_full_inventory(self):
        base_paths = set(gate.inventory_paths(ROOT))
        self.assertTrue(set(source.ACTION_FILES) <= base_paths)
        count, digest = source.candidate_inventory(ROOT)
        self.assertEqual((count, digest), gate.digest_paths(
            ROOT, sorted(base_paths | set(source.ACTION_FILES))))
        self.assertEqual(count, len(base_paths))

    def test_missing_action_file_refuses_inventory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixed.py").write_bytes(b"synthetic")
            with patch.object(gate, "inventory_paths", return_value=["fixed.py"]):
                with self.assertRaises(FileNotFoundError):
                    source.candidate_inventory(root)

    def test_database_port_checks_actual_gate_results_before_reporting_state(self):
        phase = existing.phase(source.PHASE_NAME)
        self.assertIsNotNone(phase)
        success = gate.Check("synthetic", existing.PASS)
        expectations = {"communication_0125": {}}
        port = source.GateDatabaseReadPort(ROOT, {"DATABASE_URL": "synthetic"},
                                           "synthetic:5432/db")
        with patch.object(gate, "check_graph", return_value=(success, frozenset({"0125"}))), \
                patch.object(gate, "check_expectations", return_value=(success, expectations)), \
                patch.object(existing, "parse_db_target", return_value={"database": "db"}), \
                patch.object(gate, "check_database_target", return_value=(success, "synthetic-url")), \
                patch.object(gate, "run_database_checks", return_value=[
                    gate.Check(name, existing.PASS) for name in source.REQUIRED_DB_CHECKS
                ]) as db_checks:
            state = port.state_read_only()
            self.assertEqual(state.heads, terminal.HEADS)
            self.assertEqual(state.shopify_schema_sha256, gate.file_sha256(
                ROOT / existing.INSTALL_EXPECTATIONS_RELATIVE_PATH))
            self.assertEqual(state.communication_schema_sha256, gate.file_sha256(
                ROOT / existing.COMM_EXPECTATIONS_RELATIVE_PATH))
            db_checks.assert_called_once()

    def test_database_port_refuses_failed_or_missing_check(self):
        success = gate.Check("synthetic", existing.PASS)
        port = source.GateDatabaseReadPort(ROOT, {"DATABASE_URL": "synthetic"},
                                           "synthetic:5432/db")
        for checks in (
            [gate.Check(name, existing.PASS) for name in source.REQUIRED_DB_CHECKS - {"unlock"}],
            [gate.Check(name, existing.FAIL if name == "communication_schema" else existing.PASS)
             for name in source.REQUIRED_DB_CHECKS],
        ):
            with self.subTest(checks=len(checks)), \
                    patch.object(gate, "check_graph", return_value=(success, frozenset({"0125"}))), \
                    patch.object(gate, "check_expectations", return_value=(success, {"communication_0125": {}})), \
                    patch.object(existing, "parse_db_target", return_value={"database": "db"}), \
                    patch.object(gate, "check_database_target", return_value=(success, "synthetic-url")), \
                    patch.object(gate, "run_database_checks", return_value=checks), \
                    self.assertRaisesRegex(terminal.Refused, "database_checks_failed"):
                port.state_read_only()


if __name__ == "__main__":
    unittest.main()
