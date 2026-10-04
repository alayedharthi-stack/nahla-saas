"""OTO stays dormant until ``OTO_EXTERNAL_EGRESS_ENABLED=1``.

With the switch absent every OTO route answers before touching the database
(no migration 0116, no ``OTO_TOKEN_ENC_KEY`` needed), the public webhook does not
exist, and nothing reaches OTO or WhatsApp. The dashboard reads ``/oto/availability``
and keeps the existing shipment card while it reports ``enabled: false``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import require_merchant_scope
from core.database import get_db
from routers import oto_shipping

_REPO = Path(__file__).resolve().parents[1]


class _UntouchableDb:
    """Any attribute access means a route reached the database."""

    def __getattr__(self, name):  # pragma: no cover - reaching here is the failure
        raise AssertionError(f"database touched while OTO is disabled: {name}")


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(oto_shipping.router)
    app.dependency_overrides[get_db] = lambda: _UntouchableDb()
    app.dependency_overrides[require_merchant_scope] = lambda: {"tenant_id": 7, "role": "merchant"}
    return TestClient(app, raise_server_exceptions=True)


MERCHANT_ROUTES = [
    ("PUT", "/oto/connection", {"environment": "staging", "refresh_token": "rt-12345678"}),
    ("GET", "/oto/connection", None),
    ("POST", "/oto/connection/staging/verify", None),
    ("POST", "/oto/pickup", {"environment": "staging", "code": "WH1", "name": "Main", "mobile": "0500000000",
                             "address": "Olaya St 1", "city": "Riyadh", "contact_name": "Ahmad",
                             "contact_email": "a@example.com"}),
    ("POST", "/oto/quotes", {"origin_city": "Riyadh", "destination_city": "Jeddah", "weight_kg": 1,
                             "width_cm": 10, "length_cm": 10, "height_cm": 10}),
    ("POST", "/oto/orders/1/shipments", {"delivery_option_id": 1, "weight_kg": 1, "width_cm": 10,
                                         "length_cm": 10, "height_cm": 10}),
    ("POST", "/oto/orders/1/sync", None),
    ("POST", "/oto/orders/1/label", None),
    ("POST", "/oto/orders/1/send-whatsapp", None),
    ("POST", "/oto/orders/1/cancel", None),
]


@pytest.fixture(autouse=True)
def _switch_off(monkeypatch):
    monkeypatch.delenv("OTO_EXTERNAL_EGRESS_ENABLED", raising=False)
    monkeypatch.delenv("OTO_PRODUCTION_ENABLED", raising=False)
    monkeypatch.delenv("OTO_TOKEN_ENC_KEY", raising=False)


def test_every_route_of_the_router_is_covered_here():
    declared = {(sorted(r.methods)[0], r.path) for r in oto_shipping.router.routes}
    covered = {(m, p.replace("/1/", "/{order_id}/").replace("/staging/verify", "/{environment}/verify"))
               for m, p, _ in MERCHANT_ROUTES}
    covered |= {("GET", "/oto/availability"), ("POST", "/oto/webhooks/{environment}/{event_type}")}
    assert declared == covered


@pytest.mark.parametrize("method,path,body", MERCHANT_ROUTES)
def test_merchant_routes_refuse_before_any_database_read(method, path, body):
    response = _client().request(method, path, json=body)
    assert response.status_code == 409, (path, response.text)
    assert response.json()["detail"] == "oto_integration_disabled"


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_only_the_exact_value_1_switches_it_on(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", value)
    assert oto_shipping.oto_integration_enabled() is False
    assert _client().get("/oto/availability").json() == {"enabled": False}


def test_availability_reports_on_without_a_database(monkeypatch):
    monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", "1")
    assert _client().get("/oto/availability").json() == {"enabled": True}


def test_public_webhook_does_not_exist_while_disabled():
    for environment in ("staging", "production"):
        response = _client().post(f"/oto/webhooks/{environment}/orderStatus",
                                  content=b'{"orderId": "x"}', headers={"content-type": "application/json"})
        assert response.status_code == 404


def test_enabled_webhook_refuses_an_oversized_declared_body_before_reading_it(monkeypatch):
    monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", "1")
    response = _client().post("/oto/webhooks/staging/orderStatus", content=b"{}",
                              headers={"content-type": "application/json", "content-length": "40000"})
    assert response.status_code == 413


def test_dashboard_shows_the_oto_panel_only_when_available():
    source = (_REPO / "dashboard" / "src" / "pages" / "OrderDetail.tsx").read_text(encoding="utf-8")
    assert "apiCall<{ enabled?: boolean }>('/oto/availability')" in source
    assert "{otoEnabled && (order.source === 'whatsapp' || order.source === 'manual') &&" in source
    assert "{(!otoEnabled || order.source !== 'whatsapp'" in source
    assert "useState(false)" in source.split("setOtoEnabled] =")[1].split("\n")[0]


# ── switched on: the gate opens and the routes reach their real logic ──────

class _RecordingQuery:
    def __init__(self, db, model):
        self.db, self.model = db, model

    def filter_by(self, **criteria):
        self.db.filters.append((self.model.__name__, criteria))
        return self

    def all(self):
        return list(self.db.rows)

    def first(self):
        return None


class _RecordingDb:
    def __init__(self, rows=()):
        self.rows, self.filters = list(rows), []

    def query(self, model):
        return _RecordingQuery(self, model)


def _enabled_client(db, tenant_id: int = 7) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _jwt(request, call_next):  # what jwt_enforcement_middleware sets on authenticated routes
        request.state.jwt_payload = {"tenant_id": tenant_id, "sub": "merchant@example.com", "role": "merchant"}
        return await call_next(request)

    app.include_router(oto_shipping.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_merchant_scope] = lambda: {"tenant_id": tenant_id, "role": "merchant"}
    return TestClient(app, raise_server_exceptions=True)


def test_enabled_connection_status_reads_only_this_tenant(monkeypatch):
    monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", "1")
    row = SimpleNamespace(environment="staging", enabled=False, pickup_location_code="WH1",
                          pickup_city="Riyadh", webhook_secret_ciphertext=None)
    db = _RecordingDb([row])
    response = _enabled_client(db).get("/oto/connection")
    assert response.status_code == 200
    assert response.json() == [{"environment": "staging", "enabled": False, "pickup_location_code": "WH1",
                                "pickup_city": "Riyadh", "webhook_configured": False}]
    assert db.filters == [("OtoConnection", {"tenant_id": 7})]


def test_enabled_webhook_reaches_the_connection_lookup_and_refuses_unconfigured(monkeypatch):
    from oto.order_data import oto_order_id

    monkeypatch.setenv("OTO_EXTERNAL_EGRESS_ENABLED", "1")
    db = _RecordingDb()
    response = _enabled_client(db).post("/oto/webhooks/staging/orderStatus",
                                        json={"orderId": oto_order_id(7, 5), "status": "delivered"})
    assert response.status_code == 401
    assert response.json()["detail"] == "oto_webhook_unconfigured"
    assert db.filters == [("OtoConnection", {"tenant_id": 7, "environment": "staging", "enabled": True})]
