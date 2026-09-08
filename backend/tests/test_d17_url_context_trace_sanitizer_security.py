"""Security tests for url_context_trace sanitizer and merge boundary.

INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE
MODEL_CHANGED=NO
PROMPT_CHANGED=NO
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
_REPO = os.path.abspath(os.path.join(_BACKEND, ".."))
for _p in (_REPO, _BACKEND, os.path.join(_REPO, "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.conversation_engine import StateManager  # noqa: E402
from models import MessageEvent  # noqa: E402
from modules.ai.brain.observability.url_context_trace import (  # noqa: E402
    SCHEMA_VERSION,
    UrlContextTraceRecorder,
    merge_url_context_trace_into_extra_metadata,
    sanitize_url_context_trace,
)
from routers.whatsapp_webhook import _otp_merge_save_metadata  # noqa: E402
from tests.commerce_scenario_fixtures import (  # noqa: E402
    DEFAULT_PHONE_E164,
    make_scenario_db,
    seed_conversation,
    seed_customer,
    seed_tenant,
)

from test_d17_url_context_attach_observability import (  # noqa: E402
    PUBLIC_PAGE_URL,
    _run_brain_process,
    _trace_bytes,
)
from test_d17_url_context_enrichment import GENERIC_MERCHANT  # noqa: E402

PHONE = DEFAULT_PHONE_E164.replace("+", "")
SECRET_MARKERS = (
    "SECRET_URL",
    "SECRET_TOKEN",
    "CUSTOMER_PHONE",
    "0500000000",
    "RuntimeError",
    "Traceback",
    "https://example.test",
)


def _assert_logs_clean(records: list[str]) -> None:
    blob = "\n".join(records)
    for marker in SECRET_MARKERS:
        assert marker not in blob


@pytest.fixture()
def db():
    session, _engine = make_scenario_db()
    yield session
    session.close()


@pytest.fixture()
def tenant_ctx(db):
    tenant = seed_tenant(db, name=GENERIC_MERCHANT)
    customer = seed_customer(db, tenant.id, name="أحمد سالم")
    conv = seed_conversation(db, tenant.id, customer_id=customer.id)
    return SimpleNamespace(
        tenant_id=tenant.id,
        customer_id=customer.id,
        conversation_id=conv.id,
        phone=PHONE,
        conv=conv,
    )


def test_security_a_unknown_keys_stripped() -> None:
    raw = {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": True,
        "detector_ran": True,
        "url_count": 1,
        "attach_completed": True,
        "raw_url": "https://example.test/?token=SECRET",
        "customer_phone": "0500000000",
        "arbitrary": "value",
        "fetch_attempted": True,
        "external_fetch_count": 1,
        "fetch_completed": True,
        "fetch_result": "ok",
        "facts_projected": True,
    }
    out = sanitize_url_context_trace(raw)
    assert out is not None
    assert "raw_url" not in out
    assert "customer_phone" not in out
    assert "arbitrary" not in out
    assert out["url_count"] == 1


def test_security_b_nested_injection_rejected() -> None:
    raw = {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": True,
        "detector_ran": True,
        "url_count": 1,
        "attach_completed": True,
        "fetch_result": {"nested": "bad"},
        "cache_status": ["hit"],
        "nested_payload": {"a": 1},
    }
    out = sanitize_url_context_trace(raw)
    assert out is not None
    assert "fetch_result" not in out
    assert "cache_status" not in out
    assert "nested_payload" not in out


def test_security_c_enum_injection_sanitized() -> None:
    raw = {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": True,
        "detector_ran": True,
        "url_count": 1,
        "attach_completed": True,
        "failure_stage": "evil_stage",
        "exception_class": "RuntimeError",
        "fetch_result": "totally_wrong",
        "cache_status": "bogus",
        "fetch_error_class": "not_allowed",
        "fetch_attempted": True,
        "external_fetch_count": 1,
    }
    out = sanitize_url_context_trace(raw)
    assert out is not None
    assert "failure_stage" not in out
    assert "exception_class" not in out
    assert "fetch_result" not in out
    assert "cache_status" not in out
    assert "fetch_error_class" not in out


def test_security_d_invalid_schema_rejected() -> None:
    assert sanitize_url_context_trace({"attach_entered": True}) is None
    assert sanitize_url_context_trace({"schema_version": "3", "attach_entered": True, "detector_ran": True, "url_count": 0, "attach_completed": True}) is None
    assert sanitize_url_context_trace("not-a-mapping") is None
    assert sanitize_url_context_trace(
        {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": 1,
            "detector_ran": True,
            "url_count": 0,
            "attach_completed": True,
        }
    ) is None


def test_security_e_type_confusion_rejected() -> None:
    raw = {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": 1,
        "detector_ran": True,
        "url_count": 0,
        "attach_completed": True,
    }
    assert sanitize_url_context_trace(raw) is None

    raw2 = {
        "schema_version": SCHEMA_VERSION,
        "attach_entered": True,
        "detector_ran": True,
        "url_count": 1,
        "attach_completed": True,
        "fetch_attempted": True,
        "external_fetch_count": -5,
        "received_bytes": -1,
        "duration_ms": 9_999_999,
        "http_status": 9999,
    }
    out = sanitize_url_context_trace(raw2)
    assert out is not None
    assert "external_fetch_count" not in out
    assert "received_bytes" not in out
    assert "duration_ms" not in out
    assert "http_status" not in out


def test_security_f_valid_recorder_trace_sparse() -> None:
    out, _ = asyncio.run(_run_brain_process(message=PUBLIC_PAGE_URL))
    trace = out["url_context_trace"]
    assert sanitize_url_context_trace(trace) == trace
    assert _trace_bytes(trace) <= 900


def test_security_g_key_collision_policy() -> None:
    existing = {
        "turn_timing": {"total_ms": 12},
        "url_context_trace": {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": True,
            "detector_ran": True,
            "url_count": 0,
            "attach_completed": True,
        },
    }
    malicious = {
        "url_context_trace": {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": True,
            "detector_ran": True,
            "url_count": 1,
            "attach_completed": True,
            "raw_url": "https://evil.test/SECRET",
        }
    }
    merged = _otp_merge_save_metadata(None, dict(existing), brain_result=malicious)
    assert merged["turn_timing"] == existing["turn_timing"]
    stored = merged["url_context_trace"]
    assert "raw_url" not in stored
    assert stored["url_count"] == 1

    invalid = {
        "url_context_trace": {
            "schema_version": "9",
            "attach_entered": True,
            "detector_ran": True,
            "url_count": 1,
            "attach_completed": True,
            "raw_url": "https://evil.test/SECRET",
        }
    }
    merged_invalid = _otp_merge_save_metadata(None, dict(existing), brain_result=invalid)
    assert merged_invalid["turn_timing"] == existing["turn_timing"]
    assert merged_invalid["url_context_trace"]["failure_stage"] == "trace_merge"
    assert merged_invalid["url_context_trace"]["exception_class"] == "trace_merge_error"
    assert "raw_url" not in merged_invalid["url_context_trace"]


def test_security_h_db_persistence_malicious_brain_result(db, tenant_ctx) -> None:
    malicious = {
        "url_context_trace": {
            "schema_version": SCHEMA_VERSION,
            "attach_entered": True,
            "detector_ran": True,
            "url_count": 1,
            "attach_completed": True,
            "raw_url": "https://evil.test/?token=SECRET_TOKEN",
            "customer_phone": "0500000000",
            "nested": {"pi": 3},
        },
        "reply": "should-not-merge",
        "customer_phone": "0500000000",
    }
    extra = _otp_merge_save_metadata(None, {"quality_observability": {"ok": True}}, brain_result=malicious)
    assert "reply" not in extra
    assert extra.get("quality_observability") == {"ok": True}
    StateManager.save_message(
        db,
        tenant_ctx.phone,
        "reply",
        "outbound",
        conversation_id=int(tenant_ctx.conversation_id),
        tenant_id=int(tenant_ctx.tenant_id),
        extra_metadata=extra,
    )
    db.commit()
    row = (
        db.query(MessageEvent)
        .filter(MessageEvent.tenant_id == int(tenant_ctx.tenant_id))
        .filter(MessageEvent.direction == "outbound")
        .order_by(MessageEvent.id.desc())
        .first()
    )
    assert row is not None
    blob = json.dumps(dict(row.extra_metadata or {}), ensure_ascii=False)
    assert "SECRET_TOKEN" not in blob
    assert "0500000000" not in blob
    assert "raw_url" not in blob
    assert "nested" not in blob
    trace = dict(row.extra_metadata or {}).get("url_context_trace") or {}
    assert trace.get("url_count") == 1


def test_security_i_attach_logging_privacy() -> None:
    records: list[str] = []

    class _Cap(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    loggers = [
        logging.getLogger("nahla.url_context_trace"),
        logging.getLogger("nahla.brain.pipeline"),
    ]
    handlers = [_Cap() for _ in loggers]
    for lg, handler in zip(loggers, handlers):
        lg.addHandler(handler)
        lg.setLevel(logging.WARNING)

    secret_message = (
        "SECRET_URL https://shop.test/?token=SECRET_TOKEN CUSTOMER_PHONE 0500000000"
    )
    try:
        asyncio.run(
            _run_brain_process(
                message=PUBLIC_PAGE_URL,
                extra_patches=[
                    patch(
                        "services.url_context.begin_url_context_turn",
                        side_effect=RuntimeError(secret_message),
                    )
                ],
            )
        )
    finally:
        for lg, handler in zip(loggers, handlers):
            lg.removeHandler(handler)

    _assert_logs_clean(records)
    assert any("event=url_context_attach_failure" in line for line in records)
    assert any("error_class=attach_error" in line for line in records)


def test_security_merge_direct_helper_unknown_keys() -> None:
    target: dict[str, Any] = {"persona": {"mode": "llm"}}
    merge_url_context_trace_into_extra_metadata(
        target,
        {
            "url_context_trace": {
                "schema_version": SCHEMA_VERSION,
                "attach_entered": True,
                "detector_ran": True,
                "url_count": 0,
                "attach_completed": True,
                "evil": "SECRET_TOKEN",
            },
            "evil_top": "SECRET",
        },
    )
    assert target["persona"] == {"mode": "llm"}
    assert "evil" not in target["url_context_trace"]
    assert "evil_top" not in target
