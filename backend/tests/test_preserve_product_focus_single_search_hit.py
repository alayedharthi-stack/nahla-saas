"""Product Focus Lifecycle — preserve focus on single exact search hit."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
for p in (ROOT, BACKEND):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from modules.ai.brain.commerce.commerce_focus_owner import (  # noqa: E402
    product_focus_identity,
    should_preserve_focus_after_product_list_display,
)


def _shoe_white() -> dict:
    return {
        "id": "shoe-white-1",
        "external_id": "SKU-SHOE-WHITE",
        "title": "حذاء رياضي أبيض",
        "price": 199,
    }


def _shoe_black() -> dict:
    return {
        "id": "shoe-black-1",
        "external_id": "SKU-SHOE-BLACK",
        "title": "حذاء رياضي أسود",
        "price": 209,
    }


def _shirt_blue() -> dict:
    return {
        "id": "shirt-blue-1",
        "external_id": "SKU-SHIRT-BLUE",
        "title": "قميص قطني أزرق",
        "price": 89,
    }


def _perfume_rose() -> dict:
    return {
        "id": "perf-rose-1",
        "external_id": "SKU-PERF-ROSE",
        "title": "عطر ورد 100ml",
        "price": 149,
    }


class TestShouldPreserveFocusAfterProductListDisplay:
    def test_single_exact_product_hit_preserves(self) -> None:
        focus = _shoe_white()
        candidates = [_shoe_white()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is True

    def test_multiple_product_hits_do_not_preserve(self) -> None:
        focus = _shoe_white()
        candidates = [_shoe_white(), _shoe_black()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is False

    def test_category_list_multi_candidates_do_not_preserve(self) -> None:
        focus = _shirt_blue()
        candidates = [_shirt_blue(), _perfume_rose()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is False

    def test_variant_focus_two_candidates_no_match(self) -> None:
        focus = {"title": "حذاء رياضي أبيض", "variant_label": "أبيض", "price": 199}
        candidates = [_shoe_white(), _shoe_black()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is False

    def test_variant_focus_single_matching_candidate_preserves(self) -> None:
        focus = {
            "external_id": "SKU-SHOE-WHITE",
            "title": "حذاء رياضي أبيض",
            "variant_label": "أبيض",
            "price": 199,
        }
        candidates = [_shoe_white()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is True

    def test_product_switch_focus_a_single_candidate_b(self) -> None:
        focus = _shoe_white()
        candidates = [_shirt_blue()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is False

    def test_stale_focus_after_new_search_different_single_hit(self) -> None:
        focus = _perfume_rose()
        candidates = [_shirt_blue()]
        assert should_preserve_focus_after_product_list_display(focus, candidates) is False

    def test_empty_candidates_do_not_preserve(self) -> None:
        focus = _shoe_white()
        assert should_preserve_focus_after_product_list_display(focus, []) is False

    def test_no_focus_do_not_preserve(self) -> None:
        candidates = [_shoe_white()]
        assert should_preserve_focus_after_product_list_display(None, candidates) is False
        assert should_preserve_focus_after_product_list_display({}, candidates) is False

    def test_multi_tenant_isolation_pure_helper(self) -> None:
        tenant_a_focus = {
            "external_id": "TENANT-A-SHOE",
            "title": "حذاء رياضي أبيض",
            "tenant_id": "tenant-a",
        }
        tenant_b_focus = {
            "external_id": "TENANT-B-SHIRT",
            "title": "قميص قطني أزرق",
            "tenant_id": "tenant-b",
        }
        tenant_a_candidates = [
            {
                "external_id": "TENANT-A-SHOE",
                "title": "حذاء رياضي أبيض",
                "tenant_id": "tenant-a",
            }
        ]
        tenant_b_candidates = [
            {
                "external_id": "TENANT-B-SHIRT",
                "title": "قميص قطني أزرق",
                "tenant_id": "tenant-b",
            }
        ]

        focus_a = copy.deepcopy(tenant_a_focus)
        focus_b = copy.deepcopy(tenant_b_focus)
        cands_a = copy.deepcopy(tenant_a_candidates)
        cands_b = copy.deepcopy(tenant_b_candidates)

        assert should_preserve_focus_after_product_list_display(focus_a, cands_a) is True
        assert should_preserve_focus_after_product_list_display(focus_b, cands_b) is True
        assert should_preserve_focus_after_product_list_display(focus_a, cands_b) is False
        assert should_preserve_focus_after_product_list_display(focus_b, cands_a) is False

        # Inputs must not be mutated by the pure helper.
        assert focus_a == tenant_a_focus
        assert focus_b == tenant_b_focus
        assert cands_a == tenant_a_candidates
        assert cands_b == tenant_b_candidates


class TestPipelineConditionSimulation:
    """Integration-style: post-search state shapes from live RCA."""

    def test_live_rca_single_hit_same_external_id_preserves(self) -> None:
        # set_product_focus(1921568272) then product list with candidates=1
        focus = {
            "external_id": "1921568272",
            "title": "حذاء رياضي أبيض",
            "price": 199,
            "can_checkout": True,
        }
        last_search_candidates = [
            {
                "external_id": "1921568272",
                "title": "حذاء رياضي أبيض",
                "price": 199,
                "can_checkout": True,
            }
        ]

        assert product_focus_identity(focus) == "1921568272"
        assert product_focus_identity(last_search_candidates[0]) == "1921568272"
        assert should_preserve_focus_after_product_list_display(
            focus,
            last_search_candidates,
        ) is True

    def test_live_rca_single_hit_would_clear_without_fix(self) -> None:
        """Without preserve, focus=None → downstream entity_not_resolved."""
        focus = {
            "external_id": "1921568272",
            "title": "حذاء رياضي أبيض",
        }
        candidates = [
            {
                "external_id": "1921568272",
                "title": "حذاء رياضي أبيض",
            }
        ]

        preserve = should_preserve_focus_after_product_list_display(focus, candidates)
        assert preserve is True

        # Simulate pipeline branch: preserve → focus stays; clear → focus lost.
        simulated_focus = focus if preserve else None
        assert simulated_focus is not None
        assert product_focus_identity(simulated_focus) == "1921568272"
