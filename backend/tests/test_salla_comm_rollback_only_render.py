"""DB-free checks of the exact rollback-only command bytes."""

import json
from hashlib import sha256
from pathlib import Path
import unittest

from scripts.operators import shopify_release_gate_render as prior
from scripts.operators import salla_comm_rollback_only_contract as terminal
from scripts.operators import salla_comm_rollback_only_render as render


ROOT = Path(__file__).resolve().parents[2]
TARGET = terminal.Target(
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "synthetic.internal:5432/synthetic",
)
BACKUP_SERVICE = "44444444-4444-4444-8444-444444444444"
BACKUP_VOLUME = "55555555-5555-4555-8555-555555555555"


def template():
    return render.command_template(
        ROOT, release_sha="a" * 40, target=TARGET,
        backup_service=BACKUP_SERVICE, backup_volume=BACKUP_VOLUME,
        container_root="/app",
    )


class RenderTests(unittest.TestCase):
    def test_template_is_non_circular_and_binds_exact_candidate_runner(self):
        body = template().decode("utf-8")
        self.assertEqual(body.count(render.APPROVAL_SHA_PLACEHOLDER), 2)
        self.assertIn('gate_path = os.path.join(R, "' + render.RUNNER_RELATIVE_PATH + '")', body)
        self.assertIn('"scripts/operators/salla_comm_rollback_only_contract.py"', body)
        self.assertIn('"scripts/operators/shopify_release_gate_render.py"', body)
        self.assertIn('"scripts/operators/salla_comm_rollback_only_source.py"', body)
        self.assertIn('"scripts/operators/salla_comm_rollback_only_render.py"', body)
        self.assertIn('"scripts/operators/salla_comm_rollback_only_gate.py"', body)

    def test_bound_command_decodes_to_exact_action_and_payload_pin(self):
        raw = template()
        command = render.bind_command(raw, "f" * 64).decode("ascii")
        self.assertTrue(command.startswith('python -I -c "'))
        body = prior.decode_command(command)
        self.assertNotIn(render.APPROVAL_SHA_PLACEHOLDER, body)
        self.assertIn('ASHA = "' + "f" * 64 + '"', body)
        argv_line = next(line for line in body.splitlines() if line.startswith("ARGV = "))
        argv = json.loads(argv_line.removeprefix("ARGV = "))
        self.assertEqual(argv[argv.index("--action") + 1], terminal.ACTION)
        self.assertEqual(argv[argv.index("--release-sha") + 1], "a" * 40)
        self.assertEqual(argv[argv.index("--approval-sha256") + 1], "f" * 64)
        self.assertEqual(argv[argv.index("--expect-service") + 1], TARGET.service)
        self.assertEqual(sha256(raw).hexdigest(), sha256(template()).hexdigest())

    def test_malformed_approval_digest_is_rejected(self):
        with self.assertRaisesRegex(terminal.Refused, "approval_digest_invalid"):
            render.bind_command(template(), "not-a-digest")


if __name__ == "__main__":
    unittest.main()
