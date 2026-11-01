"""Salla App Settings secrets never persist from the webhook receivers.

``app.settings.updated`` (and legacy ``app.settings``) carry every App
Settings value in clear text, including Password-type fields such as
``handoffSecret``. These tests drive the real ``/webhook/salla`` and
``/webhook/salla-oauth`` coroutines with real Starlette requests and a real
SQLite ``webhook_events`` table (including the provider/event-id unique index)
and assert that synthetic sentinel secrets never reach ``raw_body``,
``parsed_payload``, ``raw_headers``, ``last_error``, logs, signature-audit
records or exception text — while signature, replay and identity still see the
original bytes and non-secret processing is unchanged.

All values are synthetic. Generic merchant data per AGENTS.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import JSON, create_engine, text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from core import salla_webhook_redaction as redaction
from core.salla_webhook_identity import build_salla_webhook_external_event_id
from database.models import Base, Integration, Tenant, TenantSettings, WebhookEvent
from routers import webhooks

# ── Synthetic sentinels (never real values) ──────────────────────────────────
# Built at runtime so no credential-shaped literal sits in the source.


def _synthetic(label: str) -> str:
    return "_".join(("SYNTHETIC", label.upper(), "SENTINEL", "VALUE"))


SECRET = _synthetic("handoff")
SECRET_2 = _synthetic("rotated")
UNKNOWN = _synthetic("unknown")
PIN = 8675309123
SENTINELS = (SECRET, SECRET_2, UNKNOWN, str(PIN))

SIGNING_KEY = _synthetic("webhook hmac")
OAUTH_SIGNING_KEY = _synthetic("oauth hmac")
WRONG_KEY = _synthetic("wrong hmac")
OAUTH_ACCESS = _synthetic("oauth access")
OAUTH_REFRESH = _synthetic("oauth refresh")
STORE = "9001001"
POLICY_HEADER = redaction.BODY_POLICY_HEADER

ENDPOINTS = {
    "salla": (webhooks.salla_webhook, "/webhook/salla", SIGNING_KEY),
    "salla_oauth": (webhooks.salla_oauth_webhook, "/webhook/salla-oauth", OAUTH_SIGNING_KEY),
}


def _settings(**extra):
    base = {
        "handoffSecret": SECRET,
        "handoffMode": "live",
        "pilotNote": UNKNOWN,
        "pilotPin": PIN,
        "nahla_enabled": False,
        "whatsapp_number": "+966500000003",
        "reply_tone": "رسمي",
        "abandoned_cart_enabled": True,
        "discount_percentage": 20,
        "autopilot_enabled": False,
    }
    base.update(extra)
    return base


def _settings_updated(**settings_extra):
    return {
        "event": "app.settings.updated",
        "merchant": int(STORE),
        "created_at": "2026-11-01 10:00:00",
        "data": {"id": 551, "app_name": "Nahla", "settings": _settings(**settings_extra)},
    }


def _legacy_settings():
    return {"event": "app.settings", "merchant": STORE, "data": {"settings": _settings()}}


def _encode(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _sign(body: bytes, key: str = SIGNING_KEY) -> str:
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _request(body: bytes, path: str, *, signature=None, extra_headers=None) -> Request:
    headers = {"content-type": "application/json", "user-agent": "Salla-Webhook-Test"}
    if signature is not None:
        headers["x-salla-signature"] = signature
    headers.update(extra_headers or {})
    scope = {
        "type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "https", "http_version": "1.1",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": ("203.0.113.10", 44321), "server": ("api.example.test", 443),
    }
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _make_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    saved = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    try:
        Base.metadata.create_all(engine)
    finally:
        for col, original in saved:
            col.type = original
    with engine.begin() as conn:
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_webhook_events_provider_event "
            "ON webhook_events (provider, external_event_id) "
            "WHERE external_event_id IS NOT NULL"
        ))
    return sessionmaker(bind=engine)(), engine


@pytest.fixture(autouse=True)
def _observe_emitted_logs():
    """Assert on what the code emits, not on the app-level log filter.

    ``install_log_redaction`` (called by ``main`` and some tests) leaves a
    record-rewriting filter on pytest's session-long root handlers, which
    could otherwise make a leaked secret look redacted. Detach it for the
    test and restore it afterwards.
    """
    from core.log_redaction import SecretRedactingFilter

    root = logging.getLogger()
    detached = []
    for target in (root, *root.handlers):
        for flt in list(target.filters):
            if isinstance(flt, SecretRedactingFilter):
                target.removeFilter(flt)
                detached.append((target, flt))
    try:
        yield
    finally:
        for target, flt in detached:
            target.addFilter(flt)


def test_log_capture_positive_control(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("nahla.obs").info('{"settings": {"handoffSecret": "%s"}}', SECRET)
    assert SECRET in _log_text(caplog)


@pytest.fixture
def env(monkeypatch, caplog):
    db, engine = _make_db()
    monkeypatch.setattr(webhooks, "SALLA_WEBHOOK_SECRET", SIGNING_KEY)
    monkeypatch.setattr(webhooks, "SALLA_OAUTH_WEBHOOK_SECRET", OAUTH_SIGNING_KEY)
    monkeypatch.setattr(webhooks, "SALLA_WEBHOOK_ALLOW_MISSING_SIGNATURE", False)
    enforce = {"value": True}
    monkeypatch.setattr(webhooks, "resolve_enforce", lambda *_a, **_k: enforce["value"])
    audits = []
    monkeypatch.setattr(
        webhooks, "_record_signature_audit",
        lambda result, **kwargs: audits.append((result, kwargs)),
    )
    caplog.set_level(logging.DEBUG)
    yield SimpleNamespace(db=db, enforce=enforce, audits=audits, caplog=caplog)
    db.close()
    engine.dispose()


def _call(env, endpoint: str, body: bytes, *, key=None, signature="auto", extra_headers=None):
    handler, path, default_key = ENDPOINTS[endpoint]
    if signature == "auto":
        signature = _sign(body, key or default_key)
    return asyncio.run(handler(_request(body, path, signature=signature, extra_headers=extra_headers), env.db))


def _rows(env):
    env.db.expire_all()
    return env.db.query(WebhookEvent).order_by(WebhookEvent.id).all()


def _log_text(caplog) -> str:
    parts = []
    formatter = logging.Formatter()
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info:
            parts.append(formatter.formatException(record.exc_info))
    return "\n".join(parts)


def _assert_no_sentinel(env, *extra_texts):
    texts = list(extra_texts)
    for ev in _rows(env):
        texts.append(json.dumps({
            "raw_body": ev.raw_body, "parsed_payload": ev.parsed_payload,
            "raw_headers": ev.raw_headers, "last_error": ev.last_error,
            "event_type": ev.event_type, "external_event_id": ev.external_event_id,
            "store_id": ev.store_id,
        }, ensure_ascii=False, default=str))
    texts.append(_log_text(env.caplog))
    texts.append(repr(env.audits))
    for settings in env.db.query(TenantSettings).all():
        texts.append(json.dumps(
            [settings.whatsapp_settings, settings.ai_settings, settings.store_settings,
             settings.extra_metadata], ensure_ascii=False, default=str,
        ))
    blob = "\n".join(texts)
    for sentinel in SENTINELS:
        assert sentinel not in blob


def _seed_tenant_with_integration(db, provider_cfg=None):
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    db.add(tenant)
    db.flush()
    db.add(Integration(
        tenant_id=tenant.id, provider="salla", external_store_id=STORE,
        config=provider_cfg or {"store_id": STORE}, enabled=True,
    ))
    db.commit()
    return tenant.id


def _dispatch(db, ev):
    from core.webhook_dispatcher import _process_event

    asyncio.run(_process_event(db, ev))
    db.expire_all()
    return db.get(WebhookEvent, ev.id)


# ── Authenticated, valid ─────────────────────────────────────────────────────


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_signed_settings_update_persists_only_nonsecret_values(env, endpoint):
    payload = _settings_updated()
    body = _encode(payload)
    result = _call(env, endpoint, body)

    assert result["status"] == "received"
    (ev,) = _rows(env)
    assert ev.signature_valid is True
    assert ev.status == "received"
    assert ev.event_type == "app.settings.updated"
    assert ev.store_id == STORE
    prefix = "salla" if endpoint == "salla" else "salla_oauth"
    assert ev.external_event_id == build_salla_webhook_external_event_id(
        event_type="app.settings.updated", parsed_payload=payload, provider_prefix=prefix,
    )
    settings = ev.parsed_payload["data"]["settings"]
    assert set(settings) == set(payload["data"]["settings"])  # field names kept
    assert settings["handoffSecret"] == redaction.REDACTED
    assert settings["pilotNote"] == redaction.REDACTED
    assert settings["pilotPin"] == redaction.REDACTED
    for key in redaction.PROCESSED_SETTINGS_KEYS:
        assert settings[key] == payload["data"]["settings"][key]
    assert json.loads(ev.raw_body) == ev.parsed_payload
    assert ev.raw_headers[POLICY_HEADER] == "salla-redaction-v1:sanitized"
    _assert_no_sentinel(env, json.dumps(result))


SHAPES = {
    "legacy_nested": _legacy_settings(),
    "legacy_flat": {
        "event": "app.settings", "merchant": STORE,
        "data": {"nahla_enabled": True, "handoffSecret": SECRET, "pin": PIN},
    },
    "grouped_and_pairs": {
        "event": "app.settings.updated", "merchant": STORE,
        "data": {"id": 552, "settings": {
            "handoff": {"secret": SECRET, "mode": "live"},
            "fields": [{"key": "handoffSecret", "value": SECRET}],
        }},
    },
    "settings_as_pair_list": {
        "event": "app.settings.updated", "merchant": STORE,
        "data": {"id": 553, "settings": [{"name": "handoffSecret", "value": SECRET}]},
    },
    "top_level_and_deep_settings": {
        "event": "app.settings.updated", "merchant": STORE,
        "settings": {"handoffSecret": SECRET},
        "data": {"id": 554, "app": {"settings": {"handoffSecret": SECRET}}, "note": UNKNOWN},
    },
    "event_name_case": {
        "event": "APP.SETTINGS.UPDATED", "merchant": STORE,
        "data": {"id": 555, "settings": {"Handoff_Secret": SECRET, "x": PIN}},
    },
    "other_app_event_with_settings": {
        "event": "app.updated", "merchant": STORE,
        "data": {"id": 556, "settings": {"handoffSecret": SECRET, "note": UNKNOWN},
                 "credentials": {"apiKey": SECRET, "adminPassword": SECRET}},
    },
}


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_nested_and_legacy_settings_shapes_never_persist_secret(env, endpoint, shape):
    result = _call(env, endpoint, _encode(SHAPES[shape]))
    assert result["status"] == "received"
    (ev,) = _rows(env)
    assert ev.signature_valid is True and ev.status == "received"
    assert ev.raw_headers[POLICY_HEADER] == "salla-redaction-v1:sanitized"
    assert json.loads(ev.raw_body) == ev.parsed_payload
    _assert_no_sentinel(env)


@pytest.mark.parametrize("shape", ["legacy_nested", "legacy_flat"])
def test_dispatcher_still_applies_nonsecret_settings_from_sanitized_row(env, shape):
    tenant_id = _seed_tenant_with_integration(env.db)
    payload = _legacy_settings()
    if shape == "legacy_flat":
        payload = {"event": "app.settings", "merchant": STORE, "data": _settings()}
    _call(env, "salla", _encode(payload))
    (ev,) = _rows(env)

    ev = _dispatch(env.db, ev)

    assert ev.status == "processed", ev.last_error
    settings = env.db.query(TenantSettings).filter_by(tenant_id=tenant_id).one()
    assert settings.whatsapp_settings["auto_reply_enabled"] is False
    assert settings.whatsapp_settings["owner_whatsapp_number"] == "966500000003"
    assert settings.ai_settings["reply_tone"] == "formal"
    assert settings.ai_settings["abandoned_cart_enabled"] is True
    assert settings.ai_settings["allowed_discount_levels"] == "20"
    assert settings.ai_settings["autopilot_enabled"] is False
    _assert_no_sentinel(env)


def test_dispatcher_failure_text_cannot_carry_secret(env):
    _seed_tenant_with_integration(env.db)
    payload = _legacy_settings()
    payload["data"]["settings"].update(
        discount_percentage=150,
        whatsapp_number={"nested": SECRET},  # invalid shape for a processed field
    )
    _call(env, "salla", _encode(payload))
    (ev,) = _rows(env)
    assert ev.parsed_payload["data"]["settings"]["whatsapp_number"] == [redaction.REDACTED]

    ev = _dispatch(env.db, ev)

    assert ev.status == "failed"  # same outcome as the unsanitised payload
    assert ev.last_error == "ValidationError"
    _assert_no_sentinel(env)


def test_processed_field_with_invalid_shape_stays_invalid_after_redaction():
    from pydantic import ValidationError
    from routers.salla_app_settings import SallaQuickSetupIn

    for field_name in sorted(redaction.PROCESSED_SETTINGS_KEYS):
        with pytest.raises(ValidationError):
            SallaQuickSetupIn(**{field_name: {"nested": SECRET}})
        with pytest.raises(ValidationError):
            SallaQuickSetupIn(**{field_name: [redaction.REDACTED]})


# ── Invalid / missing signature ──────────────────────────────────────────────


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize("signature", ["wrong", "missing"])
def test_rejected_signature_persists_only_a_sanitized_audit_row(env, endpoint, signature):
    body = _encode(_settings_updated())
    sig = _sign(body, WRONG_KEY) if signature == "wrong" else None

    with pytest.raises(HTTPException) as exc_info:
        _call(env, endpoint, body, signature=sig)

    assert exc_info.value.status_code == 401
    (ev,) = _rows(env)
    assert ev.signature_valid is False
    assert ev.status == "failed"
    assert ev.parsed_payload is None  # never dispatchable
    assert ev.last_error.startswith("signature_invalid: SIG_REJECT")
    assert json.loads(ev.raw_body)["data"]["settings"]["handoffSecret"] == redaction.REDACTED
    assert ev.raw_headers[POLICY_HEADER] == "salla-redaction-v1:sanitized"
    _assert_no_sentinel(env, str(exc_info.value.detail))


def test_monitor_mode_accepted_invalid_signature_is_still_sanitized(env):
    env.enforce["value"] = False
    body = _encode(_settings_updated())
    result = _call(env, "salla", body, signature=_sign(body, WRONG_KEY))
    assert result["status"] == "received"
    (ev,) = _rows(env)
    assert ev.signature_valid is True  # unchanged monitor-mode semantics
    assert ev.parsed_payload["data"]["settings"]["handoffSecret"] == redaction.REDACTED
    _assert_no_sentinel(env)


# ── Malformed input ──────────────────────────────────────────────────────────

_SECRET_B = SECRET.encode()
MALFORMED = {
    "truncated": b'{"event":"app.settings.updated","data":{"settings":{"handoffSecret":"' + _SECRET_B,
    "array": _encode([{"event": "app.settings.updated", "data": {"settings": {"handoffSecret": SECRET}}}]),
    "string": _encode("handoffSecret=" + SECRET),
    "invalid_utf8": b'\xff{"handoffSecret":"' + _SECRET_B + b'"}',
    "trailing_data": _encode({"event": "order.created"}) + b' {"handoffSecret":"' + _SECRET_B + b'"}',
    "too_deep": b'{"a":' * 5000 + b'"' + _SECRET_B + b'"' + b"}" * 5000,
    "empty": b"",
}


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize("signed", [True, False])
@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_bodies_are_withheld_without_preview(env, endpoint, signed, case):
    body = MALFORMED[case]
    if signed:
        result = _call(env, endpoint, body)
        assert result["status"] == "received" and result["event"] == "unknown"
    else:
        with pytest.raises(HTTPException) as exc_info:
            _call(env, endpoint, body, signature=_sign(body, WRONG_KEY))
        assert exc_info.value.status_code == 401

    (ev,) = _rows(env)
    assert ev.raw_body is None
    assert ev.parsed_payload is None
    assert ev.status == "failed"
    assert ev.raw_headers[POLICY_HEADER] == "salla-redaction-v1:withheld"
    if signed:
        # ``too_deep`` parses but cannot be walked: withheld as unsanitizable.
        assert ev.last_error.split(": ")[0] in {
            "invalid_json", "payload_not_object", "payload_unsanitizable",
        }
        invalid_json_logs = [
            r.getMessage() for r in env.caplog.records if '"webhook.invalid_json"' in r.getMessage()
        ]
        assert invalid_json_logs and all("raw_preview" not in m for m in invalid_json_logs)
    _assert_no_sentinel(env)


def test_unsanitizable_body_is_withheld(monkeypatch):
    def explode(self, _payload):
        raise RecursionError("synthetic depth failure")

    monkeypatch.setattr(redaction._Redactor, "settings_event", explode)
    result = redaction.sanitize_salla_webhook_body(_encode(_settings_updated()))
    assert result.body_policy == redaction.BODY_WITHHELD
    assert result.raw_body is None and result.parsed_payload is None
    assert result.original_payload is None
    assert result.error == "payload_unsanitizable: RecursionError"


# ── Retries / duplicates ─────────────────────────────────────────────────────


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_retries_dedupe_to_one_sanitized_row(env, endpoint):
    first = _encode(_settings_updated())
    # Same delivery identity, different formatting and a different secret.
    retry = json.dumps(_settings_updated(handoffSecret=SECRET_2), indent=2).encode()

    results = [_call(env, endpoint, body) for body in (first, first, retry)]

    assert len({r["webhook_event_id"] for r in results}) == 1
    (ev,) = _rows(env)
    assert ev.parsed_payload["data"]["settings"]["handoffSecret"] == redaction.REDACTED
    assert any(
        '"duplicate": true' in r.getMessage() for r in env.caplog.records
    ), "duplicate path must be exercised"
    _assert_no_sentinel(env, json.dumps(results))


def test_identityless_legacy_retries_each_persist_sanitized(env):
    body = _encode(_legacy_settings())
    _call(env, "salla", body)
    _call(env, "salla", body)
    rows = _rows(env)
    assert len(rows) == 2 and all(ev.external_event_id is None for ev in rows)
    _assert_no_sentinel(env)


DUPLICATE_KEY_BODIES = {
    "shadowed_secret": (
        '{"event":"app.settings.updated","merchant":"%s","data":{"id":557,'
        '"settings":{"handoffSecret":"%s","handoffSecret":""}}}' % (STORE, SECRET)
    ),
    "spoofed_event_name": (
        '{"event":"app.settings.updated","event":"order.created","merchant":"%s",'
        '"data":{"id":558,"settings":{"anything":"%s"}}}' % (STORE, UNKNOWN)
    ),
    "shadowed_plain_field": (
        '{"event":"order.created","merchant":"%s","data":{"id":559,'
        '"note":"%s","note":"ok"}}' % (STORE, UNKNOWN)
    ),
}


@pytest.mark.parametrize("case", sorted(DUPLICATE_KEY_BODIES))
def test_repeated_json_keys_cannot_smuggle_a_shadowed_value(env, case):
    _call(env, "salla", DUPLICATE_KEY_BODIES[case].encode("utf-8"))
    (ev,) = _rows(env)
    assert ev.raw_headers[POLICY_HEADER] == "salla-redaction-v1:sanitized"
    _assert_no_sentinel(env)


# ── Original bytes for signature / replay / identity ─────────────────────────


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_signature_replay_and_identity_see_original_bytes(env, monkeypatch, endpoint):
    seen = {"verify": [], "replay": [], "identity": []}
    real_verify = webhooks.verify_salla_signature
    real_identity = webhooks.build_salla_webhook_external_event_id

    def verify_spy(*, raw_body, **kwargs):
        seen["verify"].append(raw_body)
        return real_verify(raw_body=raw_body, **kwargs)

    def replay_spy(_provider, raw_body, **_kwargs):
        seen["replay"].append(raw_body)
        return False

    def identity_spy(**kwargs):
        seen["identity"].append(json.dumps(kwargs["parsed_payload"]))
        return real_identity(**kwargs)

    monkeypatch.setattr(webhooks, "verify_salla_signature", verify_spy)
    monkeypatch.setattr(webhooks, "evaluate_replay", replay_spy)
    monkeypatch.setattr(webhooks, "build_salla_webhook_external_event_id", identity_spy)
    body = _encode(_settings_updated())

    _call(env, endpoint, body)

    assert seen["verify"] == [body] and SECRET.encode() in body
    assert seen["replay"] == [body]
    assert len(seen["identity"]) == 1 and SECRET in seen["identity"][0]
    assert env.audits[0][0].status.value == "valid"
    (ev,) = _rows(env)
    assert ev.signature_valid is True
    assert ev.raw_body.encode("utf-8") != body
    _assert_no_sentinel(env)


# ── Exceptions / logging ─────────────────────────────────────────────────────


@pytest.mark.parametrize("signed", [True, False])
def test_persistence_failure_exception_text_cannot_carry_secret(env, monkeypatch, signed):
    """SQLAlchemy errors embed bound parameters; only sanitised values may be bound."""
    bound = []

    def failing_commit():
        # ``models`` and ``database.models`` are distinct module objects
        # mapping the same tables, so match the pending row by table.
        pending = [
            obj for obj in env.db.new
            if getattr(obj, "__tablename__", None) == "webhook_events"
        ]
        params = {
            "raw_body": pending[0].raw_body,
            "parsed_payload": pending[0].parsed_payload,
            "raw_headers": pending[0].raw_headers,
        }
        bound.append(params)
        raise DBAPIError("INSERT INTO webhook_events (...) VALUES (...)", params, Exception("synthetic"))

    monkeypatch.setattr(env.db, "commit", failing_commit)
    body = _encode(_settings_updated())

    with pytest.raises(HTTPException) as exc_info:
        _call(env, "salla", body, signature=None if not signed else "auto")

    assert exc_info.value.status_code == (500 if signed else 401)
    assert bound and redaction.REDACTED in json.dumps(bound)
    log_text = _log_text(env.caplog)
    assert "[parameters:" in log_text  # the failure text really was logged
    _assert_no_sentinel(env, str(exc_info.value.detail), log_text)


def test_spoofed_body_policy_header_is_overwritten(env):
    _call(env, "salla", _encode(_settings_updated()),
          extra_headers={"X-Nahla-Body-Policy": "salla-redaction-v1:original"})
    (ev,) = _rows(env)
    policy_values = [v for k, v in ev.raw_headers.items() if k.lower() == POLICY_HEADER]
    assert policy_values == ["salla-redaction-v1:sanitized"]


# ── Unrelated events and guards unchanged ────────────────────────────────────

UNCHANGED_EVENTS = {
    "order": {
        "event": "order.created", "merchant": int(STORE),
        "data": {"id": 7001, "status": {"slug": "under_review"},
                 "items": [{"name": "حذاء رياضي أبيض", "quantity": 1}],
                 "customer": {"first_name": "نورة", "last_name": "عبدالله", "city": "الرياض"}},
    },
    "abandoned_cart": {
        "event": "abandoned.cart", "merchant": int(STORE),
        "data": {"id": 8001, "total": {"amount": 120, "currency": "SAR"},
                 "items": [{"name": "قميص قطني أزرق", "qty": 2}]},
    },
    "authorize_tokens_kept_for_dispatch": {
        "event": "app.store.authorize", "merchant": int(STORE),
        "data": {"access_token": OAUTH_ACCESS,
                 "refresh_token": OAUTH_REFRESH, "expires": 1767225600},
    },
    "communication_observation": {
        "event": "communication.whatsapp.send", "merchant": int(STORE),
        "created_at": "2026-11-01T10:00:00Z",
        "data": {"type": "order.status.updated", "notifiable": ["+966500000004"],
                 "content": "عطر ورد 100ml", "entity": {"type": "order", "id": 7002}},
    },
}


@pytest.mark.parametrize("case", sorted(UNCHANGED_EVENTS))
def test_events_without_settings_or_secret_keys_persist_byte_identical(env, case):
    body = json.dumps(UNCHANGED_EVENTS[case], ensure_ascii=False, indent=2).encode("utf-8")
    _call(env, "salla", body)
    (ev,) = _rows(env)
    assert ev.raw_body == body.decode("utf-8")
    assert ev.parsed_payload == json.loads(body)
    assert ev.raw_headers[POLICY_HEADER] == "salla-redaction-v1:original"
    if case == "communication_observation":
        assert ev.event_type == "communication.whatsapp.observed"


def test_communication_send_guard_still_rejects_unsigned_in_monitor_mode(env):
    env.enforce["value"] = False
    body = _encode(UNCHANGED_EVENTS["communication_observation"])
    with pytest.raises(HTTPException) as exc_info:
        _call(env, "salla", body, signature=_sign(body, WRONG_KEY))
    assert exc_info.value.status_code == 401
    (ev,) = _rows(env)
    assert ev.signature_valid is False and ev.parsed_payload is None


# ── Policy unit checks ───────────────────────────────────────────────────────


def test_processed_settings_keys_match_quick_setup_model():
    from routers.salla_app_settings import SallaQuickSetupIn

    assert redaction.PROCESSED_SETTINGS_KEYS == frozenset(SallaQuickSetupIn.model_fields)
    assert not any(redaction.is_secret_key(k) for k in redaction.PROCESSED_SETTINGS_KEYS)


@pytest.mark.parametrize("key", [
    "handoffSecret",
    "handoff_secret",
    "HANDOFF-SECRET",
    "client_secret",
    "adminPassword",
    "passwd",
    "apiKey",
    "api_key",
    "privateKey",
    "signing_key",
])
def test_secret_key_names(key):
    assert redaction.is_secret_key(key)


@pytest.mark.parametrize("key", [
    # OAuth tokens are applied by the dispatcher for app.store.authorize.
    "access_token", "refresh_token", "nahla_enabled", "handoffMode", "status", "settings",
])
def test_non_secret_key_names(key):
    assert not redaction.is_secret_key(key)


def test_result_repr_never_contains_body():
    result = redaction.sanitize_salla_webhook_body(_encode(_settings_updated()))
    text = repr(result)
    assert "sanitized" in text
    for sentinel in SENTINELS:
        assert sentinel not in text


# ── Sibling receiver: /salla/app-settings/webhook (does not persist) ─────────


@pytest.fixture
def sibling(monkeypatch, caplog):
    from routers import salla_app_settings

    db, engine = _make_db()
    tenant = Tenant(name="متجر تجريبي عام", is_active=True)
    db.add(tenant)
    db.commit()
    monkeypatch.setattr(salla_app_settings, "_SALLA_WEBHOOK_SECRET", SIGNING_KEY)
    monkeypatch.setattr(webhooks, "_resolve_tenant_from_store", lambda _db, _sid: tenant.id)
    caplog.set_level(logging.DEBUG)

    def call(payload, *, signature="auto"):
        body = _encode(payload)
        sig = _sign(body) if signature == "auto" else signature
        request = _request(body, "/salla/app-settings/webhook", signature=sig)
        return asyncio.run(salla_app_settings.salla_app_settings_webhook(request, db))

    yield SimpleNamespace(db=db, call=call, caplog=caplog, audits=[], tenant_id=tenant.id)
    db.close()
    engine.dispose()


def test_sibling_settings_route_applies_without_leaking(sibling):
    result = sibling.call(_legacy_settings())
    assert result["processed"] is True
    assert "handoffSecret" not in result["changed"]
    updated = sibling.call(_settings_updated())
    assert updated["processed"] is False
    with pytest.raises(HTTPException) as bad_sig:
        sibling.call(_legacy_settings(), signature=_sign(b"other", WRONG_KEY))
    invalid = _legacy_settings()
    invalid["data"]["settings"]["discount_percentage"] = 150
    with pytest.raises(HTTPException) as bad_value:
        sibling.call(invalid)
    assert (bad_sig.value.status_code, bad_value.value.status_code) == (401, 422)
    _assert_no_sentinel(
        sibling, json.dumps([result, updated]),
        str(bad_sig.value.detail), str(bad_value.value.detail),
    )
