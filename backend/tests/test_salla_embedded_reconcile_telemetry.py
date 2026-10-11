"""Regression tests for Salla embedded reconcile client telemetry endpoint."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from backend.tests._log_redaction_isolation import restoring_log_filters
from core.log_redaction import install_log_redaction

# Importing ``main`` runs ``install_log_redaction()``, which attaches the
# redacting filter to whichever root handlers exist at that moment. During
# collection those are pytest's session handlers, so the filter would stay on
# them for every later test. Keep the import's filters off them; the fixture
# below puts this module's requests under the same redaction for as long as
# each test runs.
with restoring_log_filters():
    from main import app


@pytest.fixture(autouse=True)
def _app_log_redaction():
    """Each request here runs under the redaction ``main`` installs in production."""
    with restoring_log_filters():
        install_log_redaction()
        yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_requests_here_run_under_the_app_log_redaction(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        logging.getLogger("nahla.test_telemetry_redaction").warning("callback code=%s", "telemetry-canary")
    assert "telemetry-canary" not in caplog.text
    assert "code=REDACTED" in caplog.text


def test_reconcile_telemetry_accepts_allowed_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    payload = {
        "event": "SALLA_RECONCILE_CTA_CLICK",
        "correlation_id": "src_test123_abcd",
        "sdk_loaded": True,
        "sdk_initialized": False,
        "destination_path": "/api/salla/oauth/start",
        "ts": 1_700_000_000_000,
    }
    with caplog.at_level("INFO"):
        resp = client.post("/api/salla/embedded/reconcile-telemetry", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert any("SALLA_RECONCILE_CTA_CLICK" in r.message for r in caplog.records)


def test_reconcile_telemetry_rejects_unknown_event(client: TestClient) -> None:
    resp = client.post(
        "/api/salla/embedded/reconcile-telemetry",
        json={
            "event": "NOT_ALLOWED",
            "correlation_id": "src_test123_abcd",
        },
    )
    assert resp.status_code == 400


def test_reconcile_telemetry_rejects_query_values_in_path(client: TestClient) -> None:
    resp = client.post(
        "/api/salla/embedded/reconcile-telemetry",
        json={
            "event": "SALLA_RECONCILE_NAV_ATTEMPT",
            "correlation_id": "src_test123_abcd",
            "destination_path": "/api/salla/oauth/start?embedded_reconcile=1",
        },
    )
    assert resp.status_code == 422
