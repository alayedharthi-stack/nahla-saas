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
