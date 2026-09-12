"""Opt-in model evaluation: no mocked model and no WhatsApp or DB writes.

Run with NAHLA_RUN_CATALOG_MODEL_EVAL=1 and the normal provider credentials.
Skipped tests are NOT evidence that the model understood these examples.
"""
import asyncio
import os

import pytest

from modules.ai.brain.postprocess.catalog_semantic_claims import classify_catalog_claims, contradiction_reason
from test_catalog_semantic_contracts import facts, context
from modules.ai.brain.commerce.catalog_request_interpreter import interpret_catalog_request

pytestmark = [pytest.mark.layer3_llm, pytest.mark.skipif(
    os.environ.get("NAHLA_RUN_CATALOG_MODEL_EVAL") != "1" or not os.environ.get("OPENAI_API_KEY"),
    reason="Explicit live model evaluation and provider credentials required",
)]


@pytest.mark.parametrize("text,conflict", [
    ("متوفر حذاء رياضي أبيض تقدر تشوف صورته.", False),
    ("غير متوفر حذاء رياضي أبيض للأسف.", True),
    ("متوفر حذاء رياضي أبيض وعطر ورد سعره 249.50 ريال.", True),
    ("حذاء رياضي أبيض سعره 249.50 ريال.", False),
])
def test_live_claim_understanding(text, conflict):
    result = asyncio.run(classify_catalog_claims(text, facts()))
    assert result.status == "ok"
    assert bool(contradiction_reason(text, facts(), result)) is conflict


@pytest.mark.parametrize("text,capability", [
    ("أرسل رابطه", "link"), ("وريني صورته", "image"),
    ("فيه أسود؟", "variant_question"), ("تمام يعطيك العافية", "conversation"),
])
def test_live_catalog_request_understanding(text, capability):
    result = asyncio.run(interpret_catalog_request(context(text)))
    assert result is not None and result.status == "ok"
    assert result.capability == capability
