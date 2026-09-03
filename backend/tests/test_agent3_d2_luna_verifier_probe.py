"""Real Luna probe for AGENT3-D2 semantic claim classification.

Skipped unless OPENAI_API_KEY is present. This is not a phrase detector.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.postprocess.staff_escalation_semantic_verifier import (  # noqa: E402
    classify_staff_escalation_claims,
    verifier_requested_model,
)

LIVE_FALSE_PROMISE = (
    "تمام، وصلت رسالتك. فريق المتجر بيتابع معك هنا في أقرب وقت."
)
RECEIPT_ONLY = "تمام، وصلت رسالتك."

pytestmark = pytest.mark.skipif(
    not str(os.environ.get("OPENAI_API_KEY") or "").strip(),
    reason="real Luna probe requires OPENAI_API_KEY",
)


def _run(coro):
    return asyncio.run(coro)


def test_live_regression_sentence_classifies_future_followup() -> None:
    assert verifier_requested_model() == "gpt-5.6-luna"
    claims = _run(classify_staff_escalation_claims(LIVE_FALSE_PROMISE, tenant_id=33))
    assert claims.valid_parse is True, claims.as_dict()
    assert claims.claims_future_followup is True
    assert claims.model.startswith("gpt-5.6-luna") or claims.model == "gpt-5.6-luna"


def test_receipt_only_does_not_require_future_followup() -> None:
    claims = _run(classify_staff_escalation_claims(RECEIPT_ONLY, tenant_id=33))
    assert claims.valid_parse is True, claims.as_dict()
    assert claims.claims_future_followup is False
