"""Unit tests for resolve_browse_presentation_candidates pure function."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.product_presentation_selection import (  # noqa: E402
    resolve_browse_presentation_candidates,
)

JACKET = {
    "id": 28,
    "external_id": "1921568272",
    "title": "جاكيت",
    "price": 199,
    "can_checkout": True,
}

SHOE = {
    "id": 55,
    "external_id": "shoe-1",
    "title": "حذاء رياضي أبيض",
    "price": 249,
    "can_checkout": True,
}

TITLE_ONLY = {"title": "جاكيت", "can_checkout": True}


class TestResolveBrowsePresentationCandidates:
    def test_prefers_identified_display_rows(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[JACKET],
        )
        assert len(rows) == 1
        assert rows[0]["id"] == 28

    def test_two_identified_rows_stay_multi(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[JACKET, SHOE],
        )
        assert len(rows) == 2
        assert {r["id"] for r in rows} == {28, 55}

    def test_empty_display_recovers_from_executor_via_catalog_id(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[],
            executor_products=[JACKET],
            catalog_product_ids=[28],
        )
        assert len(rows) == 1
        assert rows[0]["id"] == 28

    def test_empty_display_recovers_from_resolved_product(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[],
            resolved_product=JACKET,
            catalog_product_ids=[28],
        )
        assert len(rows) == 1
        assert rows[0]["external_id"] == "1921568272"

    def test_no_recovery_without_catalog_identity(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[],
            executor_products=[],
            catalog_product_ids=[],
        )
        assert rows == []

    def test_title_only_singleton_preserved_for_missing_identity_reason(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[TITLE_ONLY],
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "جاكيت"
        assert "id" not in rows[0]

    def test_identity_hygiene_drops_title_only_when_identified_present(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[TITLE_ONLY, JACKET],
        )
        assert len(rows) == 1
        assert rows[0]["id"] == 28

    def test_does_not_invent_card_from_id_hint_alone(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[],
            executor_products=[],
            catalog_product_ids=[99],
        )
        assert rows == []

    def test_compose_rows_used_when_display_empty(self) -> None:
        rows = resolve_browse_presentation_candidates(
            display_candidates=[],
            compose_products=[JACKET],
        )
        assert len(rows) == 1
        assert rows[0]["id"] == 28
