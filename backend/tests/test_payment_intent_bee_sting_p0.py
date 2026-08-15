"""
P0 Payment Intent Resolution — bee-sting / service inquiry must not trigger payment.
"""
from __future__ import annotations

import os
import sys

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.ai_libraries import is_payment_query  # noqa: E402
from modules.ai.brain.commerce.conversational_priority import (  # noqa: E402
    detect_payment_intent_strength,
    has_payment_outbound_consent,
)
from modules.ai.brain.intent.rules import match as match_intent  # noqa: E402
from modules.ai.brain.types import INTENT_ASK_PAYMENT_INFO  # noqa: E402


@pytest.mark.parametrize(
    "message",
    [
        "لو سمحت ابغى لسع نحل فالرياض",
        "بس ماعرف لسع فيه احد يقدر يلسعني بالرياض",
        "أبغى علاج لسع النحل",
        "وين ألقى أحد يسوي لسع نحل",
        "وش فائدة لسع النحل",
        "أبغى بروبوليس وش فائدته",
    ],
)
def test_service_inquiry_must_not_trigger_payment(message: str):
    assert not is_payment_query(message)
    assert detect_payment_intent_strength(message).strength < 0.65
    assert not has_payment_outbound_consent(
        message,
        tenant_id=33,
        route="test_p0_bee_sting",
    )
    intent = match_intent(message)
    assert intent is None or intent.name != INTENT_ASK_PAYMENT_INFO


@pytest.mark.parametrize(
    "message",
    [
        "أرسل الحساب",
        "أرسل الباركود",
        "كيف أدفع؟",
        "ابغي احول",
        "أرسل بيانات التحويل",
        "تمام بأخذ واحد أرسل الحساب",
        "ابي الباركود",
        "كيف أحول لكم؟",
    ],
)
def test_explicit_payment_requests_still_detected(message: str):
    verdict = detect_payment_intent_strength(message)
    assert verdict.strength >= 0.65
    assert has_payment_outbound_consent(
        message,
        tenant_id=33,
        route="test_p0_explicit_payment",
    )


@pytest.mark.parametrize(
    "message",
    [
        "حساب الراجحي",
        "بنك الرياض",
    ],
)
def test_bank_name_substring_is_not_outbound_consent(message: str):
    """Retrieval may still collide; consent/execution must not follow."""
    assert is_payment_query(message)
    assert detect_payment_intent_strength(message).strength < 0.65
    assert not has_payment_outbound_consent(
        message,
        tenant_id=33,
        route="test_p0_bank_name_substring",
    )


def test_riyadh_city_alone_is_not_payment_bank():
    assert not is_payment_query("أنا في الرياض وابغى لسع نحل")
    assert not is_payment_query("التوصيل للرياض")


@pytest.mark.parametrize(
    "message",
    [
        "فيه فرع بنك الرياض قريب؟",
        "رقم حساب بنك الرياض؟",
        "تحويل بنك الرياض",
        "ابي حساب بنك الرياض",
    ],
)
def test_riyadh_bank_qualified_stays_ask_payment_info(message: str):
    intent = match_intent(message)
    assert intent is not None
    assert intent.name == INTENT_ASK_PAYMENT_INFO
    assert is_payment_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "رقم حساب بنك الرياض؟",
        "ابي حساب بنك الرياض",
    ],
)
def test_riyadh_requestive_payment_still_has_consent(message: str):
    assert has_payment_outbound_consent(
        message,
        tenant_id=33,
        route="test_riyadh_bank_gate",
    )


def test_branch_like_bank_name_is_not_outbound_consent():
    assert not has_payment_outbound_consent(
        "فيه فرع بنك الرياض قريب؟",
        tenant_id=33,
        route="test_riyadh_branch_not_consent",
    )


@pytest.mark.parametrize(
    "message",
    [
        "في الرياض",
        "بالرياض",
        "فالرياض",
        "احد بالرياض",
        "محل بالرياض",
        "لسع نحل بالرياض",
    ],
)
def test_riyadh_city_token_must_not_be_ask_payment_info(message: str):
    intent = match_intent(message)
    assert intent is None or intent.name != INTENT_ASK_PAYMENT_INFO
    assert not is_payment_query(message)
    assert not has_payment_outbound_consent(
        message,
        tenant_id=33,
        route="test_riyadh_city_gate",
    )
