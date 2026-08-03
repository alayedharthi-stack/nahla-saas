"""Pytest hooks for Salla acceptance report artifact."""
from __future__ import annotations

import pytest

from tests.salla_acceptance.harness import print_console_summary, write_acceptance_report


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    summary = write_acceptance_report()
    print_console_summary(summary)
