"""Defensive tests for the campaign wizard test-send pipeline.

These all run without a database — we only exercise the pure helpers
(``_body_text``, ``_dynamic_url_buttons``, ``_coerce_recipient``,
``_coerce_merchant_vars``, ``build_test_payload``) so the test suite
stays fast and we can pin the contract independently of the rest of
the platform.

The bug they prevent: the wizard's "إرسال اختبار" step was failing
with ``'str' object has no attribute 'get'`` whenever a template's
``components`` JSON contained a string entry, or when the variables
map contained a non-string value. Both shapes now pass through the
defensive coercion layer below.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from services.campaign_wizard.test_send import (  # noqa: E402
    MOCK_DEFAULTS,
    _body_text,
    _coerce_merchant_vars,
    _coerce_recipient,
    _dynamic_url_buttons,
    build_test_payload,
)


def _tpl(components, *, name="welcome_test", language="ar", status="APPROVED"):
    """Lightweight stand-in for ``WhatsAppTemplate``."""
    return SimpleNamespace(
        id=1, tenant_id=1, name=name, language=language,
        status=status, components=components,
    )


# ─────────────────────────── _body_text ──────────────────────────────
def test_body_text_skips_string_component_entries():
    """Old Salla payloads occasionally include raw strings in
    ``components``. We must skip them, not crash on .get()."""
    tpl = _tpl([
        "this_is_garbage_string",
        {"type": "BODY", "text": "أهلاً {{1}} 👋"},
    ])
    assert _body_text(tpl) == "أهلاً {{1}} 👋"


def test_body_text_returns_empty_when_no_body():
    tpl = _tpl([{"type": "HEADER", "text": "x"}])
    assert _body_text(tpl) == ""


def test_body_text_handles_none_components():
    tpl = _tpl(None)
    assert _body_text(tpl) == ""


# ────────────────────── _dynamic_url_buttons ─────────────────────────
def test_dynamic_buttons_skips_string_component_entries():
    tpl = _tpl([
        "garbage",
        {"type": "BUTTONS", "buttons": [
            "another_garbage_string",
            {"type": "URL", "url": "https://store.example/{{1}}"},
            {"type": "PHONE_NUMBER", "phone_number": "+966555906901"},
        ]},
    ])
    out = _dynamic_url_buttons(tpl)
    assert len(out) == 1
    assert out[0]["index"] == 1  # garbage at index 0 is skipped
    assert out[0]["url_template"] == "https://store.example/{{1}}"


def test_dynamic_buttons_skips_static_url_buttons():
    tpl = _tpl([{"type": "BUTTONS", "buttons": [
        {"type": "URL", "url": "https://store.example/static-page"},  # no {{1}}
    ]}])
    assert _dynamic_url_buttons(tpl) == []


def test_dynamic_buttons_handles_buttons_field_as_string():
    """Defensive: ``buttons`` should be a list, but we tolerate junk."""
    tpl = _tpl([{"type": "BUTTONS", "buttons": "not_a_list"}])
    assert _dynamic_url_buttons(tpl) == []


# ─────────────────────── _coerce_recipient ───────────────────────────
def test_coerce_recipient_accepts_plain_string():
    phone, name = _coerce_recipient("0542980511")
    assert phone == "0542980511"
    assert name == "اختبار"


def test_coerce_recipient_strips_whitespace():
    phone, name = _coerce_recipient("  0542980511  ")
    assert phone == "0542980511"
    assert name == "اختبار"


def test_coerce_recipient_accepts_dict_phone_field():
    phone, name = _coerce_recipient({"phone": "0542980511", "name": "Saud"})
    assert phone == "0542980511"
    assert name == "Saud"


def test_coerce_recipient_accepts_dict_aliases():
    # Various older shapes the legacy clients have emitted.
    for key in ("mobile", "number", "to_phone", "to"):
        phone, _name = _coerce_recipient({key: "0542980511"})
        assert phone == "0542980511"


def test_coerce_recipient_rejects_empty_string():
    with pytest.raises(ValueError):
        _coerce_recipient("")
    with pytest.raises(ValueError):
        _coerce_recipient("   ")


def test_coerce_recipient_rejects_dict_without_phone():
    with pytest.raises(ValueError):
        _coerce_recipient({"name": "Saud"})


def test_coerce_recipient_rejects_unknown_shape():
    with pytest.raises(ValueError):
        _coerce_recipient([1, 2, 3])
    with pytest.raises(ValueError):
        _coerce_recipient(None)


def test_coerce_recipient_int_is_stringified():
    phone, name = _coerce_recipient(966555906901)
    assert phone == "966555906901"
    assert name == "اختبار"


# ──────────────────── _coerce_merchant_vars ──────────────────────────
def test_coerce_vars_passes_dict_through():
    out = _coerce_merchant_vars({"{{1}}": "Saud", "{{2}}": "https://x"})
    assert out == {"{{1}}": "Saud", "{{2}}": "https://x"}


def test_coerce_vars_drops_non_scalar_values():
    out = _coerce_merchant_vars({"{{1}}": "Saud", "{{2}}": ["a", "b"]})
    assert out == {"{{1}}": "Saud"}


def test_coerce_vars_handles_none():
    assert _coerce_merchant_vars(None) == {}


def test_coerce_vars_accepts_int_values():
    out = _coerce_merchant_vars({"{{1}}": 42})
    assert out == {"{{1}}": "42"}


def test_coerce_vars_accepts_list_of_pairs():
    out = _coerce_merchant_vars([
        {"key": "{{1}}", "value": "Saud"},
        {"name": "{{2}}", "value": "https://x"},
        {"placeholder": "{{3}}", "text": "extra"},
    ])
    assert out == {"{{1}}": "Saud", "{{2}}": "https://x", "{{3}}": "extra"}


def test_coerce_vars_accepts_json_string():
    out = _coerce_merchant_vars('{"{{1}}": "Saud"}')
    assert out == {"{{1}}": "Saud"}


def test_coerce_vars_handles_invalid_json_gracefully():
    assert _coerce_merchant_vars("not_json") == {}


# ─────────────────────── build_test_payload ──────────────────────────
def test_build_payload_does_not_raise_on_string_component():
    """Smoke test for the original bug: a string entry in components
    must not blow up the pipeline."""
    tpl = _tpl([
        "garbage_string",
        {"type": "BODY", "text": "أهلاً {{1}} 👋"},
    ])
    payload = build_test_payload(
        tpl,
        to_phone_e164="966542980511",
        merchant_vars={"{{1}}": "Saud"},
    )
    assert payload["to"] == "966542980511"
    assert payload["template"]["name"] == "welcome_test"
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    assert body["parameters"][0]["text"] == "Saud"


def test_build_payload_falls_back_to_mock_defaults():
    tpl = _tpl([{"type": "BODY", "text": "Hi {{1}} {{2}}"}])
    payload = build_test_payload(tpl, to_phone_e164="966555906901", merchant_vars={})
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    assert body["parameters"][0]["text"] == MOCK_DEFAULTS["{{1}}"]
    assert body["parameters"][1]["text"] == MOCK_DEFAULTS["{{2}}"]


def test_build_payload_accepts_bare_index_keys_for_vars():
    """Frontend has historically used both ``{{1}}`` and ``"1"`` keys."""
    tpl = _tpl([{"type": "BODY", "text": "Hi {{1}}"}])
    payload = build_test_payload(
        tpl, to_phone_e164="966555906901", merchant_vars={"1": "Saud"},
    )
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    assert body["parameters"][0]["text"] == "Saud"


def test_build_payload_drops_list_value_in_vars():
    tpl = _tpl([{"type": "BODY", "text": "Hi {{1}}"}])
    payload = build_test_payload(
        tpl, to_phone_e164="966555906901",
        merchant_vars={"{{1}}": ["should", "be", "dropped"]},  # type: ignore[dict-item]
    )
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    # Falls through to MOCK_DEFAULTS instead of joining the list.
    assert body["parameters"][0]["text"] == MOCK_DEFAULTS["{{1}}"]


# ────────── send_test_message — meta error response shapes ───────────
#
# Regression tests for the production bug where the wizard step 6
# rendered the raw Python error
#     'str' object has no attribute 'get'
# whenever Meta (or, more often, a CDN/proxy in front of Meta) returned
# ``{"error": "Bad Gateway"}`` instead of the documented structured
# ``{"error": {"message": ..., "code": ...}}`` shape. The fix collapses
# both shapes into the same merchant-friendly result.

import asyncio  # noqa: E402

from unittest.mock import patch  # noqa: E402

from services.campaign_wizard.test_send import send_test_message  # noqa: E402


class _FakeQuery:
    def __init__(self, result):
        self._result = result
    def filter(self, *_a, **_k): return self
    def order_by(self, *_a, **_k): return self
    def first(self): return self._result


class _FakeDB:
    def __init__(self, *, template, wa_conn, tenant=None):
        self._template = template
        self._wa_conn = wa_conn
        self._tenant = tenant

    def query(self, model):
        name = getattr(model, "__name__", str(model))
        if "Template" in name:
            return _FakeQuery(self._template)
        if "Connection" in name:
            return _FakeQuery(self._wa_conn)
        if name == "Tenant":
            return _FakeQuery(self._tenant)
        return _FakeQuery(None)


def _approved_template():
    """A minimal APPROVED template suitable for end-to-end test-send."""
    return SimpleNamespace(
        id=11, tenant_id=7, name="welcome_test",
        language="ar", status="APPROVED",
        components=[{"type": "BODY", "text": "Hi {{1}}"}],
    )


def _wa_conn():
    return SimpleNamespace(phone_number_id="pn_123", status="connected")


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_send_test_message_handles_meta_error_as_string(monkeypatch):
    """The bug: ``provider_send_message`` returned ``{"error": "string"}``
    and ``meta_err.get("message")`` blew up because ``meta_err`` was a
    string, not the documented dict. Result must now be a structured
    failure with the bare string surfaced as the error_message."""
    db = _FakeDB(template=_approved_template(), wa_conn=_wa_conn())

    async def fake_send(*_a, **_k):
        return {"error": "Bad Gateway"}, object()

    with patch(
        "services.whatsapp_platform.service.provider_send_message",
        new=fake_send,
    ):
        with patch(
            "services.customer_intelligence.normalize_phone",
            new=lambda v: v,
        ):
            result = _run(send_test_message(
                db, tenant_id=7, template_db_id=11,
                to_phone="+966500000000", merchant_vars={"{{1}}": "Saud"},
            ))

    assert result["sent"] is False
    assert result["error_code"] == "meta:meta_error"
    assert "Bad Gateway" in result["error_message"]


def test_send_test_message_handles_meta_error_as_dict(monkeypatch):
    """The documented Meta shape — ensure we still extract code/message
    cleanly after the defensive isinstance guard."""
    db = _FakeDB(template=_approved_template(), wa_conn=_wa_conn())

    async def fake_send(*_a, **_k):
        return (
            {"error": {"code": 132001, "message": "Template translation missing"}},
            object(),
        )

    with patch(
        "services.whatsapp_platform.service.provider_send_message",
        new=fake_send,
    ):
        with patch(
            "services.customer_intelligence.normalize_phone",
            new=lambda v: v,
        ):
            result = _run(send_test_message(
                db, tenant_id=7, template_db_id=11,
                to_phone="+966500000000", merchant_vars={"{{1}}": "Saud"},
            ))

    assert result["sent"] is False
    assert result["error_code"] == "meta:132001"
    assert result["error_message"] == "Template translation missing"


def test_send_test_message_top_level_catchall(monkeypatch):
    """Defence-in-depth: any future regression that leaks a raw
    Python error from inside the inner orchestrator must still
    return a structured Arabic message, not a raw exception text."""
    db = _FakeDB(template=_approved_template(), wa_conn=_wa_conn())

    async def boom(*_a, **_k):
        raise AttributeError("'str' object has no attribute 'get'")

    # Patch the inner function to simulate an unrelated regression.
    with patch(
        "services.campaign_wizard.test_send._send_test_message_inner",
        new=boom,
    ):
        result = _run(send_test_message(
            db, tenant_id=7, template_db_id=11,
            to_phone="+966500000000", merchant_vars={"{{1}}": "Saud"},
        ))

    assert result["sent"] is False
    assert result["error_code"] == "unexpected_error"
    # Must be the friendly Arabic message — never the raw Python error.
    assert "'get'" not in result["error_message"]
    assert "تعذّر" in result["error_message"]


def test_send_test_message_handles_first_message_not_dict(monkeypatch):
    """If Meta returns ``{"messages": ["wamid..."]}`` (rare bad proxy
    response), we must not call .get() on the bare string."""
    db = _FakeDB(template=_approved_template(), wa_conn=_wa_conn())

    async def fake_send(*_a, **_k):
        return {"messages": ["wamid_garbage"]}, object()

    with patch(
        "services.whatsapp_platform.service.provider_send_message",
        new=fake_send,
    ):
        with patch(
            "services.customer_intelligence.normalize_phone",
            new=lambda v: v,
        ):
            result = _run(send_test_message(
                db, tenant_id=7, template_db_id=11,
                to_phone="+966500000000", merchant_vars={"{{1}}": "Saud"},
            ))

    # Treated as "no message id" — never raises AttributeError.
    assert result["sent"] is False
    assert result["error_code"] == "no_message_id"


# The campaign wizard must send the IMAGE header that Meta approved, just
# like campaign delivery. A resumable-upload review sample is not send media.
_IMAGE_URL = "https://media.example/template-headers/7/generic-shoe.png"


def _image_template(url=_IMAGE_URL):
    template = _approved_template()
    template.name = "generic_shoe_offer"
    template.components.insert(0, {
        "type": "HEADER", "format": "IMAGE",
        "example": {"header_url": url, "header_handle": ["review-sample-only"]},
    })
    return template


def test_image_test_payload_keeps_header_body_and_dynamic_button():
    template = _image_template()
    template.components.insert(0, "legacy-component")
    template.components.append({"type": "BUTTONS", "buttons": [
        {"type": "URL", "url": "https://store.example/{{1}}"},
    ]})

    payload = build_test_payload(
        template, to_phone_e164="+966500000000",
        merchant_vars={"{{1}}": "Customer", "cart_url": "https://store.example/shoes"},
    )

    components = payload["template"]["components"]
    assert [component["type"] for component in components] == ["header", "body", "button"]
    assert components[0]["parameters"] == [{"type": "image", "image": {"link": _IMAGE_URL}}]
    assert components[1]["parameters"] == [{"type": "text", "text": "Customer"}]
    assert components[2]["parameters"] == [{"type": "text", "text": "shoes"}]
    assert "review-sample-only" not in str(payload)
    assert template.components[1]["example"]["header_handle"] == ["review-sample-only"]


@pytest.mark.parametrize("tenant_id", [7, 71])
def test_image_test_send_passes_saved_image_to_provider(tenant_id):
    from unittest.mock import AsyncMock

    template = _image_template()
    template.tenant_id = tenant_id
    db = _FakeDB(template=template, wa_conn=_wa_conn())
    send = AsyncMock(return_value=({"messages": [{"id": "wamid.test-image"}]}, object()))
    with patch("services.whatsapp_platform.service.provider_send_message", send):
        with patch("services.customer_intelligence.normalize_phone", lambda value: value):
            result = _run(send_test_message(
                db, tenant_id=tenant_id, template_db_id=template.id,
                to_phone="+966500000000", merchant_vars={"{{1}}": "Customer"},
            ))

    assert result["sent"] is True
    assert result["wa_message_id"] == "wamid.test-image"
    send.assert_awaited_once()
    assert send.call_args.kwargs["tenant_id"] == tenant_id
    payload = send.call_args.kwargs["payload"]
    assert payload["template"]["components"][0] == {
        "type": "header", "parameters": [{"type": "image", "image": {"link": _IMAGE_URL}}],
    }


@pytest.mark.parametrize("url", ["", "http://media.example/image.png", "https://user:secret@media.example/image.png"])
def test_image_test_send_rejects_missing_or_invalid_image_before_provider(url):
    from unittest.mock import AsyncMock
    from services.template_image_header import MISSING_IMAGE

    template = _image_template(url)
    db = _FakeDB(template=template, wa_conn=_wa_conn())
    send = AsyncMock(return_value=({"messages": [{"id": "must-not-send"}]}, object()))
    with patch("services.whatsapp_platform.service.provider_send_message", send):
        with patch("services.customer_intelligence.normalize_phone", lambda value: value):
            result = _run(send_test_message(
                db, tenant_id=7, template_db_id=template.id, to_phone="+966500000000",
            ))

    assert result["sent"] is False
    assert result["error_code"] == "invalid_template_image"
    assert result["error_message"] == MISSING_IMAGE
    send.assert_not_called()
