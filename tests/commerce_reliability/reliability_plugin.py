"""pytest plugin for the Commerce Conversation Reliability Gate.

Imported by ``tests/commerce_reliability/conftest.py`` (and by the inner
self-check session that proves the reconciliation rules). It contains no
application imports so the rules can be tested on their own.

Reconciliation rules (never silently broadened):

* A test listed in the reviewed manifest as a baseline defect or an
  unimplemented contract must fail by raising the exact recorded marker
  (``RELIABILITY_BASELINE[<case>] <signature>`` or
  ``RELIABILITY_UNIMPLEMENTED[<contract>] <signature>``). That exact failure
  is reported as an expected failure (xfail) so the run stays
  baseline-compatible.
* The same test passing → ``RECONCILE unexpected_pass`` failure.
* The same test failing differently → ``RECONCILE signature_changed``.
* An expired allowance → ``RECONCILE expired_allowance``, whatever the test
  did.

Every other test keeps its ordinary outcome. The gate runner
(``scripts/commerce_reliability_gate.py``) re-checks all of this from the
JUnit output with :mod:`reliability_evaluator`, so no single layer can pass
the gate on its own.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.commerce_reliability import reliability_evaluator as ev

MANIFEST_ENV = "NAHLA_RELIABILITY_MANIFEST"
TODAY_ENV = "NAHLA_RELIABILITY_TODAY"  # self-check only; the runner ignores it
DEFAULT_MANIFEST = Path(__file__).with_name("reliability_manifest.json")

_MANIFEST_KEY: "pytest.StashKey[Dict[str, Any]]" = pytest.StashKey()
_ALLOWANCES_KEY: "pytest.StashKey[Dict[str, Dict[str, Any]]]" = pytest.StashKey()


class BaselineDefect(AssertionError):
    """Raised by a runtime test when it observes a recorded baseline defect."""


class UnimplementedContract(AssertionError):
    """Raised by a runtime test when a planned runtime contract is absent."""


_EXPECTED_EXCEPTION = {"baseline": BaselineDefect, "unimplemented": UnimplementedContract}


def manifest_path() -> Path:
    raw = str(os.environ.get(MANIFEST_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_MANIFEST


def _today() -> _dt.date:
    raw = str(os.environ.get(TODAY_ENV) or "").strip()
    if raw:
        return _dt.date.fromisoformat(raw)
    return _dt.date.today()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "reliability_postgres: PostgreSQL tier of the commerce reliability gate "
        "(fails, never skips, when NAHLA_RELIABILITY_REQUIRE_PG=1)",
    )
    manifest = ev.load_manifest(manifest_path())
    config.stash[_MANIFEST_KEY] = manifest
    config.stash[_ALLOWANCES_KEY] = ev.allowance_index(manifest)


def _reconcile(report: pytest.TestReport, text: str) -> None:
    detail = report.longreprtext if report.failed else ""
    report.outcome = "failed"
    report.longrepr = text + ("\n\n" + detail if detail else "")
    if hasattr(report, "wasxfail"):
        delattr(report, "wasxfail")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    if call.when != "call":
        return
    entry = item.config.stash.get(_ALLOWANCES_KEY, {}).get(item.nodeid)
    if entry is None:
        return
    marker = ev.expected_marker(entry)
    if ev.allowance_expired(entry, _today()):
        _reconcile(
            report,
            f"{ev.RECONCILE_PREFIX} expired_allowance:{entry['id']}:expiry={entry.get('expiry')} "
            "— the harness never extends an allowance; re-review the manifest entry",
        )
        return
    if report.passed:
        _reconcile(
            report,
            f"{ev.RECONCILE_PREFIX} unexpected_pass:{entry['id']} — the recorded "
            f"{entry['kind']} no longer reproduces; delete the manifest entry in the fixing PR",
        )
        return
    if report.failed:
        exc = call.excinfo.value if call.excinfo is not None else None
        if isinstance(exc, _EXPECTED_EXCEPTION[entry["kind"]]) and str(exc) == marker:
            report.outcome = "skipped"
            report.wasxfail = marker
            return
        observed = f"{type(exc).__name__}: {str(exc)[:240]}" if exc is not None else "no exception"
        _reconcile(
            report,
            f"{ev.RECONCILE_PREFIX} signature_changed:{entry['id']} "
            f"expected={marker!r} observed={observed!r}",
        )


class _AllowanceRaiser:
    """Fixture object: raise the exact recorded marker for the current test."""

    def __init__(self, request: pytest.FixtureRequest, kind: str) -> None:
        self._request = request
        self._kind = kind

    def _raise(self, ident: str, signature: str, **facts: Any) -> None:
        allowances = self._request.config.stash.get(_ALLOWANCES_KEY, {})
        entry = allowances.get(self._request.node.nodeid)
        if entry is None or entry["kind"] != self._kind or entry["id"] != ident:
            raise AssertionError(
                f"{ev.RECONCILE_PREFIX} allowance_not_listed_for_test:{ident}:"
                f"{self._request.node.nodeid}"
            )
        exc = _EXPECTED_EXCEPTION[self._kind](ev.format_marker(self._kind, ident, signature))
        exc.facts = dict(facts)  # type: ignore[attr-defined]
        raise exc

    def defect(self, case_id: str, signature: str, **facts: Any) -> None:
        self._raise(case_id, signature, **facts)

    def contract(self, contract_id: str, signature: str, **facts: Any) -> None:
        self._raise(contract_id, signature, **facts)


@pytest.fixture(scope="session")
def reliability_manifest(request: pytest.FixtureRequest) -> Dict[str, Any]:
    return request.config.stash[_MANIFEST_KEY]


@pytest.fixture
def baseline(request: pytest.FixtureRequest) -> _AllowanceRaiser:
    """``baseline.defect(case_id, signature)`` records a manifest baseline defect."""
    return _AllowanceRaiser(request, "baseline")


@pytest.fixture
def unimplemented(request: pytest.FixtureRequest) -> _AllowanceRaiser:
    """``unimplemented.contract(contract_id, signature)`` records an absent contract."""
    return _AllowanceRaiser(request, "unimplemented")


__all__ = [
    "BaselineDefect", "DEFAULT_MANIFEST", "MANIFEST_ENV", "TODAY_ENV",
    "UnimplementedContract", "baseline", "manifest_path", "pytest_configure",
    "pytest_runtest_makereport", "reliability_manifest", "unimplemented",
]
