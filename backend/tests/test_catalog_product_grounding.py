"""Regression tests: catalog-grounded product option replies (P0)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
for p in (str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")):
    if p not in sys.path:
        sys.path.insert(0, p)

from modules.ai.brain.commerce.catalog_product_grounding import (  # noqa: E402
    build_catalog_grounded_list_reply,
    build_uncertain_catalog_reply,
    collect_verified_catalog_titles,
    collect_verified_catalog_titles_from_ctx,
    extract_seasonal_product_subject,
    is_seasonal_availability_ask,
    seasonal_subject_in_catalog,
)
from modules.ai.brain.commerce.product_ordering_prompt import (  # noqa: E402
    build_product_ordering_prompt,
)
from modules.ai.brain.decision.actions import ACTION_LLM_REPLY, ACTION_SEARCH_PRODUCTS  # noqa: E402
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.postprocess.catalog_product_grounding_guard import (  # noqa: E402
    apply_catalog_product_grounding_guard,
)
from modules.ai.brain.postprocess.product_claim_grounding_evidence import (  # noqa: E402
    ProductClaimGroundingEvidence,
)
from modules.ai.brain.product_discovery_gate import (  # noqa: E402
    INQUIRY_CLASS_BROAD,
    classify_product_inquiry_route,
)
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)

_HONEY_CATALOG = [
    {"title": "عسل طلح نجد", "external_id": "1"},
    {"title": "عسل سمر الحجاز", "external_id": "2"},
]

_FORBIDDEN_INVENTED = (
    "عسل القطف",
    "عسل الشهد",
    "عسل السدر",
    "عسل الطلح البلدي",
)


def _ctx(
    message: str,
    *,
    candidates: List[Dict[str, Any]] | None = None,
    intent_name: str = "ask_product",
) -> BrainContext:
    return BrainContext(
        tenant_id=33,
        customer_phone="966500000001",
        message=message,
        intent=Intent(name=intent_name, confidence=0.9, raw_message=message),
        state=MerchantConversationState(
            greeted=True,
            stage="discovery",
            last_search_candidates=candidates or [],
        ),
        facts=CommerceFacts(
            has_products=True,
            orderable=True,
            top_products=candidates or _HONEY_CATALOG,
        ),
    )


def _evidence(
    titles: List[str],
    *,
    executor: List[Dict[str, Any]] | None = None,
) -> ProductClaimGroundingEvidence:
    available = tuple(
        {"id": i + 1, "title": t, "can_checkout": True}
        for i, t in enumerate(titles)
    )
    return ProductClaimGroundingEvidence(
        available_products=available,
        executor_product_ids=frozenset(
            p["id"] for p in (executor or []) if isinstance(p.get("id"), int)
        ),
    )


class TestCatalogProductGroundingHelpers:
    def test_collect_verified_titles_merges_sources(self) -> None:
        titles = collect_verified_catalog_titles(
            candidates=[{"title": "عسل طلح نجد"}],
            recommended=[{"title": "عسل سمر الحجاز"}],
            top_products=[{"title": "عسل طلح نجد"}],
        )
        assert titles == ["عسل طلح نجد", "عسل سمر الحجاز"]

    def test_collect_titles_from_ctx_uses_browse_scope_filter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        products = [
            {"id": 10, "title": "Group A Product"},
            {"id": 20, "title": "Group B Product"},
        ]
        ctx = _ctx("وش عندكم من المجموعة؟", candidates=products)
        ctx._db = MagicMock()  # type: ignore[attr-defined]

        def _filter(products_in, **kwargs):
            assert kwargs["source"] == "catalog_grounding"
            assert kwargs["tenant_id"] == 33
            return [p for p in products_in if p.get("id") == 10]

        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard."
            "filter_products_for_browse_turn",
            _filter,
        )

        titles = collect_verified_catalog_titles_from_ctx(ctx)
        assert titles == ["Group A Product"]

    def test_collect_titles_from_ctx_does_not_fallback_when_scope_drops_all(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        products = [
            {"id": 10, "title": "Group A Product"},
            {"id": 20, "title": "Group B Product"},
        ]
        ctx = _ctx("وش عندكم من المجموعة؟", candidates=products)
        ctx._db = MagicMock()  # type: ignore[attr-defined]

        monkeypatch.setattr(
            "modules.ai.brain.commerce.commerce_browse_category_guard."
            "filter_products_for_browse_turn",
            lambda *_a, **_k: [],
        )

        assert collect_verified_catalog_titles_from_ctx(ctx) == []

    def test_build_catalog_grounded_list_only_catalog_names(self) -> None:
        reply = build_catalog_grounded_list_reply(
            ["عسل طلح نجد", "عسل سمر الحجاز"],
            category_hint="العسل",
        )
        assert "الكتالوج" in reply
        assert "المتوفر حاليًا" not in reply
        assert "تحب أعرض لك الأسعار" not in reply
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in reply

    def test_seasonal_subject_not_in_catalog(self) -> None:
        assert is_seasonal_availability_ask("متى يجي العسل الصيفي؟")
        subject = extract_seasonal_product_subject("متى يجي العسل الصيفي؟")
        assert "صيفي" in subject
        assert seasonal_subject_in_catalog(subject, ["عسل طلح نجد", "عسل سمر الحجاز"]) is False


class TestProductOrderingPromptGrounding:
    def test_honey_inquiry_lists_only_catalog_products(self) -> None:
        ctx = _ctx("أبغى الاستفسار عن العسل", candidates=_HONEY_CATALOG)
        prompt = build_product_ordering_prompt(ctx)
        assert "طلح نجد" in prompt
        assert "سمر الحجاز" in prompt
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in prompt

    def test_honey_types_ask_lists_only_catalog_products(self) -> None:
        ctx = _ctx("وش أنواع العسل؟", candidates=_HONEY_CATALOG)
        prompt = build_product_ordering_prompt(ctx)
        assert "طلح نجد" in prompt
        assert "سمر الحجاز" in prompt
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in prompt

    def test_no_catalog_never_invents_honey_types(self) -> None:
        ctx = _ctx("أبي عسل", candidates=[])
        ctx.facts.top_products = []
        prompt = build_product_ordering_prompt(ctx)
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in prompt
        assert "طلح نجد" not in prompt
        assert "سمر الحجاز" not in prompt
        assert "خليني أتأكد" in prompt or "ما ظهرت" in prompt

    def test_empty_catalog_uncertainty_reply(self) -> None:
        reply = build_uncertain_catalog_reply(category_hint="العسل")
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in reply
        assert "ما ظهرت" in reply or "خليني أتأكد" in reply


class TestBroadInquiryRouting:
    def test_broad_inquiry_honey_routes_catalog_search(self) -> None:
        msg = "أبغى الاستفسار عن العسل"
        decision = DefaultDecisionEngine().decide(_ctx(msg))
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("source") == "category_browse"
        assert decision.action != ACTION_LLM_REPLY

    def test_types_overview_routes_catalog_search(self) -> None:
        msg = "وش أنواع العسل؟"
        decision = DefaultDecisionEngine().decide(_ctx(msg))
        assert decision.action == ACTION_SEARCH_PRODUCTS
        assert decision.args.get("query")

    def test_classify_broad_inquiry_route_is_search(self) -> None:
        inquiry_class, route = classify_product_inquiry_route(
            _ctx("أبغى الاستفسار عن العسل"), query="العسل",
        )
        assert inquiry_class == INQUIRY_CLASS_BROAD
        assert route == "search"


class TestCatalogProductGroundingGuard:
    def test_blocks_invented_honey_types(self) -> None:
        invented_reply = (
            "وعليكم السلام، المتوفر عندنا:\n"
            "- عسل الطلح البلدي\n"
            "- عسل السدر\n"
            "- عسل القطف\n"
            "- عسل الشهد"
        )
        result = apply_catalog_product_grounding_guard(
            reply=invented_reply,
            inbound_text="أبغى الاستفسار عن العسل",
            category_hint="العسل",
            evidence=_evidence(["عسل طلح نجد", "عسل سمر الحجاز"]),
            chosen_path="llm",
        )
        assert result.replaced is True
        assert "الكتالوج" in result.reply
        assert "المتوفر حاليًا" not in result.reply
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in result.reply

    def test_seasonal_honey_no_invented_availability(self) -> None:
        invented_reply = (
            "العسل الصيفي يجي بعد أسبوعين ويكون متوفر قريبًا."
        )
        result = apply_catalog_product_grounding_guard(
            reply=invented_reply,
            inbound_text="متى يجي العسل الصيفي؟",
            category_hint="العسل",
            evidence=_evidence(["عسل طلح نجد", "عسل سمر الحجاز"]),
            chosen_path="llm",
        )
        assert result.replaced is True
        assert "بعد أسبوع" not in result.reply
        assert "قريب" not in result.reply or "أؤكد" in result.reply

    def test_no_catalog_safe_uncertainty(self) -> None:
        invented_reply = "عندنا عسل السدر وعسل القطف وعسل الشهد."
        result = apply_catalog_product_grounding_guard(
            reply=invented_reply,
            inbound_text="وش المتوفر؟",
            evidence=_evidence([]),
            chosen_path="llm",
        )
        assert result.replaced is True
        for forbidden in _FORBIDDEN_INVENTED:
            assert forbidden not in result.reply

    def test_grounded_catalog_reply_allowed(self) -> None:
        grounded = build_catalog_grounded_list_reply(
            ["عسل طلح نجد", "عسل سمر الحجاز"],
            category_hint="العسل",
        )
        result = apply_catalog_product_grounding_guard(
            reply=grounded,
            inbound_text="وش أنواع العسل؟",
            evidence=_evidence(["عسل طلح نجد", "عسل سمر الحجاز"]),
            chosen_path="llm",
        )
        assert result.replaced is False

    def test_deterministic_search_path_skipped(self) -> None:
        invented = "عسل السدر وعسل القطف متوفرين."
        result = apply_catalog_product_grounding_guard(
            reply=invented,
            evidence=_evidence(["عسل طلح نجد"]),
            chosen_path="product_search_results",
        )
        assert result.replaced is False

    @pytest.mark.parametrize("forbidden", _FORBIDDEN_INVENTED)
    def test_forbidden_names_blocked_unless_in_catalog(self, forbidden: str) -> None:
        catalog = [forbidden] if forbidden == "عسل السدر" else ["عسل طلح نجد"]
        reply = f"المتوفر: {forbidden}"
        result = apply_catalog_product_grounding_guard(
            reply=reply,
            evidence=_evidence(catalog),
            chosen_path="llm",
        )
        if forbidden in catalog:
            assert result.replaced is False
        else:
            assert result.replaced is True
            assert forbidden not in result.reply
