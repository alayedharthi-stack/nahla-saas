"""Dormant Shopify privacy compliance webhooks — database-free unit tests.

No network, no database: the router runs on a bare FastAPI app with the
storage seam replaced by recording doubles. The app secret, every email,
phone and token-shaped value is generated at runtime. Neutral generic
merchants only. The PostgreSQL proofs (migration 0123, receipts, replay,
reinstall, worker fences, cleanup scope) live in
``backend/tests/test_shopify_privacy_webhooks_pg.py``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from services.shopify_privacy import headers as H
from services.shopify_privacy import payloads as P
from services.shopify_privacy.config import INTAKE_ENABLED_ENV, MAX_BODY_BYTES, MAX_ORDER_IDS, TOPIC_PATHS
from services.shopify_privacy.models import (
    PRIVACY_TABLES,
    REF_EMAIL,
    REF_ID,
    REF_NONE,
    REF_PHONE,
    TOPIC_CUSTOMERS_DATA_REQUEST as DR,
    TOPIC_CUSTOMERS_REDACT as CR,
    TOPIC_SHOP_REDACT as SR,
)

_REPO = Path(__file__).resolve().parents[1]
SHOP = "generic-shoes-store.myshopify.com"
SHOP_ID = 7100000001
SECRET = "privacy-test-secret-" + secrets.token_hex(16)


def _email() -> str:
    return f"nora.{secrets.token_hex(4)}@example.test"


def _phone() -> str:
    return "+9665" + str(secrets.randbelow(10 ** 8)).zfill(8)


def data_request_body(**over):
    body = {"shop_id": SHOP_ID, "shop_domain": SHOP, "orders_requested": [299938, 280263],
            "customer": {"id": 191167, "email": _email(), "phone": _phone()}, "data_request": {"id": 9999}}
    body.update(over)
    return body


def customers_redact_body(**over):
    body = {"shop_id": SHOP_ID, "shop_domain": SHOP,
            "customer": {"id": 191167, "email": _email(), "phone": _phone()}, "orders_to_redact": [299938]}
    body.update(over)
    return body


def shop_redact_body(**over):
    body = {"shop_id": SHOP_ID, "shop_domain": SHOP}
    body.update(over)
    return body


BODIES = {DR: data_request_body, CR: customers_redact_body, SR: shop_redact_body}


def raw(body) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode()


def sign(data: bytes, secret: str = SECRET) -> str:
    return base64.b64encode(hmac.new(secret.encode(), data, hashlib.sha256).digest()).decode()


# ── Signed-body classification and strict parsing ─────────────────────────────

@pytest.mark.parametrize("topic", [DR, CR, SR])
def test_each_official_shape_classifies_as_its_own_topic(topic):
    assert P.classify(BODIES[topic]()) == topic
    parsed = P.parse_privacy_body(raw(BODIES[topic]()), topic)
    assert parsed.topic == topic and parsed.shop_domain == SHOP and parsed.shop_gid == f"gid://shopify/Shop/{SHOP_ID}"


@pytest.mark.parametrize("body_topic", [DR, CR, SR])
@pytest.mark.parametrize("claimed", [DR, CR, SR])
def test_a_signed_body_is_never_actionable_under_another_topic(body_topic, claimed):
    data = raw(BODIES[body_topic]())
    if body_topic == claimed:
        assert P.parse_privacy_body(data, claimed).topic == claimed
        return
    with pytest.raises(P.PayloadRejected) as excinfo:
        P.parse_privacy_body(data, claimed)
    assert excinfo.value.code == P.C_TOPIC_BODY_MISMATCH


@pytest.mark.parametrize("shape", [
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "customer": {"id": 1}},                       # no distinguishing array
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "orders_to_redact": []},                      # redact without customer
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "customer": {"id": 1}, "orders_requested": []},   # no data_request
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "customer": {"id": 1}, "data_request": {"id": 2}},  # no orders_requested
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "orders_requested": [], "data_request": {"id": 2}},  # no customer
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "customer": {"id": 1}, "orders_requested": [],
     "data_request": {"id": 2}, "orders_to_redact": []},                                     # both topics' markers
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "customer": {"id": 1}, "orders_to_redact": [],
     "orders_requested": []},
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "orders_requested": []},
    {"shop_id": SHOP_ID, "shop_domain": SHOP, "data_request": {"id": 2}},
])
def test_conflicting_or_incomplete_shapes_classify_as_nothing(shape):
    assert P.classify(shape) is None
    for topic in (DR, CR, SR):
        with pytest.raises(P.PayloadRejected) as excinfo:
            P.parse_privacy_body(raw(shape), topic)
        assert excinfo.value.code == P.C_TOPIC_BODY_MISMATCH


def test_customer_identifier_kinds_without_requiring_an_id():
    cases = [
        ({"id": 5, "email": _email()}, REF_ID),
        ({"email": _email()}, REF_EMAIL),            # email-only customer record
        ({"id": None, "email": _email(), "phone": None}, REF_EMAIL),
        ({"phone": _phone()}, REF_PHONE),
        ({}, REF_NONE),
        ({"email": "   "}, REF_NONE),
    ]
    for customer, kind in cases:
        for topic in (DR, CR):
            parsed = P.parse_privacy_body(raw(BODIES[topic](customer=customer)), topic)
            assert parsed.customer_ref_kind == kind


def test_parsed_payload_carries_no_customer_value():
    email, phone = _email(), _phone()
    for topic in (DR, CR):
        parsed = P.parse_privacy_body(raw(BODIES[topic](customer={"id": 44, "email": email, "phone": phone})), topic)
        text = repr(parsed) + json.dumps(parsed.__dict__)
        assert email not in text and phone not in text and "44" not in text.replace(str(SHOP_ID), "")
    parsed = P.parse_privacy_body(raw(customers_redact_body(orders_to_redact=[11, 22, 33])), CR)
    assert parsed.orders_count == 3 and parsed.data_request_id is None
    parsed = P.parse_privacy_body(raw(data_request_body(orders_requested=[], data_request={"id": 77})), DR)
    assert parsed.orders_count == 0 and parsed.data_request_id == 77


@pytest.mark.parametrize("topic,mutation", [
    (CR, {"orders_to_redact": None}),
    (CR, {"orders_to_redact": "299938"}),
    (CR, {"orders_to_redact": [0]}),
    (CR, {"orders_to_redact": [-1]}),
    (CR, {"orders_to_redact": [True]}),
    (CR, {"orders_to_redact": ["1"]}),
    (CR, {"orders_to_redact": [1.5]}),
    (CR, {"orders_to_redact": list(range(1, MAX_ORDER_IDS + 2))}),
    (DR, {"orders_requested": None}),
    (DR, {"orders_requested": [10 ** 20]}),
    (DR, {"data_request": {}}),
    (DR, {"data_request": {"id": "9999"}}),
    (DR, {"data_request": {"id": 0}}),
    (DR, {"data_request": []}),
    (DR, {"customer": "x"}),
    (DR, {"customer": {"id": "191167"}}),
    (CR, {"customer": {"id": True}}),
    (CR, {"customer": {"email": 5}}),
    (CR, {"customer": {"email": "a" * 400}}),
    (CR, {"customer": {"phone": ["1"]}}),
])
def test_distinguishing_fields_are_bounded_and_typed(topic, mutation):
    with pytest.raises(P.PayloadRejected) as excinfo:
        P.parse_privacy_body(raw(BODIES[topic](**mutation)), topic)
    assert excinfo.value.code == P.C_PAYLOAD_INVALID


@pytest.mark.parametrize("domain", [
    "Generic-Shoes-Store.myshopify.com", "generic-shoes-store.myshopify.com.", "a.b.myshopify.com",
    "xn--80ak6aa92e.myshopify.com", "generic-shoes-store.example.com", "https://generic-shoes-store.myshopify.com",
    " generic-shoes-store.myshopify.com", "generic-shoes-store.myshopify.com\n", "", None, 5,
])
def test_signed_shop_domain_must_be_exactly_canonical(domain):
    for topic in (DR, CR, SR):
        with pytest.raises(P.PayloadRejected) as excinfo:
            P.parse_privacy_body(raw(BODIES[topic](shop_domain=domain)), topic)
        assert excinfo.value.code == P.C_SHOP_IDENTITY_INVALID


@pytest.mark.parametrize("shop_id", [0, -5, True, "7100000001", 7.5, 10 ** 20, None])
def test_signed_shop_id_must_be_a_positive_integer(shop_id):
    with pytest.raises(P.PayloadRejected) as excinfo:
        P.parse_privacy_body(raw(shop_redact_body(shop_id=shop_id)), SR)
    assert excinfo.value.code == P.C_SHOP_IDENTITY_INVALID


@pytest.mark.parametrize("data", [
    b'{"shop_id": 1, "shop_id": 2, "shop_domain": "generic-shoes-store.myshopify.com"}',  # duplicate key
    b'{"shop_id": NaN, "shop_domain": "generic-shoes-store.myshopify.com"}',
    b'[{"shop_id": 1}]', b'"x"', b"", b"\xff\xfe{}", b"{", b"[" * 5000 + b"]" * 5000,
])
def test_strict_json_object_only(data):
    with pytest.raises(P.PayloadRejected) as excinfo:
        P.parse_privacy_body(data, SR)
    assert excinfo.value.code == P.C_PAYLOAD_INVALID


def test_unknown_topic_claim_is_refused():
    with pytest.raises(P.PayloadRejected):
        P.parse_privacy_body(raw(shop_redact_body()), "app/uninstalled")


# ── Unsigned diagnostic headers ───────────────────────────────────────────────

def test_webhook_id_kept_only_as_a_canonical_uuid():
    assert H.canonical_webhook_id("B54557E4-BDD9-4B37-8A5F-BF7D70BCD043") == "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043"
    token = "shpat_" + secrets.token_hex(16)
    for value in (_email(), token, "b54557e4bdd94b378a5fbf7d70bcd043", "b54557e4-bdd9-4b37-8a5f-bf7d70bcd04",
                  "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043\n", "g54557e4-bdd9-4b37-8a5f-bf7d70bcd043",
                  "ｂ54557e4-bdd9-4b37-8a5f-bf7d70bcd04", "", None, "x" * 5000):
        assert H.canonical_webhook_id(value) is None


def test_triggered_at_kept_only_as_a_parsed_timestamp():
    assert H.parse_triggered_at("2026-10-09T18:00:27.877041743Z") == datetime(
        2026, 10, 9, 18, 0, 27, 877041, tzinfo=timezone.utc)
    assert H.parse_triggered_at("2026-10-09T21:00:27+03:00") == datetime(2026, 10, 9, 18, 0, 27, tzinfo=timezone.utc)
    for value in (_email(), "shpat_" + secrets.token_hex(16), "2026-10-09 18:00:27Z", "2026-13-09T18:00:27Z",
                  "2026-10-09T18:00:27", "2026-10-09T18:00:27+24:00", "1999-01-01T00:00:00Z",
                  "2026-10-09T18:00:27.1234567890Z", "", None, "2" * 100):
        assert H.parse_triggered_at(value) is None


# ── Schema shape (no PII columns, separate metadata, chained revision) ────────

def test_privacy_tables_have_no_column_for_raw_or_personal_values():
    forbidden = ("email", "phone", "raw", "body_text", "payload", "hmac", "signature", "order_ids", "customer_id",
                 "headers", "secret", "token")
    for table in PRIVACY_TABLES:
        for column in table.columns:
            assert not any(f in column.name for f in forbidden), (table.name, column.name)


def test_privacy_tables_never_reach_startup_create_all():
    from models import Base

    names = set(Base.metadata.tables)
    assert not ({t.name for t in PRIVACY_TABLES} & names)
    main_source = (_REPO / "backend" / "main.py").read_text(encoding="utf-8")
    assert "create_privacy_tables" not in main_source


def test_revision_0123_chains_on_0122_and_declares_the_same_tables():
    import importlib.util

    path = _REPO / "database" / "migrations" / "versions" / "0123_shopify_privacy_requests.py"
    spec = importlib.util.spec_from_file_location("m0123", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0123" and module.down_revision == "0122"
    defs = module._definitions()
    for table in PRIVACY_TABLES:
        assert sorted(c.name for c in defs[table.name].columns) == sorted(c.name for c in table.columns)


def test_audited_schema_snapshot_equals_the_shopify_models():
    """The bounded no-data assessment is only as good as this pin: any column
    change in an audited table must bump AUDIT_VERSION and redo the audit."""
    from services.shopify_catalog.models import CATALOG_TABLES
    from services.shopify_connection.models import SHOPIFY_TABLES
    from services.shopify_privacy.audit import AUDITED_SCOPE, AUDITED_SHOPIFY_SCHEMA

    models = {t.name: frozenset(c.name for c in t.columns) for t in list(SHOPIFY_TABLES) + list(CATALOG_TABLES)}
    assert models == AUDITED_SHOPIFY_SCHEMA
    personal = ("email", "phone", "customer", "order", "address", "first_name", "last_name")
    # Reviewed: a location's boolean capability flag, not an order resource.
    reviewed = {("shopify_catalog_locations", "fulfills_online_orders")}
    for table, columns in AUDITED_SHOPIFY_SCHEMA.items():
        assert not [c for c in columns if any(p in c for p in personal) and (table, c) not in reviewed], table
    assert AUDITED_SCOPE["catalog_free_text"] == "not_assessed"
    assert AUDITED_SCOPE["generic_customer_mapping"] == "unresolved"
    assert AUDITED_SCOPE["legal_fulfillment"] == "not_asserted"


def test_no_approved_erasure_policy_ships():
    from services.shopify_privacy import cleanup

    assert cleanup.APPROVED_POLICY is None and cleanup.approved_policy() is None


def test_executor_refuses_without_flag_or_policy_before_touching_storage():
    from services.shopify_privacy import cleanup

    class _NoDb:
        def rollback(self):
            pass

        def get(self, *a, **k):
            raise AssertionError("storage must not be read")

    policy = cleanup.ApprovedErasurePolicy(version="synthetic-v1", approved_by="test", scope=frozenset({"catalog_mirror"}))
    with pytest.raises(cleanup.ExecutionRefused) as excinfo:
        cleanup.execute_shop_redaction(_NoDb(), request_id=1, policy=policy, operator_ref="op-1", env={})
    assert excinfo.value.code == cleanup.C_DESTRUCTIVE_DISABLED
    env = {"NAHLA_SHOPIFY_PRIVACY_DESTRUCTIVE_ENABLED": "1"}
    for bad in (None, cleanup.approved_policy(),
                cleanup.ApprovedErasurePolicy(version="v", approved_by="x", scope=frozenset({"other"}))):
        with pytest.raises(cleanup.ExecutionRefused) as excinfo:
            cleanup.execute_shop_redaction(_NoDb(), request_id=1, policy=bad, operator_ref="op-1", env=env)
        assert excinfo.value.code == cleanup.C_POLICY_NOT_APPROVED
    with pytest.raises(cleanup.ExecutionRefused) as excinfo:
        cleanup.execute_shop_redaction(_NoDb(), request_id=1, policy=policy, operator_ref=_email(), env=env)
    assert excinfo.value.code == cleanup.C_OPERATOR_REF_INVALID


def test_worker_is_dormant_without_its_flag():
    from services.shopify_privacy import worker

    def _factory():
        raise AssertionError("no database access while disabled")

    assert worker.run_tick(session_factory=_factory, env={}) == {"skipped": "disabled"}


# ── Router on a bare app with a recording storage seam ────────────────────────

class _Db:
    def __init__(self):
        self.rollbacks = 0
        self.closed = 0

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


@pytest.fixture()
def intake(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from routers import shopify_privacy_webhooks as router_module

    monkeypatch.setenv(INTAKE_ENABLED_ENV, "1")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", SECRET)
    db = _Db()
    state = {"tables": True, "fail": None, "calls": []}

    def record(db_, **kwargs):
        if state["fail"] is not None:
            raise state["fail"]
        state["calls"].append(kwargs)
        return "received", 1

    monkeypatch.setattr(router_module.store, "tables_present", lambda _db: state["tables"])
    monkeypatch.setattr(router_module.store, "record_receipt", record)
    router_module.set_receipt_session_factory(lambda: db)
    app = FastAPI()
    app.include_router(router_module.router)

    @app.get("/unrelated")
    async def unrelated():
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    client.state = state
    client.db = db
    client.app_ = app
    client.router_module = router_module
    yield client
    router_module.set_receipt_session_factory(None)


def _headers(data: bytes, topic: str, **over):
    h = {"X-Shopify-Hmac-Sha256": sign(data), "X-Shopify-Topic": topic, "X-Shopify-Shop-Domain": SHOP,
         "Content-Type": "application/json", "X-Shopify-Webhook-Id": "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043",
         "X-Shopify-Triggered-At": "2026-10-09T18:00:27.877041743Z"}
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


def test_routes_are_absent_while_the_intake_flag_is_off(intake, monkeypatch):
    monkeypatch.delenv(INTAKE_ENABLED_ENV)
    for topic, path in list(TOPIC_PATHS.items()) + [(SR, "/webhooks/shopify/privacy")]:
        data = raw(BODIES[topic]())
        resp = intake.post(path, content=data, headers=_headers(data, topic))
        assert resp.status_code == 404
    assert intake.state["calls"] == []


@pytest.mark.parametrize("topic", [DR, CR, SR])
def test_valid_signed_delivery_is_acknowledged_after_the_receipt(intake, topic):
    data = raw(BODIES[topic]())
    for path in (TOPIC_PATHS[topic], "/webhooks/shopify/privacy"):
        resp = intake.post(path, content=data, headers=_headers(data, topic))
        assert resp.status_code == 200 and resp.json() == {"ok": True}
        assert resp.headers["cache-control"] == "no-store"
    call = intake.state["calls"][-1]
    assert call["payload"].topic == topic
    assert call["body_sha256"] == hashlib.sha256(data).hexdigest()
    assert call["webhook_id"] == "b54557e4-bdd9-4b37-8a5f-bf7d70bcd043"
    assert call["triggered_at"] == datetime(2026, 10, 9, 18, 0, 27, 877041, tzinfo=timezone.utc)


def test_hmac_covers_the_raw_bytes_not_the_json_value(intake):
    body = shop_redact_body()
    signed = raw(body)
    reencoded = json.dumps(body, indent=2).encode()
    resp = intake.post(TOPIC_PATHS[SR], content=reencoded, headers=_headers(signed, SR))
    assert resp.status_code == 401 and resp.json() == {"error": "unauthorized"}
    assert intake.post(TOPIC_PATHS[SR], content=signed, headers=_headers(signed, SR)).status_code == 200


@pytest.mark.parametrize("signature", [
    "wrong", sign(b"other-body"), sign(b"{}", "another-secret"), "", "!!!!", base64.b64encode(b"x" * 32).decode(),
    "A" * 200,
])
def test_wrong_or_malformed_signatures_are_401(intake, signature):
    data = raw(shop_redact_body())
    resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR, **{"X-Shopify-Hmac-Sha256": signature}))
    assert resp.status_code == 401
    assert intake.state["calls"] == []


def test_missing_or_repeated_signature_header_is_401(intake):
    data = raw(shop_redact_body())
    h = _headers(data, SR)
    del h["X-Shopify-Hmac-Sha256"]
    assert intake.post(TOPIC_PATHS[SR], content=data, headers=h).status_code == 401
    pairs = list(_headers(data, SR).items()) + [("X-Shopify-Hmac-Sha256", sign(data))]
    assert intake.post(TOPIC_PATHS[SR], content=data, headers=pairs).status_code == 401
    assert intake.state["calls"] == []


def test_missing_secret_fails_closed_with_401(intake, monkeypatch, caplog):
    monkeypatch.delenv("SHOPIFY_CLIENT_SECRET")
    data = raw(shop_redact_body())
    with caplog.at_level(logging.INFO, logger="nahla.shopify_privacy"):
        resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR))
    assert resp.status_code == 401 and intake.state["calls"] == []
    assert "secret_missing" in caplog.text and SHOP not in caplog.text


def test_oversized_bodies_are_refused_before_authentication(intake):
    data = raw(shop_redact_body())
    big = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR, **{"Content-Length": str(MAX_BODY_BYTES + 1)}))
    assert big.status_code == 413

    def stream():
        for _ in range(MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024

    h = _headers(data, SR)
    resp = intake.post(TOPIC_PATHS[SR], content=stream(), headers=h)
    assert resp.status_code == 413 and intake.state["calls"] == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/json; charset=latin-1", "application/xml",
                                          "application/json-patch+json", None])
def test_content_type_must_be_json_after_authentication(intake, content_type):
    data = raw(shop_redact_body())
    resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR, **{"Content-Type": content_type}))
    assert resp.status_code == 415
    bad_sig = _headers(data, SR, **{"Content-Type": content_type, "X-Shopify-Hmac-Sha256": "x"})
    assert intake.post(TOPIC_PATHS[SR], content=data, headers=bad_sig).status_code == 401
    ok = _headers(data, SR, **{"Content-Type": "application/json; charset=utf-8"})
    assert intake.post(TOPIC_PATHS[SR], content=data, headers=ok).status_code == 200


def test_topic_header_must_exist_be_known_and_agree_with_the_route(intake):
    data = raw(shop_redact_body())
    h = _headers(data, SR)
    del h["X-Shopify-Topic"]
    assert intake.post(TOPIC_PATHS[SR], content=data, headers=h).json() == {"error": "topic_invalid"}
    assert intake.post(TOPIC_PATHS[SR], content=data,
                       headers=_headers(data, "app/uninstalled")).json() == {"error": "topic_invalid"}
    resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, CR))
    assert resp.status_code == 400 and resp.json() == {"error": "topic_mismatch"}
    assert intake.state["calls"] == []


@pytest.mark.parametrize("body_topic", [DR, CR, SR])
@pytest.mark.parametrize("claimed", [DR, CR, SR])
def test_cross_topic_forgery_through_routes_and_headers(intake, body_topic, claimed):
    """A retained signed body re-sent with another unsigned route and topic is never stored."""
    if body_topic == claimed:
        return
    data = raw(BODIES[body_topic]())
    for path in (TOPIC_PATHS[claimed], "/webhooks/shopify/privacy"):
        resp = intake.post(path, content=data, headers=_headers(data, claimed))
        assert resp.status_code == 400 and resp.json() == {"error": "topic_body_mismatch"}
    # Route and header disagreeing with each other is refused before the body is read.
    resp = intake.post(TOPIC_PATHS[claimed], content=data, headers=_headers(data, body_topic))
    assert resp.status_code == 400 and resp.json() == {"error": "topic_mismatch"}
    assert intake.state["calls"] == []


@pytest.mark.parametrize("shop_header", [SHOP.upper(), "other-store.myshopify.com", None, ""])
def test_unsigned_shop_header_must_equal_the_signed_domain(intake, shop_header):
    data = raw(shop_redact_body())
    resp = intake.post(TOPIC_PATHS[SR], content=data,
                       headers=_headers(data, SR, **{"X-Shopify-Shop-Domain": shop_header}))
    assert resp.status_code == 400 and resp.json()["error"] in ("shop_mismatch", "header_invalid")
    assert intake.state["calls"] == []


def test_storage_absent_or_failing_is_never_acknowledged(intake):
    data = raw(shop_redact_body())
    intake.state["tables"] = False
    resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR))
    assert resp.status_code == 503 and resp.json() == {"error": "retry"}
    intake.state["tables"] = True
    for failure in (OperationalError("INSERT", {}, Exception("lock timeout")), RuntimeError("commit failed")):
        intake.state["fail"] = failure
        resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR))
        assert resp.status_code == 503 and resp.json() == {"error": "retry"}
    assert intake.db.rollbacks == 3 and intake.db.closed == 3  # every failed receipt rolls back and closes


def test_no_personal_value_header_or_secret_reaches_logs_or_storage(intake, caplog):
    email, phone = _email(), _phone()
    token = "shpat_" + secrets.token_hex(16)
    data = raw(customers_redact_body(customer={"id": 191167, "email": email, "phone": phone}))
    hostile = _headers(data, CR, **{"X-Shopify-Webhook-Id": email, "X-Shopify-Triggered-At": token})
    with caplog.at_level(logging.DEBUG):
        assert intake.post(TOPIC_PATHS[CR], content=data, headers=hostile).status_code == 200
        intake.post(TOPIC_PATHS[CR], content=data, headers=_headers(data, CR, **{"X-Shopify-Hmac-Sha256": "x"}))
        intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR))
        intake.state["fail"] = RuntimeError(f"boom {email}")
        intake.post(TOPIC_PATHS[CR], content=data, headers=hostile)
    call = intake.state["calls"][0]
    assert call["webhook_id"] is None and call["triggered_at"] is None
    stored = repr(call["payload"]) + json.dumps(call["payload"].__dict__) + call["body_sha256"]
    logged = caplog.text + "".join(repr(r.args) for r in caplog.records)
    for secret_value in (email, phone, token, SECRET, sign(data), data.decode()):
        assert secret_value not in stored
        assert secret_value not in logged


def test_middleware_fallback_keeps_privacy_failures_non_2xx():
    """The last-resort fallback rewrites only the singular /webhook/ prefix to 200."""
    from starlette.requests import Request

    from core.middleware import _safe_fallback_response

    def req(path):
        return Request({"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b""})

    for path in list(TOPIC_PATHS.values()) + ["/webhooks/shopify/privacy"]:
        assert _safe_fallback_response(req(path), 500, exc=RuntimeError("x")).status_code == 500
    assert _safe_fallback_response(req("/webhook/meta"), 500).status_code == 200  # the boundary avoided


_STACK_PROBE = r"""
import base64, hashlib, hmac, json, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/backend", sys.argv[1] + "/database"]
from fastapi.testclient import TestClient
import main
from routers import shopify_privacy_webhooks as R
from services.shopify_privacy import store

class FakeDb:
    def rollback(self):
        pass

    def close(self):
        pass

R.set_receipt_session_factory(lambda: FakeDb())

secret = sys.argv[2]
body = json.dumps({"shop_id": 7100000001, "shop_domain": "generic-shoes-store.myshopify.com"}).encode()
headers = {"X-Shopify-Hmac-Sha256": base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode(),
           "X-Shopify-Topic": "shop/redact", "X-Shopify-Shop-Domain": "generic-shoes-store.myshopify.com",
           "Content-Type": "application/json"}
client = TestClient(main.app, raise_server_exceptions=False)
out = {}

def exploding_parse(raw, topic):
    raise RuntimeError("validation exploded")

real_parse = R.parse_privacy_body
R.parse_privacy_body = exploding_parse          # escapes the handler: the middleware stack decides
out["escaped"] = client.post("/webhooks/shopify/privacy/shop-redact", content=body, headers=headers).status_code
R.parse_privacy_body = real_parse

def broken_inspect(db):
    raise RuntimeError("schema inspection exploded")

store.tables_present = broken_inspect           # fails on the receipt thread
out["inspection_failure"] = client.post("/webhooks/shopify/privacy/shop-redact", content=body, headers=headers).status_code
store.tables_present = lambda db: True

def failing_receipt(db, **kwargs):
    raise RuntimeError("receipt commit failed")

store.record_receipt = failing_receipt
out["receipt_failure"] = client.post("/webhooks/shopify/privacy/shop-redact", content=body, headers=headers).status_code
store.tables_present = lambda db: False
out["no_storage"] = client.post("/webhooks/shopify/privacy", content=body, headers=headers).status_code
headers["X-Shopify-Hmac-Sha256"] = "x"
out["bad_hmac"] = client.post("/webhooks/shopify/privacy/shop-redact", content=body, headers=headers).status_code
print(json.dumps(out))
"""


def test_privacy_failures_stay_non_2xx_through_the_full_middleware_stack():
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    secret = "stack-probe-secret-" + secrets.token_hex(12)
    env.update({INTAKE_ENABLED_ENV: "1", "SHOPIFY_CLIENT_SECRET": secret})
    proc = subprocess.run([sys.executable, "-c", _STACK_PROBE, str(_REPO), secret], cwd=str(_REPO), env=env,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr[-3000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out == {"escaped": 500, "inspection_failure": 503, "receipt_failure": 503, "no_storage": 503,
                   "bad_hmac": 401}, out


def test_sentry_scrubs_privacy_bodies_headers_and_withholds_frames():
    from core.observability_sentry import _scrub_event

    email, phone = _email(), _phone()
    header_email, header_token = _email(), "opaque" + secrets.token_hex(20)
    domain_email, topic_token = _email(), "tok_" + secrets.token_urlsafe(24)
    name_token = "X-" + secrets.token_hex(24)  # a token encoded as a valid header name
    data = raw(customers_redact_body(customer={"id": 191167, "email": email, "phone": phone}))
    signature = sign(data)
    event = {
        "request": {"url": "https://api.example.test/webhooks/shopify/privacy/customers-redact",
                    "headers": {"X-Shopify-Hmac-Sha256": signature, "X-Shopify-Topic": topic_token,
                                "X-Shopify-Webhook-Id": header_email, "X-Shopify-Triggered-At": header_token,
                                "X-Shopify-Shop-Domain": domain_email, "User-Agent": header_token,
                                name_token: "1"},
                    "data": json.loads(data)},
        "exception": {"values": [{"value": "receipt failed", "stacktrace": {"frames": [
            {"module": "services.shopify_privacy.store", "vars": {"raw": data.decode(), "email": email}},
            {"module": "routers.shopify_privacy_webhooks", "vars": {"raw": data.decode(), "signature": signature}},
            {"filename": "/app/backend/services/shopify_privacy/payloads.py", "abs_path":
             "/app/backend/services/shopify_privacy/payloads.py", "vars": {"payload": json.loads(data)}},
        ]}}]},
    }
    scrubbed = json.dumps(_scrub_event(event))
    for value in (email, phone, signature, header_email, header_token, domain_email, topic_token,
                  name_token, name_token[2:]):
        assert value not in scrubbed
    assert event["request"]["data"] == "[scrubbed]"
    assert event["request"]["headers"] == {"[withheld]": "shopify webhook headers"}
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert all(f["vars"] == {"[withheld]": "sensitive frame"} for f in frames)


# ── Review regressions: persisted bounds ──────────────────────────────────────

def test_every_persisted_fixed_code_fits_its_column():
    """A code longer than its column would make a normal outcome fail to
    persist and retry forever."""
    import inspect as pyinspect

    from services.shopify_connection.models import ShopifyConnectionAudit
    from services.shopify_privacy import audit, store, worker
    from services.shopify_privacy.models import ShopifyPrivacyRequest, ShopifyPrivacyRequestEvent

    reason_limit = ShopifyPrivacyRequest.__table__.c.reason.type.length
    assert ShopifyPrivacyRequestEvent.__table__.c.reason.type.length == reason_limit
    reasons = {v for k, v in vars(worker).items() if k.startswith("R_") and isinstance(v, str)}
    reasons |= {"audit_table_missing", "audit_schema_drift", "in_flight_authorization", "shop_lease_held",
                "import_running", store.REASON_REDELIVERED}
    # Every literal reason the audit module can return is in the set above.
    source = pyinspect.getsource(audit)
    for literal in ("audit_table_missing", "audit_schema_drift", "in_flight_authorization", "shop_lease_held",
                    "import_running"):
        assert f'"{literal}"' in source
    assert worker.R_NO_CUSTOMER_RESOURCES == "no_structured_customer_records_in_audited_scope"
    too_long = sorted(r for r in reasons if len(r) > reason_limit)
    assert not too_long, too_long
    action_limit = ShopifyPrivacyRequestEvent.__table__.c.action.type.length
    for action in ("received", "redelivered", "requeued", "evaluated", "executed"):
        assert len(action) <= action_limit
    assert len("privacy_shop_redact_probe_requested") <= ShopifyConnectionAudit.__table__.c.action.type.length
    from services.shopify_privacy.config import AUDIT_VERSION

    assert len(AUDIT_VERSION) <= ShopifyPrivacyRequest.__table__.c.audit_version.type.length
    state_limit = ShopifyPrivacyRequest.__table__.c.state.type.length
    from services.shopify_privacy.models import STATES

    assert all(len(s) <= state_limit for s in STATES)


def test_data_request_id_is_bounded_at_signed_bigint():
    top = 2 ** 63 - 1
    assert P.parse_privacy_body(raw(data_request_body(data_request={"id": top})), DR).data_request_id == top
    for value in (2 ** 63, 10 ** 19, 10 ** 20 - 1):
        with pytest.raises(P.PayloadRejected) as excinfo:
            P.parse_privacy_body(raw(data_request_body(data_request={"id": value})), DR)
        assert excinfo.value.code == P.C_PAYLOAD_INVALID


def test_bigint_overflow_is_a_deterministic_400_not_a_storage_retry(intake):
    for value in (2 ** 63, 10 ** 20):
        data = raw(data_request_body(data_request={"id": value}))
        resp = intake.post(TOPIC_PATHS[DR], content=data, headers=_headers(data, DR))
        assert resp.status_code == 400 and resp.json() == {"error": "payload_invalid"}
    assert intake.state["calls"] == []
    data = raw(data_request_body(data_request={"id": 2 ** 63 - 1}))
    assert intake.post(TOPIC_PATHS[DR], content=data, headers=_headers(data, DR)).status_code == 200
    assert intake.state["calls"][-1]["payload"].data_request_id == 2 ** 63 - 1


def test_worker_backoff_exponent_is_clamped():
    from services.shopify_privacy import config, worker

    assert worker.backoff_seconds(1) == config.WORKER_BACKOFF_BASE_SECONDS
    assert worker.backoff_seconds(0) == config.WORKER_BACKOFF_BASE_SECONDS
    assert worker.backoff_seconds(10 ** 12) == config.WORKER_BACKOFF_CAP_SECONDS


def test_sentry_header_withholding_is_narrow_to_shopify_webhooks():
    from core.observability_sentry import _scrub_event

    event = {"request": {"url": "https://api.example.test/merchant/catalog", "headers": {"User-Agent": "probe/1.0"},
                         "data": {"name": "قميص قطني أزرق"}}}
    _scrub_event(event)
    assert event["request"]["headers"]["User-Agent"] == "probe/1.0"
    assert event["request"]["data"] != "[scrubbed]"


# ── Event loop and delivery budget ────────────────────────────────────────────

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _async_post(app, path, data, headers):
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(path, content=data, headers=headers)
    return go


def test_delayed_inspection_is_bounded_and_never_blocks_the_event_loop(intake, monkeypatch):
    """A stalled schema inspection runs on the receipt pool: the delivery gets a
    503 within the budget while an unrelated endpoint keeps answering."""
    import httpx

    from services.shopify_privacy import config

    release = threading.Event()
    finished = threading.Event()

    def slow_inspection(_db):
        release.wait(10)
        finished.set()
        return True

    monkeypatch.setattr(intake.router_module.store, "tables_present", slow_inspection)
    monkeypatch.setattr(intake.router_module, "RECEIPT_BUDGET_SECONDS", 0.5)
    data = raw(shop_redact_body())

    async def scenario():
        transport = httpx.ASGITransport(app=intake.app_)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            delivery = asyncio.create_task(client.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR)))
            await asyncio.sleep(0.05)
            probe_started = time.monotonic()
            unrelated = await client.get("/unrelated")
            probe_elapsed = time.monotonic() - probe_started
            resp = await delivery
            return resp, unrelated, probe_elapsed, time.monotonic() - started

    resp, unrelated, probe_elapsed, elapsed = asyncio.run(scenario())
    assert unrelated.status_code == 200 and probe_elapsed < 0.3
    assert resp.status_code == 503 and resp.json() == {"error": "retry"}
    assert elapsed < 0.5 + 0.5
    assert config.RECEIPT_BUDGET_SECONDS + config.RECEIPT_BODY_READ_SECONDS < 5.0
    release.set()
    assert finished.wait(5)
    # The residual thread released its slot when it ended (bounded residual work).
    deadline = time.monotonic() + 5
    while intake.router_module._SLOTS._value != config.RECEIPT_WORKERS and time.monotonic() < deadline:
        time.sleep(0.01)
    assert intake.router_module._SLOTS._value == config.RECEIPT_WORKERS
    assert intake.state["calls"] == []  # the receipt checked the deadline and never committed


def test_exhausted_receipt_slots_answer_503_at_once_without_backlog(intake, monkeypatch):
    from services.shopify_privacy import config

    release = threading.Event()
    started = threading.Semaphore(0)

    def blocked_inspection(_db):
        started.release()
        release.wait(10)
        return True

    monkeypatch.setattr(intake.router_module.store, "tables_present", blocked_inspection)
    monkeypatch.setattr(intake.router_module, "RECEIPT_BUDGET_SECONDS", 0.3)
    data = raw(shop_redact_body())
    for _ in range(config.RECEIPT_WORKERS):
        assert intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR)).status_code == 503
    for _ in range(config.RECEIPT_WORKERS):
        assert started.acquire(timeout=5)
    t0 = time.monotonic()
    resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR))
    assert resp.status_code == 503 and time.monotonic() - t0 < 0.25   # no slot: refused immediately
    release.set()
    deadline = time.monotonic() + 5
    while intake.router_module._SLOTS._value != config.RECEIPT_WORKERS and time.monotonic() < deadline:
        time.sleep(0.01)
    assert intake.router_module._SLOTS._value == config.RECEIPT_WORKERS
    assert intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR)).status_code == 200


def test_exhausted_pool_checkout_is_a_fast_503(intake, monkeypatch):
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    def pool_exhausted(_db):
        raise PoolTimeout("QueuePool limit reached")

    monkeypatch.setattr(intake.router_module.store, "record_receipt", lambda db, **k: pool_exhausted(db))
    data = raw(shop_redact_body())
    t0 = time.monotonic()
    resp = intake.post(TOPIC_PATHS[SR], content=data, headers=_headers(data, SR))
    assert resp.status_code == 503 and time.monotonic() - t0 < 1.0


def test_dedicated_receipt_engine_has_its_own_tight_bounds():
    from services.shopify_privacy import config, store

    factory = store.dedicated_session_factory("postgresql+psycopg2://nahla:nahla_password@127.0.0.1:1/none")
    engine = factory.kw["bind"]
    try:
        assert engine.pool.size() == config.RECEIPT_WORKERS and engine.pool._max_overflow == 0
        assert engine.pool._timeout == config.RECEIPT_POOL_TIMEOUT_SECONDS
        assert config.RECEIPT_POOL_TIMEOUT_SECONDS + config.RECEIPT_CONNECT_TIMEOUT_SECONDS <= config.RECEIPT_BUDGET_SECONDS
        assert config.RECEIPT_STATEMENT_TIMEOUT_MS / 1000 < config.RECEIPT_BUDGET_SECONDS
    finally:
        engine.dispose()
    import session as shared

    assert shared.engine.pool._timeout != config.RECEIPT_POOL_TIMEOUT_SECONDS or shared.engine is not engine


def test_slow_body_is_refused_with_408(intake, monkeypatch):
    import httpx

    monkeypatch.setattr(intake.router_module, "RECEIPT_BODY_READ_SECONDS", 0.2)
    data = raw(shop_redact_body())

    async def trickle():
        yield data[:5]
        await asyncio.sleep(1.0)
        yield data[5:]

    async def scenario():
        transport = httpx.ASGITransport(app=intake.app_)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            h = _headers(data, SR)
            return await client.post(TOPIC_PATHS[SR], content=trickle(), headers=h)

    resp = asyncio.run(scenario())
    assert resp.status_code == 408 and resp.json() == {"error": "body_timeout"}
    assert intake.state["calls"] == []


def test_receipt_rolls_back_instead_of_committing_after_the_deadline():
    from services.shopify_privacy import store

    class _Tx:
        committed = rolled_back = 0

        def commit(self):
            self.committed += 1

        def rollback(self):
            self.rolled_back += 1

    tx = _Tx()
    with pytest.raises(store.ReceiptUnavailable):
        store._commit_before(tx, time.monotonic() - 0.001)
    assert tx.committed == 0 and tx.rolled_back == 1
    store._commit_before(tx, time.monotonic() + 5)
    assert tx.committed == 1
