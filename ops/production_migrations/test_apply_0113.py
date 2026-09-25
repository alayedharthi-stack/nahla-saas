import contextlib
import importlib.util
import io
import os
from pathlib import Path
import types
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("operator_0113", Path(__file__).with_name("apply_0113.py"))
operator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(operator)


class OperatorTest(unittest.TestCase):
    def env(self, mode="APPLY_VERIFIED_0113"):
        return {"DATABASE_URL": "postgresql://test:synthetic%40secret@verified.invalid:5432/example",
                "NAHLA_0113_TARGET": "verified.invalid:5432/example",
                "NAHLA_0113_CONFIRM": mode}

    def test_idle_does_not_connect(self):
        for env in ({}, {"NAHLA_0113_CONFIRM": "yes"}, {"NAHLA_0113_CONFIRM": "APPLY_VERIFIED_0112"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), \
                    patch.object(operator, "create_engine") as engine, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(operator.main(), 0)
                engine.assert_not_called()

    def test_target_and_libpq_redirections_refused(self):
        for changes in ({"NAHLA_0113_TARGET": "other.invalid:5432/example"},
                        {"NAHLA_0113_TARGET": ""},
                        {"PGHOST": "other.invalid"},
                        {"DATABASE_URL": "postgresql://test:x@localhost:5432/example"},
                        {"DATABASE_URL": self.env()["DATABASE_URL"] + "?host=other.invalid"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(RuntimeError):
                operator.validated_url({**self.env(), **changes}, require_target=True)
        url, _ = operator.validated_url(self.env(), require_target=True)
        self.assertEqual(url.password, "synthetic@secret")
        # Inspecting names the target instead of requiring it.
        _, identity = operator.validated_url({**self.env("INSPECT"), "NAHLA_0113_TARGET": ""},
                                             require_target=False)
        self.assertEqual(identity, "verified.invalid:5432/example")

    def run_mocked(self, states, returncode=0, mode="APPLY_VERIFIED_0113"):
        out = io.StringIO()
        with patch.dict(os.environ, self.env(mode), clear=True), \
             patch.object(operator, "verify_reviewed_files"), \
             patch.object(operator, "load_migration", return_value=MagicMock()), \
             patch.object(operator, "create_engine", return_value=MagicMock()) as engine, \
             patch.object(operator, "read_state", side_effect=states) as state, \
             patch.object(operator, "bounded_sweep", return_value={"removed": 0}) as sweep, \
             patch.object(operator.subprocess, "run", return_value=types.SimpleNamespace(
                 returncode=returncode, stderr="")) as command, \
             contextlib.redirect_stdout(out):
            result = operator.main()
        self.state = state
        self.engine = engine
        return result, command, sweep, out.getvalue()

    def test_only_explicit_0113_from_0112_and_verification_are_success(self):
        result, command, sweep, out = self.run_mocked(
            [(operator.BEFORE, False, []), (operator.AFTER, True, [])])
        self.assertEqual(result, 0)
        args, kwargs = command.call_args
        self.assertEqual(args[0][-3:], ["alembic", "upgrade", "0113"])
        self.assertEqual(kwargs["cwd"], operator.ROOT / "database")
        self.assertIn("lock_timeout=5s", kwargs["env"]["PGOPTIONS"])
        sweep.assert_called_once()
        self.assertIn('"status": "verified"', out)

    def test_inspect_never_migrates(self):
        result, command, sweep, out = self.run_mocked([(operator.BEFORE, False, [])], mode="INSPECT")
        self.assertEqual(result, 0)
        command.assert_not_called()
        sweep.assert_not_called()
        self.assertIn('"status": "inspected"', out)
        self.assertFalse(self.state.call_args.kwargs["compare"])
        self.assertNotIn("secret", out)

    def test_unexpected_revision_or_present_relation_never_migrates(self):
        for state, message in (((({"0110", "0111"}), False, []), "unexpected_starting"),
                               (({"0093"}, False, []), "unexpected_starting"),
                               ((operator.BEFORE, True, []), "relation_present")):
            with self.subTest(state=state):
                with self.assertRaisesRegex(RuntimeError, message):
                    self.run_mocked([state])

    def test_already_applied_is_verified_not_reapplied(self):
        result, command, sweep, _out = self.run_mocked([(operator.AFTER, True, [])])
        self.assertEqual(result, 0)
        command.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "applied_revision_schema_mismatch"):
            self.run_mocked([(operator.AFTER, True, ["a difference"])])

    def test_failed_command_cannot_report_success(self):
        result, command, sweep, _out = self.run_mocked([(operator.BEFORE, False, [])], returncode=1)
        self.assertEqual(result, 1)
        command.assert_called_once()
        sweep.assert_not_called()

    def test_incomplete_post_schema_refused(self):
        for after in ((operator.AFTER, False, []), (operator.AFTER, True, ["diff"]),
                      ({"0111", "0112"}, True, [])):
            with self.subTest(after=after), self.assertRaisesRegex(RuntimeError, "post_migration"):
                self.run_mocked([(operator.BEFORE, False, []), after])

    def test_every_read_is_bounded_and_inspect_is_read_only(self):
        for compare in (False, True):
            with self.subTest(compare=compare):
                connection = MagicMock()
                connection.execute.return_value.scalars.return_value = ["0111", "0112"]
                connection.execute.return_value.scalar.return_value = False
                operator.read_state(connection, MagicMock(), compare=compare)
                sql = [str(call.args[0]) for call in connection.execute.call_args_list]
                self.assertIn("SET LOCAL lock_timeout = '5s'", sql)
                self.assertIn("SET LOCAL statement_timeout = '60s'", sql)
                self.assertEqual(sql[0] == "SET TRANSACTION READ ONLY", not compare)
                connection.rollback.assert_called_once()

    def test_the_connection_is_bounded(self):
        result, _command, _sweep, _out = self.run_mocked([(operator.BEFORE, False, [])], mode="INSPECT")
        self.assertEqual(result, 0)
        self.assertEqual(operator.CONNECT_ARGS["connect_timeout"], 15)
        self.assertIn("tcp_user_timeout", operator.CONNECT_ARGS)
        self.assertEqual(self.engine.call_args.kwargs["connect_args"], operator.CONNECT_ARGS)

    def test_a_stopped_run_is_ended_and_names_its_step(self):
        out, exited = io.StringIO(), []
        with patch.object(operator.os, "_exit", side_effect=exited.append), \
                contextlib.redirect_stdout(out):
            operator.step("read_state")
            operator.arm_watchdog(0.05).join(2)
        self.assertEqual(exited, [1])
        self.assertIn('"error": "watchdog_expired:read_state"', out.getvalue())

    def test_a_finished_run_disarms_its_watchdog(self):
        with patch.object(operator, "arm_watchdog") as watchdog:
            self.run_mocked([(operator.BEFORE, False, [])], mode="INSPECT")
        watchdog.assert_called_once_with(operator.WATCHDOG_SECONDS["INSPECT"])
        watchdog.return_value.cancel.assert_called_once()

    def test_a_read_that_waited_out_its_bound_reports_who_it_waited_behind(self):
        for pgcode, reported in (("55P03", True), ("57014", True), ("08006", False)):
            with self.subTest(pgcode=pgcode):
                error = operator.OperationalError("SELECT", {}, types.SimpleNamespace(pgcode=pgcode))
                with patch.object(operator, "report_lock_holders") as report, \
                        self.assertRaises(operator.OperationalError):
                    self.run_mocked(error, mode="INSPECT")
                self.assertEqual(report.called, reported)

    def test_the_lock_report_names_kinds_never_statement_text(self):
        sql = str(operator.LOCK_HOLDERS)
        self.assertIn("split_part(ltrim(a.query), ' ', 1)", sql)
        self.assertEqual(sql.count("a.query"), 1)


if __name__ == "__main__":
    unittest.main()
