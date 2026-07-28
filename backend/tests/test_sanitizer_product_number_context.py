"""«رقم المنتج» prompts must not be rewritten as a missing-contact-number line.

Production incident 2026-07-28, tenant 1. The customer asked «أبغى فستان» and
received:

    «يا هلا، حالياً لا يوجد رقم تواصل مهيأ لإرساله. المنتج أو اسمه وأكمل طلبك ✨»

reproduced byte-for-byte by passing

    «يا هلا، تفضل رقم المنتج أو اسمه وأكمل طلبك ✨»

through ``maybe_scrub_unkept_asset_promise``: the phone-promise pattern matched
the span «تفضل رقم» and the fallback swallowed it, leaving a dangling tail.

«اكتب رقم» / «اختر رقم» escaped only because those verbs are not promise verbs,
so the exemption keys off the catalog noun bound to «رقم», evaluated against the
matched span so a genuine contact promise is still scrubbed.
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in (_BACKEND, os.path.join(_BACKEND, ".."), os.path.join(_BACKEND, "..", "database")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.outbound_sanitizer import (  # noqa: E402
    ASSET_PHONE,
    contains_promised_asset,
    maybe_scrub_unkept_asset_promise,
)

_PHONE_FALLBACK_MARKER = "لا يوجد رقم تواصل"

# The exact outbound text that produced the live incident.
LIVE_TEXT = "يا هلا، تفضل رقم المنتج أو اسمه وأكمل طلبك ✨"

# Catalog prompts: «رقم» is bound to a product/option noun.
CATALOG_PROMPTS = (
    LIVE_TEXT,
    "يا هلا، هذا رقم المنتج أو اسمه وأكمل طلبك ✨",
    "اكتب رقم المنتج أو اسمه وأكمل طلبك.",
    "اختر رقم الخيار أو اسم المنتج وأكمل معك",
    "حدد رقم المنتج الذي تريده",
    "أرسل رقم المنتج أو اسمه",
    "عطني رقم الخيار",
    "هذا أقرب خيار لطلبك\n\nاختر رقم الخيار أو اسم المنتج وأكمل معك.",
)

# Genuine contact promises: «رقم» is NOT bound to a catalog noun.
CONTACT_PROMISES = (
    "تفضل رقم أبو هشام 0555555555",
    "هذا رقم الجوال 0555555555",
    "تفضل رقم التواصل مع المسؤول",
    "هذا رقم خدمة العملاء",
)


def _scrub(text: str):
    return maybe_scrub_unkept_asset_promise(
        text,
        has_url=False,
        has_media=False,
        has_phone=False,
        has_product_card=False,
    )


@pytest.mark.parametrize("text", CATALOG_PROMPTS)
def test_catalog_number_prompts_are_not_scrubbed(text: str) -> None:
    out, scrubbed, asset = _scrub(text)
    assert scrubbed is False, f"«{text}» must not be scrubbed"
    assert asset is None
    assert out == text
    assert _PHONE_FALLBACK_MARKER not in out


@pytest.mark.parametrize("text", CATALOG_PROMPTS)
def test_catalog_number_prompts_are_not_detected_as_phone_promise(text: str) -> None:
    assert contains_promised_asset(text) != ASSET_PHONE


def test_live_incident_text_round_trips_unchanged() -> None:
    out, scrubbed, asset = _scrub(LIVE_TEXT)
    assert scrubbed is False
    assert asset is None
    assert out == LIVE_TEXT
    # The dangling tail that reached the customer must be impossible now.
    assert "المنتج أو اسمه وأكمل طلبك" in out
    assert out.count("رقم المنتج") == 1


@pytest.mark.parametrize("text", CONTACT_PROMISES)
def test_genuine_contact_promises_are_still_scrubbed(text: str) -> None:
    """The fix must not silence the guard where a real number is promised."""
    assert contains_promised_asset(text) == ASSET_PHONE
    out, scrubbed, asset = _scrub(text)
    assert scrubbed is True
    assert asset == ASSET_PHONE
    assert _PHONE_FALLBACK_MARKER in out


def test_catalog_prompt_and_contact_promise_in_one_text() -> None:
    """A catalog prompt must not grant blanket immunity to the whole reply."""
    text = "اختر رقم المنتج أو اسمه. وتفضل رقم أبو هشام 0555555555"
    out, scrubbed, asset = _scrub(text)
    assert scrubbed is True
    assert asset == ASSET_PHONE
    # The catalog prompt survives; only the contact promise is rewritten.
    assert "اختر رقم المنتج أو اسمه." in out
    assert "أبو هشام" not in out or _PHONE_FALLBACK_MARKER in out
