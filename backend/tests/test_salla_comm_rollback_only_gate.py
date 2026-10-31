"""Synthetic entrypoint checks for the container-side trust boundary."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.operators import salla_comm_rollback_only_contract as terminal
from scripts.operators import salla_comm_rollback_only_gate as runner


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TEMPLATE = b"synthetic source-bound command template"
TARGET = terminal.Target(
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "synthetic.internal:5432/synthetic",
)


class Approvals:
    def __init__(self, approval):
        self.approval = approval

    def approved_action(self):
        return self.approval


class Database:
    def __init__(self):
        self.reads = 0

    def state_read_only(self):
        self.reads += 1
        return terminal.DatabaseState(terminal.HEADS_BEFORE, "d" * 64, "e" * 64)


class Renderer:
    def render(self, command_template, approval):
        return command_template


def approval():
    return terminal.Approval(
        action=terminal.ACTION, target=TARGET, candidate_source_sha="a" * 40,
        heads_before=tuple(sorted(terminal.HEADS_BEFORE)),
        heads_after=tuple(sorted(terminal.HEADS_AFTER)),
        owner_approval_reference="synthetic-owner-message",
        backup_service_id="44444444-4444-4444-8444-444444444444",
        backup_volume_id="55555555-5555-4555-8555-555555555555",
        backup_receipt_sha256="1" * 64, restore_receipt_sha256="2" * 64,
        rollback_command_template_sha256=sha256(TEMPLATE).hexdigest(),
        inventory_digest="c" * 64, inventory_count=164,
        shopify_schema_sha256="d" * 64, communication_schema_sha256="e" * 64,
        approved_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=15), approved=True,
    )


def args():
    return [
        "--root", str(ROOT), "--action", terminal.ACTION,
        "--release-sha", "a" * 40, "--inventory-digest", "c" * 64,
        "--inventory-count", "164", "--approval-sha256", "f" * 64,
        "--expect-project", TARGET.project,
        "--expect-environment", TARGET.environment,
        "--expect-service", TARGET.service,
        "--expect-db", TARGET.database_identity,
        "--expect-backup-service", "44444444-4444-4444-8444-444444444444",
        "--expect-backup-volume", "55555555-5555-4555-8555-555555555555",
    ]


class GateTests(unittest.TestCase):
    def test_container_checks_locals_without_a_railway_token(self):
        record = approval()
        database = Database()
        environ = {name: "false" for name in terminal.FLAG_NAMES}
        with patch.object(runner.source, "source_preflight"), \
                patch.object(runner.render, "command_template", return_value=TEMPLATE):
            result = runner.evaluate(args(), environ, NOW, approvals=Approvals(record),
                                     database=database, renderer=Renderer())
        self.assertEqual(result.approval, record)
        self.assertEqual(database.reads, 1)

    def test_wrong_action_refuses_before_preflight(self):
        wrong = args()
        wrong[wrong.index("--action") + 1] = "migrate_0125"
        with patch.object(runner.source, "source_preflight") as preflight, \
                self.assertRaisesRegex(terminal.Refused, "action_invalid"):
            runner.evaluate(wrong, {}, NOW)
        preflight.assert_not_called()

    def test_recovery_ids_must_match_approved_payload(self):
        record = terminal.Approval(**{**approval().__dict__,
                                      "backup_service_id": "other-service"})
        environ = {name: "false" for name in terminal.FLAG_NAMES}
        with patch.object(runner.source, "source_preflight"), \
                patch.object(runner.render, "command_template", return_value=TEMPLATE), \
                self.assertRaisesRegex(terminal.Refused, "recovery_identity_mismatch"):
            runner.evaluate(args(), environ, NOW, approvals=Approvals(record),
                            database=Database(), renderer=Renderer())

    def test_missing_approval_field_fails_closed(self):
        port = runner.PinnedApprovalReadPort({}, "f" * 64)
        with patch.object(runner.gate, "read_approval_payload",
                          return_value=({"record_version": 1}, None)), \
                self.assertRaisesRegex(terminal.Refused, "approval_shape_invalid"):
            port.approved_action()


if __name__ == "__main__":
    unittest.main()
