"""
tests/test_template_variants.py
─────────────────────────────────
Unit tests for Phase 3 — Template Dedup + Variations.

Covers:
  1. Each of the 6 variant-aware templates returns a DIFFERENT string for
     each of the three variants (0, 1, 2) — guarantees wording actually rotates.
  2. DefaultComposer._variant_idx() returns len(history) % 3.
  3. DefaultComposer._last_outbound() finds the latest "out"/"outbound" turn.
  4. DefaultComposer._is_duplicate() detects when first-70-chars match.
  5. _is_duplicate() returns False when the first-70-chars differ.
  6. _is_duplicate() returns False when there is no outbound history.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

REPO_ROOT   = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
for _p in [str(REPO_ROOT), str(BACKEND_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from modules.ai.brain.compose import templates as T
from modules.ai.brain.compose.responder import DefaultComposer
from modules.ai.brain.types import (
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
    SalesContextSnapshot,
)


# ── Minimal BrainContext stub ─────────────────────────────────────────────────

def _ctx(history: List[Dict[str, Any]] | None = None) -> BrainContext:
    """Minimal BrainContext with controllable history."""
    ctx = MagicMock(spec=BrainContext)
    ctx.history = history or []
    ctx.tenant_id = "test-tenant"
    ctx.facts = MagicMock(spec=CommerceFacts)
    ctx.facts.store_name = "متجر التجربة"
    ctx.state = MagicMock(spec=MerchantConversationState)
    ctx.sales_context = MagicMock(spec=SalesContextSnapshot)
    ctx.intent = MagicMock(spec=Intent)
    return ctx


def _out(body: str) -> Dict[str, Any]:
    return {"direction": "out", "body": body}


def _in(body: str) -> Dict[str, Any]:
    return {"direction": "in", "body": body}


# ─────────────────────────────────────────────────────────────────────────────
# 1. Template variant uniqueness — all 6 templates
# ─────────────────────────────────────────────────────────────────────────────

class TestGreetingVariants:
    def test_all_three_variants_differ(self):
        v0 = T.greeting(store_name="نحلة", variant=0)
        v1 = T.greeting(store_name="نحلة", variant=1)
        v2 = T.greeting(store_name="نحلة", variant=2)
        assert v0 != v1
        assert v1 != v2
        assert v0 != v2

    def test_variant_wraps_modulo(self):
        assert T.greeting(variant=3) == T.greeting(variant=0)
        assert T.greeting(variant=4) == T.greeting(variant=1)

    def test_store_name_injected(self):
        result = T.greeting(store_name="متجر الاختبار", variant=0)
        assert "متجر الاختبار" in result


class TestProductResultsVariants:
    _LINES = "1. منتج أ — 50 ريال\n2. منتج ب — 80 ريال"

    def test_all_three_variants_differ(self):
        v0 = T.product_results(self._LINES, query="عباية", count=2, variant=0)
        v1 = T.product_results(self._LINES, query="عباية", count=2, variant=1)
        v2 = T.product_results(self._LINES, query="عباية", count=2, variant=2)
        assert v0 != v1
        assert v1 != v2
        assert v0 != v2

    def test_product_lines_present_in_all_variants(self):
        for v in range(3):
            result = T.product_results(self._LINES, variant=v)
            assert "منتج أ" in result

    def test_variant_wraps_modulo(self):
        assert T.product_results(self._LINES, variant=3) == T.product_results(self._LINES, variant=0)


class TestNarrowChoicesVariants:
    _PRODUCTS = [
        {"title": "عباية سوداء", "price": 150},
        {"title": "عباية بيضاء", "price": 120},
    ]

    def test_all_three_variants_differ(self):
        v0 = T.narrow_choices(self._PRODUCTS, variant=0)
        v1 = T.narrow_choices(self._PRODUCTS, variant=1)
        v2 = T.narrow_choices(self._PRODUCTS, variant=2)
        assert v0 != v1
        assert v1 != v2
        assert v0 != v2

    def test_products_listed_in_all_variants(self):
        for v in range(3):
            result = T.narrow_choices(self._PRODUCTS, variant=v)
            assert "عباية سوداء" in result

    def test_empty_products_falls_through(self):
        result = T.narrow_choices([], variant=0)
        assert isinstance(result, str)
        assert len(result) > 0


class TestNoProductsVariants:
    def test_all_three_variants_differ(self):
        v0 = T.no_products(variant=0)
        v1 = T.no_products(variant=1)
        v2 = T.no_products(variant=2)
        assert v0 != v1
        assert v1 != v2
        assert v0 != v2

    def test_variant_wraps_modulo(self):
        assert T.no_products(variant=3) == T.no_products(variant=0)


class TestHandoffVariants:
    def test_all_three_variants_differ(self):
        v0 = T.handoff(variant=0)
        v1 = T.handoff(variant=1)
        v2 = T.handoff(variant=2)
        assert v0 != v1
        assert v1 != v2
        assert v0 != v2

    def test_variant_wraps_modulo(self):
        assert T.handoff(variant=3) == T.handoff(variant=0)


class TestGenericFallbackVariants:
    def test_all_three_variants_differ(self):
        v0 = T.generic_fallback(variant=0)
        v1 = T.generic_fallback(variant=1)
        v2 = T.generic_fallback(variant=2)
        assert v0 != v1
        assert v1 != v2
        assert v0 != v2

    def test_variant_wraps_modulo(self):
        assert T.generic_fallback(variant=3) == T.generic_fallback(variant=0)


# ─────────────────────────────────────────────────────────────────────────────
# 2. DefaultComposer._variant_idx
# ─────────────────────────────────────────────────────────────────────────────

class TestVariantIdx:
    def test_empty_history_gives_zero(self):
        ctx = _ctx(history=[])
        assert DefaultComposer._variant_idx(ctx) == 0

    def test_one_turn_gives_one(self):
        ctx = _ctx(history=[_in("مرحبا")])
        assert DefaultComposer._variant_idx(ctx) == 1

    def test_two_turns_gives_two(self):
        ctx = _ctx(history=[_in("مرحبا"), _out("أهلاً")])
        assert DefaultComposer._variant_idx(ctx) == 2

    def test_three_turns_wraps_to_zero(self):
        ctx = _ctx(history=[_in("a"), _out("b"), _in("c")])
        assert DefaultComposer._variant_idx(ctx) == 0

    def test_rotates_with_turn_count(self):
        for length in range(9):
            ctx = _ctx(history=[_in("x")] * length)
            assert DefaultComposer._variant_idx(ctx) == length % 3

    def test_none_history_treated_as_empty(self):
        ctx = _ctx(history=None)
        assert DefaultComposer._variant_idx(ctx) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. DefaultComposer._last_outbound
# ─────────────────────────────────────────────────────────────────────────────

class TestLastOutbound:
    def test_empty_history_returns_empty(self):
        ctx = _ctx(history=[])
        assert DefaultComposer._last_outbound(ctx) == ""

    def test_returns_last_out_turn(self):
        ctx = _ctx(history=[_out("الرد الأول"), _in("سؤال"), _out("الرد الثاني")])
        assert DefaultComposer._last_outbound(ctx) == "الرد الثاني"

    def test_ignores_inbound_turns(self):
        ctx = _ctx(history=[_in("رسالة"), _in("رسالة أخرى")])
        assert DefaultComposer._last_outbound(ctx) == ""

    def test_outbound_direction_keyword(self):
        ctx = _ctx(history=[{"direction": "outbound", "body": "رد صحيح"}])
        assert DefaultComposer._last_outbound(ctx) == "رد صحيح"

    def test_picks_latest_when_multiple(self):
        history = [_out(f"رد رقم {i}") for i in range(5)]
        ctx = _ctx(history=history)
        assert DefaultComposer._last_outbound(ctx) == "رد رقم 4"


# ─────────────────────────────────────────────────────────────────────────────
# 4 & 5. DefaultComposer._is_duplicate
# ─────────────────────────────────────────────────────────────────────────────

class TestIsDuplicate:
    def test_detects_exact_duplicate(self):
        text = T.no_products(variant=0)
        ctx = _ctx(history=[_out(text)])
        assert DefaultComposer._is_duplicate(text, ctx) is True

    def test_detects_duplicate_by_first_70_chars(self):
        long_text = "أ" * 100
        last      = "أ" * 100 + " تختلف هنا"
        ctx = _ctx(history=[_out(last)])
        assert DefaultComposer._is_duplicate(long_text, ctx) is True

    def test_different_text_not_duplicate(self):
        v0 = T.no_products(variant=0)
        v1 = T.no_products(variant=1)
        ctx = _ctx(history=[_out(v0)])
        assert DefaultComposer._is_duplicate(v1, ctx) is False

    def test_no_outbound_history_not_duplicate(self):
        ctx = _ctx(history=[_in("رسالة العميل")])
        assert DefaultComposer._is_duplicate("أي نص", ctx) is False

    def test_empty_history_not_duplicate(self):
        ctx = _ctx(history=[])
        assert DefaultComposer._is_duplicate(T.handoff(variant=0), ctx) is False

    def test_short_text_under_70_chars(self):
        short = "مرحبا"
        ctx = _ctx(history=[_out(short)])
        assert DefaultComposer._is_duplicate(short, ctx) is True

    def test_whitespace_stripped_before_compare(self):
        text = "  نص مع مسافات  "
        ctx  = _ctx(history=[_out("  نص مع مسافات  ")])
        assert DefaultComposer._is_duplicate(text, ctx) is True

    def test_greeting_variants_not_cross_duplicate(self):
        v0 = T.greeting(variant=0)
        v1 = T.greeting(variant=1)
        ctx = _ctx(history=[_out(v0)])
        assert DefaultComposer._is_duplicate(v1, ctx) is False

    def test_handoff_variants_not_cross_duplicate(self):
        v0 = T.handoff(variant=0)
        v1 = T.handoff(variant=1)
        ctx = _ctx(history=[_out(v0)])
        assert DefaultComposer._is_duplicate(v1, ctx) is False
