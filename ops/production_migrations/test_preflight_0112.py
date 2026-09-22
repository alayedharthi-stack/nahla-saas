"""Offline regression checks for the production read-only preflight."""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy.engine import URL, make_url


spec = importlib.util.spec_from_file_location(
    "preflight_0112", Path(__file__).with_name("preflight_0112.py")
)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class StopBeforeDatabaseConnection(Exception):
    pass


class PreflightConnectionTests(unittest.TestCase):
    def test_driver_selection_preserves_credentials_without_connecting(self):
        for password in ("synthetic-password", "synthetic@password:/%"):
            with self.subTest(password_kind="encoded" if "@" in password else "plain"):
                source = URL.create(
                    "postgresql", username="example_user", password=password,
                    host="example.invalid", port=5432, database="example_db",
                )
                with patch.dict(os.environ, {
                    "DATABASE_URL": source.render_as_string(hide_password=False)
                }), patch("sqlalchemy.create_engine", side_effect=StopBeforeDatabaseConnection) as factory:
                    with self.assertRaises(StopBeforeDatabaseConnection):
                        preflight.main()
                actual = make_url(factory.call_args.args[0])
                self.assertEqual(actual.password, password)
                self.assertEqual(actual.drivername, "postgresql+psycopg")
                self.assertEqual(actual.host, source.host)
                self.assertEqual(actual.username, source.username)
                self.assertEqual(actual.database, source.database)

    def test_unconfigured_runner_stays_idle(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}), patch("sqlalchemy.create_engine") as factory:
            self.assertEqual(preflight.main(), 0)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
