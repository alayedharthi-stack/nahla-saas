"""Regression tests for Salla embedded reconcile client telemetry endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


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
