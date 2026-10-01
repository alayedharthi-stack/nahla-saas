import base64
import asyncio
import hashlib
import hmac
from types import SimpleNamespace

import httpx
import pytest

from oto.client import OtoClient, OtoApiError
from oto.crypto import decrypt_secret, encrypt_secret
from oto.order_data import build_order_payload, oto_order_id, parse_oto_order_id
from oto.security import verify_webhook
from oto.state import apply_oto_status


def test_client_uses_v2_path_and_bearer_without_exposing_token():
    observed = []

    def respond(request):
        observed.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("/refreshToken"):
            return httpx.Response(200, json={"access_token": "short-secret"})
        return httpx.Response(200, json={"success": True, "deliveryCompany": []})

    client = OtoClient("long-secret", base_url="https://staging-api.tryoto.com/rest/v2",
                       transport=httpx.MockTransport(respond))
    assert asyncio.run(client.check_oto_delivery_fee({"originCity": "Riyadh"}))["success"]
    assert observed == [("/rest/v2/refreshToken", None),
                        ("/rest/v2/checkOTODeliveryFee", "Bearer short-secret")]


def test_client_does_not_echo_provider_error_body():
    client = OtoClient("long-secret", transport=httpx.MockTransport(
        lambda _: httpx.Response(400, text="customer-secret")))
    with pytest.raises(OtoApiError) as error:
        asyncio.run(client.order_status("123"))
    assert "customer-secret" not in str(error.value)


def test_dedicated_encryption_key_required_and_roundtrip(monkeypatch):
    monkeypatch.delenv("OTO_TOKEN_ENC_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encrypt_secret("private")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("OTO_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    encrypted = encrypt_secret("private")
    assert "private" not in encrypted
    assert decrypt_secret(encrypted) == "private"


def test_signed_webhook_rejects_replay_and_tampering():
    now = 1_700_000_000_000
    message = f"nahlah-4-9:delivered:{now}".encode()
    signature = base64.b64encode(hmac.new(b"secret", message, hashlib.sha256).digest()).decode()
    payload = {"orderId": "nahlah-4-9", "status": "delivered",
               "timestamp": str(now), "signature": signature}
    assert verify_webhook(payload, secret="secret", event_type="orderStatus", now_ms=now)
    assert not verify_webhook({**payload, "status": "returned"}, secret="secret",
                              event_type="orderStatus", now_ms=now)
    assert not verify_webhook(payload, secret="secret", event_type="orderStatus", now_ms=now + 301_000)


def test_provider_status_requires_evidence_and_never_downgrades():
    order = SimpleNamespace(status="ready_to_ship")
    shipment = SimpleNamespace(status="oto_shipment_requested", source_event_at=None,
                               tracking_data_source=None, last_verified_at=None,
                               latest_event=None, tracking_number=None,
                               carrier=None, tracking_url=None, label_url=None,
                               external_shipment_id=None)
    assert apply_oto_status(order, shipment, {"status": "pickedUp", "timestamp": "2000",
                                              "trackingNumber": "TRK", "shipmentId": "SHP"})
    assert order.status == "shipped"
    assert shipment.external_shipment_id == "SHP"
    assert not apply_oto_status(order, shipment, {"status": "returned", "timestamp": "1000"})
    assert order.status == "shipped"


def test_order_id_carries_tenant_and_incomplete_address_fails():
    assert parse_oto_order_id(oto_order_id(4, 9)) == (4, 9)
    assert parse_oto_order_id("123") is None
    order = SimpleNamespace(id=9, status="ready_to_ship", customer_info={},
                            extra_metadata={}, customer_name="Buyer", line_items=[])
    with pytest.raises(ValueError):
        build_order_payload(order, tenant_id=4, pickup_code="P1", weight_kg=1,
                            width_cm=10, length_cm=10, height_cm=10)


def test_confirmed_cod_order_maps_to_oto_without_salla():
    order = SimpleNamespace(id=9, status="cod_pending", customer_info={
        "name": "Buyer", "mobile": "966500000000", "city": "Riyadh",
        "short_address_code": "ABCD1234", "country": "SA",
    }, extra_metadata={"short_address_code": "ABCD1234", "amount_value": 125,
                       "payment_method": "cash_on_delivery"}, customer_name="Buyer",
        line_items=[{"name": "Product", "unit_price": 125, "quantity": 1, "sku": "SKU1"}])
    payload = build_order_payload(order, tenant_id=4, pickup_code="P1", weight_kg=1,
                                  width_cm=10, length_cm=10, height_cm=10)
    assert payload["orderId"] == "nahlah-4-9"
    assert payload["createShipment"] is False
    assert payload["amount_due"] == 125
    assert payload["customer"]["shortAddressCode"] == "ABCD1234"
    assert payload["items"][0]["sku"] == "SKU1"
