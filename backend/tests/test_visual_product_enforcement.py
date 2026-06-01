"""
tests/test_visual_product_enforcement.py
────────────────────────────────────────
Targeted tests for the May 2026 #6 visual-product enforcement layer.
The previous P0 / P1+P2+P3 work fixed the brain's bare-confirmation
path; this round closes the OTHER recurring regression where a
customer explicitly asks to SEE a product (image, link, catalog)
and the LLM produces a text-only description without emitting a
[PRODUCT:] marker — final_delivery_mode lands on ``text_only`` and
the customer gets prose where they asked for a card.

Two test surfaces are exercised:

  1. The expanded :data:`_PRODUCT_INBOUND_KEYWORDS` set in
     ``modules.observability.delivery_mode`` MUST now classify the
     five production phrasings as visual-product intents.

  2. The pure pick-candidate helper
     (:func:`pick_best_candidate_title`) must walk the brain-state
     cascade in the right order so the webhook always has a usable
     title to feed to ``resolve_by_query``.

The webhook-level integration (``_enforce_visual_product_intent``)
is exercised in production via the ``[VISUAL_PRODUCT_ENFORCEMENT]``
trace lines and the existing ``[FINAL_DELIVERY]`` / ``[DELIVERY_
GUARD_FAIL]`` log assertions; we keep this file pure / DB-free so
the suite stays in the ~ms range.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the ``backend`` package importable when running pytest from
# the repo root — mirrors the convention used by the other backend
# tests in this directory.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

# Ensure the SQLAlchemy models module import path isn't tripped by
# the visual_enforcement module itself (it has zero DB imports, but
# `modules.observability` does pull `delivery_mode` which is also
# pure — defensive import-time assertion).
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.observability import (  # noqa: E402
    customer_wants_product_or_image,
    has_visual_marker,
    pick_best_candidate_title,
)
from modules.observability.visual_enforcement import (  # noqa: E402
    SOURCE_FOCUS,
    SOURCE_INBOUND_TEXT,
    SOURCE_LAST_RECOMMENDED,
    SOURCE_LAST_SEARCH,
    SOURCE_NONE,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1.  The 5 mandatory production phrasings MUST be classified as visual asks.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inbound", [
    # Original production regression — covered by the OLD keyword
    # pack ("أبغى أشوف") but re-pinned here so a future cleanup
    # of the set can't quietly regress it.
    "أبغى أشوف صورة لعسل السمر",
    # New keyword: "ورني" (already in old set, verifying still hits)
    "ورني السمر",
    # New keyword: "أرسل رابط" — was missing → text_only slipped.
    "أرسل رابط السمر",
    # Old keyword "كتالوج" with possessive prefix "أبي ال…".
    "أبي الكتالوج",
    # New keyword: "عندك صورة" — was missing → text_only slipped.
    "عندك صورة للضهيان؟",
])
def test_mandatory_visual_phrasings_classified(inbound: str) -> None:
    """All five production phrasings must be visual intents.

    Failure means the keyword pack regressed and a customer asking
    to SEE a product would get a text-only reply with no follow-up
    card — the exact production bug this layer fixes."""
    assert customer_wants_product_or_image(
        inbound_text=inbound, brain_action="",
    ) is True, (
        f"expected visual-intent classification for {inbound!r}; "
        "check _PRODUCT_INBOUND_KEYWORDS in delivery_mode.py"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Negative — neutral chitchat must NOT trip the guard.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("inbound", [
    # Bare "صورة" in a non-product context.
    "ارسل لي صورة العقد",          # legal document
    "وش رايك في صورة البروفايل؟",  # profile picture chitchat
    # Bare "رابط" in a non-product context.
    "أرسل رابط الموقع الإلكتروني",  # website link
    # Bare "كتالوج" only inside an unrelated reference (here we
    # intentionally avoid the word to validate the absence path).
    "السلام عليكم كيف الحال؟",
    "متى يفتح المتجر اليوم؟",
])
def test_neutral_messages_not_flagged_as_visual(inbound: str) -> None:
    """The keyword set must remain conservative.

    A noisy classifier would trigger the enforcer on every chitchat
    turn, attaching random catalog cards and creating a worse UX
    than the bug we're fixing. These messages MUST stay below the
    visual-intent threshold."""
    assert customer_wants_product_or_image(
        inbound_text=inbound, brain_action="",
    ) is False, (
        f"unexpectedly flagged {inbound!r} as visual intent — "
        "broaden the negative test set or tighten the keyword pack"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Brain-action signals still win (orthogonal to keywords).
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", [
    "search_products",
    "recommend_addon",
    "propose_draft_order",
])
def test_brain_action_alone_is_enough(action: str) -> None:
    """When the brain decided ``search_products`` etc., the guard
    fires regardless of inbound text — the decision engine already
    ran the full intent pipeline and chose to surface product
    content."""
    assert customer_wants_product_or_image(
        inbound_text="ابدأ", brain_action=action,
    ) is True


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Candidate picker cascade — strongest signal first.
# ─────────────────────────────────────────────────────────────────────────────

def test_candidate_picker_prefers_current_product_focus() -> None:
    """The most specific signal — the brain pinned a product this
    same turn / a few turns ago — wins."""
    bs = {
        "current_product_focus": {"id": 1, "title": "عسل السمر", "price": 280},
        "product_focus_turn": 5,
        "turn": 6,
        "last_search_candidates": [{"id": 2, "title": "عسل سدر"}],
        "last_recommended_products": [{"id": 3, "title": "عسل طلح"}],
    }
    title, source = pick_best_candidate_title(bs, "أرسل الصورة")
    assert title == "عسل السمر"
    assert source == SOURCE_FOCUS


def test_candidate_picker_falls_back_to_last_search() -> None:
    """No active focus → use the top of the last search list."""
    bs = {
        "current_product_focus": None,
        "last_search_candidates": [
            {"id": 2, "title": "عسل سدر جبلي"},
            {"id": 3, "title": "عسل سمر"},
        ],
    }
    title, source = pick_best_candidate_title(bs, "ورنيه")
    assert title == "عسل سدر جبلي"
    assert source == SOURCE_LAST_SEARCH


def test_candidate_picker_falls_back_to_last_recommended() -> None:
    """Older soft signal still beats raw text."""
    bs = {
        "last_recommended_products": [
            {"id": 9, "title": "عسل القولون"},
        ],
    }
    title, source = pick_best_candidate_title(bs, "أبغى أشوف")
    assert title == "عسل القولون"
    assert source == SOURCE_LAST_RECOMMENDED


def test_candidate_picker_uses_inbound_text_as_last_resort() -> None:
    """No state cache → hand the raw inbound text to the resolver;
    its fuzzy match against the synced catalog turns
    ``"أبغى أشوف صورة لعسل السمر"`` into ``"عسل السمر"``."""
    title, source = pick_best_candidate_title(
        {}, "أبغى أشوف صورة لعسل السمر",
    )
    assert title == "عسل السمر"
    assert source == SOURCE_INBOUND_TEXT


def test_candidate_picker_empty_everywhere_yields_none() -> None:
    """Defensive — no state AND no inbound text returns the sentinel."""
    title, source = pick_best_candidate_title({}, "")
    assert title == ""
    assert source == SOURCE_NONE


def test_candidate_picker_skips_blank_titles_in_lists() -> None:
    """Non-empty entries with blank titles must not poison the cascade."""
    bs = {
        "last_search_candidates": [
            {"id": 1, "title": "   "},       # whitespace only
            {"id": 2},                        # missing title key
            {"id": 3, "title": "عسل المراعي"},
        ],
    }
    title, source = pick_best_candidate_title(bs, "ايش عندك؟")
    assert title == "عسل المراعي"
    assert source == SOURCE_LAST_SEARCH


def test_candidate_picker_tolerates_malformed_state() -> None:
    """``brain_state`` may be a partial / non-dict blob in degenerate
    cases (corrupt JSON, mid-migration). The picker must not raise."""
    # current_product_focus is a non-dict
    bs = {"current_product_focus": "not a dict"}
    title, source = pick_best_candidate_title(bs, "أبي صورة")
    # Deictic without trusted focus → no candidate (clarify path).
    assert title == ""
    assert source == SOURCE_NONE


# ─────────────────────────────────────────────────────────────────────────────
# 5.  has_visual_marker — the short-circuit so we never double-attach.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply, expected", [
    ("جربي عسل السمر [PRODUCT:عسل السمر]", True),
    ("الباركود مرفق [MEDIA_KEY:bank_barcode]", True),
    ("لدينا عدة أنواع: السمر والطلح والسدر", False),
    ("", False),
    (None, False),
    # Case-insensitive — the brain occasionally emits lower-case.
    ("see [product:عسل السمر]", True),
    ("see [media_key:cert]", True),
])
def test_has_visual_marker(reply, expected) -> None:
    """The substring check must be case-insensitive and tolerate
    ``None`` so the webhook's short-circuit never accidentally
    enforces over a successful marker emission."""
    assert has_visual_marker(reply) is expected
