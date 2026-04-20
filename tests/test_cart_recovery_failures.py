"""
tests/test_cart_recovery_failures.py
────────────────────────────────────
Pin the contract of ``services.cart_recovery_failures`` — the structured
failure taxonomy that the abandoned-cart recovery engine writes into
``AutomationExecution.action_taken`` and that the merchant dashboard
renders directly.

Without this taxonomy every failure collapsed into "فشل الإرسال" with
no actionable detail; the regression we're guarding against is silently
sliding back into that state when a future refactor of the engine or
the WhatsApp provider layer changes the error envelope shape.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for p in (REPO_ROOT, BACKEND_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.cart_recovery_failures import (  # noqa: E402
    FAILURE_INVALID_PHONE,
    FAILURE_MISSING_PHONE,
    FAILURE_NO_APPROVED_TEMPLATE,
    FAILURE_NO_WA_CONNECTION,
    FAILURE_PROVIDER_AUTH,
    FAILURE_PROVIDER_EMPTY_RESPONSE,
    FAILURE_PROVIDER_RETURNED_ERROR,
    FAILURE_PROVIDER_TIMEOUT,
    FAILURE_RATE_LIMITED,
    FAILURE_RECEIVER_INCAPABLE,
    FAILURE_RE_ENGAGEMENT_WINDOW,
    FAILURE_TEMPLATE_PARAM_MISMATCH,
    FAILURE_UNKNOWN,
    classify_internal_error,
    classify_meta_response,
    classify_send_exception,
    label_for_code,
)


# ── classify_internal_error ──────────────────────────────────────────────────
def test_classify_internal_no_customer_phone_maps_to_missing_phone_label():
    code, label = classify_internal_error("no_customer_phone")
    assert code == FAILURE_MISSING_PHONE
    assert "رقم" in label  # Arabic label rendered


def test_classify_internal_invalid_phone_maps_to_invalid_phone():
    code, label = classify_internal_error("invalid_phone_number")
    assert code == FAILURE_INVALID_PHONE
    assert "غير صالح" in label


def test_classify_internal_no_template_maps_to_template_not_approved():
    code, _ = classify_internal_error("no_approved_template")
    assert code == FAILURE_NO_APPROVED_TEMPLATE


def test_classify_internal_no_wa_connection_maps_to_no_connection():
    code, _ = classify_internal_error("no_whatsapp_connection")
    assert code == FAILURE_NO_WA_CONNECTION


def test_classify_internal_unknown_string_falls_back_to_send_failed_unknown():
    """Free-form strings collapse to ``FAILURE_UNKNOWN`` so the dashboard
    never has to render an English token leaked from a Python
    traceback."""
    code, label = classify_internal_error("'messages'")  # KeyError stringified
    assert code == FAILURE_UNKNOWN
    assert "غير محدد" in label


def test_classify_internal_empty_or_none_returns_unknown():
    assert classify_internal_error(None)[0] == FAILURE_UNKNOWN
    assert classify_internal_error("")[0] == FAILURE_UNKNOWN


# ── classify_meta_response: success cases ───────────────────────────────────
def test_classify_meta_response_with_message_id_returns_none():
    """Healthy Meta response — engine must not flag this as failure."""
    healthy = {"messaging_product": "whatsapp", "messages": [{"id": "wamid.ABC"}]}
    assert classify_meta_response(healthy) is None


# ── classify_meta_response: error envelopes ─────────────────────────────────
def test_classify_meta_response_template_not_exists_via_code():
    response = {"error": {"code": 132001, "message": "Template name does not exist"}}
    result = classify_meta_response(response)
    assert result is not None
    code, label, raw = result
    assert code == FAILURE_NO_APPROVED_TEMPLATE
    assert raw["code"] == 132001
    assert "Meta" in label or "معتمد" in label


def test_classify_meta_response_re_engagement_window_via_code():
    """The single most common Salla → Nahla failure: customer hasn't
    messaged in 24h, free-form is rejected."""
    response = {"error": {"code": 131047, "message": "Re-engagement message"}}
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_RE_ENGAGEMENT_WINDOW


def test_classify_meta_response_recipient_incapable():
    response = {"error": {"code": 131026, "message": "Receiver cannot be reached"}}
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_RECEIVER_INCAPABLE


def test_classify_meta_response_token_expired():
    response = {"error": {"code": 190, "message": "Access token has expired"}}
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_PROVIDER_AUTH


def test_classify_meta_response_param_mismatch_via_code():
    response = {
        "error": {
            "code": 132000,
            "message": "Number of parameters does not match expected number",
        }
    }
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_TEMPLATE_PARAM_MISMATCH


def test_classify_meta_response_rate_limit_via_code():
    response = {"error": {"code": 131048, "message": "Spam rate limit hit"}}
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_RATE_LIMITED


def test_classify_meta_response_unknown_code_falls_back_to_provider_error():
    """An unknown Meta code must still classify as a failure (not None)
    so the engine doesn't silently record success."""
    response = {"error": {"code": 999999, "message": "Some weird error"}}
    code, _, raw = classify_meta_response(response)
    assert code == FAILURE_PROVIDER_RETURNED_ERROR
    assert raw["code"] == 999999


def test_classify_meta_response_heuristic_template_paused():
    """Code missing but message says template paused → still classifies."""
    response = {"error": {"message": "Template is paused"}}
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_NO_APPROVED_TEMPLATE


def test_classify_meta_response_heuristic_param_format():
    response = {"error": {"message": "Parameter format mismatch"}}
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_TEMPLATE_PARAM_MISMATCH


# ── classify_meta_response: empty / partial responses ───────────────────────
def test_classify_meta_response_empty_dict_returns_empty_response_failure():
    """The smoking-gun bug: response is ``{}`` because Meta returned
    an error AND the engine swallowed it. Pre-fix this looked like
    success (wa_message_id=None). Now it's a structured failure."""
    code, _, _ = classify_meta_response({})
    assert code == FAILURE_PROVIDER_EMPTY_RESPONSE


def test_classify_meta_response_messages_array_without_id_is_failure():
    response = {"messages": [{}]}  # has the array but no id
    code, _, _ = classify_meta_response(response)
    assert code == FAILURE_PROVIDER_EMPTY_RESPONSE


def test_classify_meta_response_none_returns_empty_response_failure():
    code, _, _ = classify_meta_response(None)
    assert code == FAILURE_PROVIDER_EMPTY_RESPONSE


# ── classify_send_exception ─────────────────────────────────────────────────
def test_classify_send_exception_timeout_is_provider_timeout():
    class FakeTimeout(Exception):
        pass
    FakeTimeout.__name__ = "TimeoutException"
    code, _, raw = classify_send_exception(FakeTimeout("read timed out"))
    assert code == FAILURE_PROVIDER_TIMEOUT
    assert "TimeoutException" in raw


def test_classify_send_exception_generic_collapses_to_unknown():
    code, label, raw = classify_send_exception(KeyError("messages"))
    assert code == FAILURE_UNKNOWN
    assert "KeyError" in raw
    assert "محدد" in label  # Arabic generic label


# ── label_for_code ──────────────────────────────────────────────────────────
def test_label_for_code_known_returns_arabic():
    label = label_for_code(FAILURE_NO_APPROVED_TEMPLATE)
    assert "Meta" in label or "معتمد" in label


def test_label_for_code_unknown_returns_generic_arabic():
    label = label_for_code("totally_made_up_code_xyz")
    assert label == label_for_code(FAILURE_UNKNOWN)


def test_label_for_code_none_returns_generic():
    assert label_for_code(None) == label_for_code(FAILURE_UNKNOWN)
