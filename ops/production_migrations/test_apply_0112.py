import contextlib
import importlib.util
import io
import os
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("operator_0112", Path(__file__).with_name("apply_0112.py"))
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


class OperatorTest(unittest.TestCase):
    def env(self):
        return {"DATABASE_URL": "postgresql://test:synthetic%40secret@verified.invalid:5432/example",
                "NAHLA_0112_TARGET": "verified.invalid:5432/example",
                "NAHLA_0112_CONFIRM": "APPLY_VERIFIED_0112"}

    def test_idle_does_not_connect(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(operator, "create_engine") as engine:
            self.assertEqual(operator.main(), 0)
            engine.assert_not_called()

    def test_target_and_libpq_redirections_refused(self):
        for changes in ({"NAHLA_0112_TARGET": "other.invalid:5432/example"},
                        {"PGHOST": "other.invalid"},
                        {"DATABASE_URL": self.env()["DATABASE_URL"] + "?host=other.invalid"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(RuntimeError):
                operator.validated_url({**self.env(), **changes})
        url, _ = operator.validated_url(self.env())
        self.assertEqual(url.password, "synthetic@secret")

    def run_mocked(self, states, returncode=0):
        with patch.dict(os.environ, self.env(), clear=True), \
             patch.object(operator, "create_engine", return_value=MagicMock()), \
             patch.object(operator, "read_state", side_effect=states), \
             patch.object(operator.subprocess, "run", return_value=types.SimpleNamespace(
                 returncode=returncode, stderr="")) as command, \
             contextlib.redirect_stdout(io.StringIO()):
            result = operator.main()
            return result, command

    def test_only_explicit_0112_and_verification_are_success(self):
        columns = {"tracking_data_source", "external_shipment_id", "carrier", "tracking_url",
                   "latest_event", "source_event_at", "last_verified_at"}
        result, command = self.run_mocked([(operator.BEFORE, set(), False), (operator.AFTER, columns, True)])
        self.assertEqual(result, 0)
        args, kwargs = command.call_args
        self.assertEqual(args[0][-3:], ["alembic", "upgrade", "0112"])
        self.assertEqual(kwargs["cwd"], operator.ROOT / "database")
        self.assertIn("lock_timeout=5s", kwargs["env"]["PGOPTIONS"])

    def test_unexpected_revision_never_migrates(self):
        with patch.dict(os.environ, self.env(), clear=True), \
             patch.object(operator, "create_engine", return_value=MagicMock()), \
             patch.object(operator, "read_state", return_value=({"0093"}, set(), False)), \
             patch.object(operator.subprocess, "run") as command:
            with self.assertRaisesRegex(RuntimeError, "unexpected_starting"):
                operator.main()
            command.assert_not_called()

    def test_failed_command_cannot_report_success(self):
        result, command = self.run_mocked([(operator.BEFORE, set(), False)], returncode=1)
        self.assertEqual(result, 1)
        command.assert_called_once()

    def test_incomplete_post_schema_refused(self):
        with self.assertRaisesRegex(RuntimeError, "post_migration"):
            self.run_mocked([(operator.BEFORE, set(), False), (operator.AFTER, set(), False)])


if __name__ == "__main__":
    unittest.main()
