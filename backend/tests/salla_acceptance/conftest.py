"""Pytest hooks for Salla acceptance report artifact."""
from __future__ import annotations

import pytest

from tests.salla_acceptance.harness import (
    LAYER2_ACCEPTANCE_RESULTS,
    print_console_summary,
    print_layer2_summary,
    write_acceptance_report,
    write_layer2_report,
)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    summary = write_acceptance_report()
    print_console_summary(summary)
    if LAYER2_ACCEPTANCE_RESULTS:
        layer2 = write_layer2_report()
        print_layer2_summary(layer2)
