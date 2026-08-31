"""Pipeline 6b must not wipe structurally global catalog-navigation fallback.

Live event tenant=1 conversation=9 inbound=56756:
CatalogNavigateHandler returned products, then pipeline 6b
``filter_products_for_browse_turn`` extracted scopes=['منتجاتكم'] and
overwrote products to [].

Fix is ownership-metadata skip only — no customer-language phrase lists.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
os.environ.setdefault("NAHLA_TEST_NO_DB", "1")

from modules.ai.brain.catalog.catalog_browse_scope_resolver import (  # noqa: E402
    filter_products_to_merchant_group,
)
from modules.ai.brain.catalog.catalog_browse_turn_policy import (  # noqa: E402
    is_catalog_browse_message,
)
from modules.ai.brain.catalog.navigation_signals import (  # noqa: E402
    message_indicates_catalog_browse,
)
from modules.ai.brain.commerce.commerce_browse_category_guard import (  # noqa: E402
    extract_browse_category_scopes,
    filter_products_for_browse_turn,
)
from modules.ai.brain.decision.actions import ACTION_CATALOG_NAVIGATE  # noqa: E402
from modules.ai.brain.pipeline import (  # noqa: E402
    apply_pipeline_browse_category_filter,
    is_structurally_global_catalog_fallback,
    structurally_global_catalog_fallback_skip_reason,
)
from modules.ai.brain.persona.catalog_product_answer import (  # noqa: E402
    _build_catalog_navigation_bundle,
)
from modules.ai.brain.types import Decision, MerchantConversationState  # noqa: E402

MSG_LIVE = "وش منتجاتكم ؟"
MSG_JACKETS_SHOW = "ورني الجاكيتات"
MSG_JACKETS_AVAIL = "وش عندكم من الجاكيتات؟"
MSG_JACKETS_HAVE = "وش عندكم جاكيتات؟"
MSG_MULTI = "وش عندكم جاكيتات أو فساتين ؟"

PATH_TOP_FALLBACK = "catalog_navigation_top_products_fallback"
PATH_GROUP_PRODUCTS = "catalog_navigation_group_products"

_MIXED_GLOBAL = [
    {
        "id": 101,
        "title": "حذاء رياضي أبيض",
        "category": "احذية",
        "orderable": True,
        "can_checkout": True,
        "price": 189,
    },
    {
        "id": 102,
        "title": "قميص قطني أزرق",
        "category": "قمصان",
        "orderable": True,
        "can_checkout": True,
        "price": 89,
    },
    {
        "id": 103,
        "title": "عطر ورد 100ml",
        "category": "عطور",
        "orderable": True,
        "can_checkout": True,
        "price": 120,
    },
]

_APPAREL_MIXED = [
    {
        "id": 1,
        "title": "جاكيت",
        "category": "جاكيتات",
        "orderable": True,
        "can_checkout": True,
        "price": 169,
    },
    {
        "id": 2,
        "title": "فستان",
        "category": "فساتين",
        "orderable": True,
        "can_checkout": True,
        "price": 210,
    },
    {
        "id": 3,
        "title": "بنطلون",
        "category": "بناطيل",
        "orderable": True,
        "can_checkout": True,
        "price": 95,
    },
]


def _global_fallback_decision(**extra: object) -> Decision:
    args = {
        "turn_owner": "catalog_navigation",
        "owner_locked": True,
        "chosen_path": PATH_TOP_FALLBACK,
        "navigator_no_groups_fallback": True,
        "navigator_step": "top_products_fallback",
        "navigation_state_patch": {
            "selected_collection": "",
            "current_catalog_group": None,
            "catalog_navigation_source": "top_fallback",
        },
    }
    args.update(extra)
    return Decision(action=ACTION_CATALOG_NAVIGATE, args=args, reason="no groups fallback")


def _global_fallback_result_data(products: list[dict]) -> dict:
    return {
        "products": list(products),
        "turn_owner": "catalog_navigation",
        "owner_locked": True,
        "chosen_path": PATH_TOP_FALLBACK,
        "navigator_no_groups_fallback": True,
        "query": "",
        "navigation_state_patch": {
            "selected_collection": "",
            "current_catalog_group": None,
            "catalog_navigation_source": "top_fallback",
        },
    }


def _ids(products: list[dict]) -> list[int]:
    return [int(p["id"]) for p in products]


def _orphan_only_bullet_lines(text: str) -> list[str]:
    return [
        line
        for line in str(text or "").splitlines()
        if line.strip() in {"-", "•"}
    ]


class TestLexicalFalsePositiveStillPresent:
    """Guard still extracts منتجاتكم — the fix is not a phrase-list patch."""

    def test_live_message_still_extracts_inventory_word_as_scope(self) -> None:
        scopes = extract_browse_category_scopes(MSG_LIVE)
        assert scopes
        assert "منتجاتكم" in scopes

    def test_lexical_filter_still_wipes_unrelated_categories(self) -> None:
        filtered = filter_products_for_browse_turn(
            _MIXED_GLOBAL,
            message=MSG_LIVE,
            query="",
            source="",
        )
        assert _ids(filtered) == []


class TestExactProductionRegression:
    def test_pipeline_6b_preserves_executor_top_products(self) -> None:
        decision = _global_fallback_decision()
        result_data = _global_fallback_result_data(_MIXED_GLOBAL)
        state = MerchantConversationState(
            greeted=True,
            stage="exploring",
            current_catalog_group=None,
            selected_collection="",
            catalog_navigation_source="top_fallback",
        )
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
            state=state,
        ) is True
        assert structurally_global_catalog_fallback_skip_reason(
            decision=decision,
            result_data=result_data,
            state=state,
        ) == "structurally_global_catalog_fallback"

        filtered = apply_pipeline_browse_category_filter(
            list(result_data["products"]),
            message=MSG_LIVE,
            query="",
            source="",
            decision=decision,
            result_data=result_data,
            state=state,
        )
        assert _ids(filtered) == [101, 102, 103]
        assert result_data["products_before_filter"] == 3
        assert result_data["products_after_filter"] == 3
        assert result_data["browse_category_filter_applied"] == "no"
        assert result_data["browse_category_filter_skip_reason"] == (
            "structurally_global_catalog_fallback"
        )
        result_data["products"] = list(filtered)
        assert _ids(result_data["products"]) == [101, 102, 103]

    def test_compose_receives_nonempty_catalog_rows_not_absence(self) -> None:
        decision = _global_fallback_decision()
        result_data = _global_fallback_result_data(_MIXED_GLOBAL)
        products = apply_pipeline_browse_category_filter(
            list(result_data["products"]),
            message=MSG_LIVE,
            query="",
            decision=decision,
            result_data=result_data,
            state=MerchantConversationState(catalog_navigation_source="top_fallback"),
        )
        bundle, compose_rows = _build_catalog_navigation_bundle(
            tenant_id=1,
            customer_phone="966500000001",
            inbound_text=MSG_LIVE,
            products=products,
            navigator_no_groups_fallback=True,
            decision_args=decision.args,
            settings={},
        )
        facts = bundle.verified_facts
        assert compose_rows
        assert facts["has_catalog_products"] is True
        assert facts["has_eligible_products"] is True
        assert int(facts["eligible_product_count"]) == 3
        assert facts.get("no_confirmed_sellable_products") is not True
        assert not _orphan_only_bullet_lines(
            "\n".join(str(row.get("title") or "") for row in compose_rows)
        )


class TestSkipIsStructuralOnly:
    def test_message_alone_does_not_skip(self) -> None:
        decision = Decision(action=ACTION_CATALOG_NAVIGATE, args={})
        result_data = {"products": list(_MIXED_GLOBAL)}
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
            state=MerchantConversationState(),
        ) is False
        filtered = apply_pipeline_browse_category_filter(
            list(_MIXED_GLOBAL),
            message=MSG_LIVE,
            decision=decision,
            result_data=result_data,
            state=MerchantConversationState(),
        )
        assert _ids(filtered) == []
        assert result_data["browse_category_filter_applied"] == "yes"

    def test_skip_false_without_no_groups_flag(self) -> None:
        decision = _global_fallback_decision(navigator_no_groups_fallback=False)
        result_data = _global_fallback_result_data(_MIXED_GLOBAL)
        result_data["navigator_no_groups_fallback"] = False
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
        ) is False

    def test_skip_false_when_owner_not_locked(self) -> None:
        decision = _global_fallback_decision(owner_locked=False)
        result_data = _global_fallback_result_data(_MIXED_GLOBAL)
        result_data["owner_locked"] = False
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
        ) is False

    def test_skip_false_for_group_products_path(self) -> None:
        decision = Decision(
            action=ACTION_CATALOG_NAVIGATE,
            args={
                "turn_owner": "catalog_navigation",
                "owner_locked": True,
                "chosen_path": PATH_GROUP_PRODUCTS,
                "navigator_no_groups_fallback": True,
            },
        )
        result_data = {
            "turn_owner": "catalog_navigation",
            "owner_locked": True,
            "chosen_path": PATH_GROUP_PRODUCTS,
            "navigator_no_groups_fallback": True,
        }
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
        ) is False


class TestCategoryScopedBrowseStillFilters:
    def test_jackets_show_does_not_take_structural_skip(self) -> None:
        decision = Decision(action=ACTION_CATALOG_NAVIGATE, args={})
        result_data = {"products": list(_APPAREL_MIXED)}
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
        ) is False
        apply_pipeline_browse_category_filter(
            list(_APPAREL_MIXED),
            message=MSG_JACKETS_SHOW,
            decision=decision,
            result_data=result_data,
        )
        assert result_data["browse_category_filter_applied"] == "yes"
        assert result_data["browse_category_filter_skip_reason"] == ""

    def test_jacket_show_selected_group_keeps_only_jackets(self) -> None:
        kept = filter_products_to_merchant_group(
            _APPAREL_MIXED,
            product_ids=[1],
        )
        assert _ids(kept) == [1]

    def test_jacket_availability_keeps_only_jackets(self) -> None:
        decision = Decision(action=ACTION_CATALOG_NAVIGATE, args={})
        result_data = {"products": list(_APPAREL_MIXED)}
        filtered = apply_pipeline_browse_category_filter(
            list(_APPAREL_MIXED),
            message=MSG_JACKETS_AVAIL,
            decision=decision,
            result_data=result_data,
        )
        assert _ids(filtered) == [1]
        assert result_data["browse_category_filter_applied"] == "yes"

    def test_jacket_have_phrase_keeps_only_jackets(self) -> None:
        filtered = filter_products_for_browse_turn(
            _APPAREL_MIXED,
            message=MSG_JACKETS_HAVE,
            query="",
            source="top_products",
        )
        assert _ids(filtered) == [1]

    def test_honey_isolation_still_drops_cross_category(self) -> None:
        catalog = [
            {"id": 20, "title": "عسل طلح", "category": "عسل", "orderable": True},
            {"id": 21, "title": "كريم سم النحل", "category": "كريم", "orderable": True},
            {"id": 22, "title": "زيت سم النحل", "category": "زيت", "orderable": True},
        ]
        decision = Decision(action=ACTION_CATALOG_NAVIGATE, args={})
        result_data: dict = {"products": list(catalog)}
        filtered = apply_pipeline_browse_category_filter(
            catalog,
            message="وش عندكم عسل",
            decision=decision,
            result_data=result_data,
        )
        assert _ids(filtered) == [20]
        assert result_data["browse_category_filter_applied"] == "yes"


class TestMultiCategoryUnionUnchanged:
    def test_union_keeps_both_families(self) -> None:
        decision = Decision(action=ACTION_CATALOG_NAVIGATE, args={})
        result_data = {"products": list(_APPAREL_MIXED)}
        filtered = apply_pipeline_browse_category_filter(
            list(_APPAREL_MIXED),
            message=MSG_MULTI,
            source="top_products",
            decision=decision,
            result_data=result_data,
        )
        assert set(_ids(filtered)) == {1, 2}
        assert 3 not in set(_ids(filtered))
        assert result_data["browse_category_filter_applied"] == "yes"


class TestActiveGroupNoCrossCategoryLeak:
    def test_selected_group_blocks_structural_skip(self) -> None:
        decision = _global_fallback_decision()
        result_data = _global_fallback_result_data(_APPAREL_MIXED)
        state = MerchantConversationState(
            current_catalog_group={
                "id": "jackets",
                "slug": "jackets",
                "label": "جاكيتات",
            },
            selected_collection="jackets",
            catalog_navigation_source="group_products",
        )
        assert is_structurally_global_catalog_fallback(
            decision=decision,
            result_data=result_data,
            state=state,
        ) is False

    def test_merchant_group_ids_keep_only_group_products(self) -> None:
        kept = filter_products_to_merchant_group(
            _APPAREL_MIXED,
            product_ids=[1],
        )
        assert _ids(kept) == [1]


class TestRealEmptyCatalog:
    def test_empty_executor_list_stays_empty_and_absence_facts(self) -> None:
        decision = _global_fallback_decision()
        result_data = _global_fallback_result_data([])
        filtered = apply_pipeline_browse_category_filter(
            [],
            message=MSG_LIVE,
            decision=decision,
            result_data=result_data,
            state=MerchantConversationState(catalog_navigation_source="top_fallback"),
        )
        assert filtered == []
        assert result_data["products_before_filter"] == 0
        assert result_data["products_after_filter"] == 0
        bundle, compose_rows = _build_catalog_navigation_bundle(
            tenant_id=1,
            customer_phone="966500000001",
            inbound_text=MSG_LIVE,
            products=filtered,
            navigator_no_groups_fallback=True,
            decision_args=decision.args,
            settings={},
        )
        facts = bundle.verified_facts
        assert compose_rows == []
        assert facts["has_eligible_products"] is False
        assert facts["has_catalog_products"] is False
        assert int(facts["eligible_product_count"]) == 0


class TestTenantIsolation:
    def test_pipeline_filter_does_not_mix_tenant_product_lists(self) -> None:
        products_a = [dict(_MIXED_GLOBAL[0], id=11, title="حذاء تاجر أ")]
        products_b = [dict(_MIXED_GLOBAL[1], id=22, title="قميص تاجر ب")]
        decision = _global_fallback_decision()
        data_a = _global_fallback_result_data(products_a)
        data_b = _global_fallback_result_data(products_b)
        out_a = apply_pipeline_browse_category_filter(
            products_a,
            message=MSG_LIVE,
            decision=decision,
            result_data=data_a,
            state=MerchantConversationState(catalog_navigation_source="top_fallback"),
        )
        out_b = apply_pipeline_browse_category_filter(
            products_b,
            message=MSG_LIVE,
            decision=decision,
            result_data=data_b,
            state=MerchantConversationState(catalog_navigation_source="top_fallback"),
        )
        assert _ids(out_a) == [11]
        assert _ids(out_b) == [22]
        assert _ids(out_a) != _ids(out_b)


class TestPr898BroadBrowseOwnership:
    def test_live_message_still_catalog_browse_owned(self) -> None:
        assert message_indicates_catalog_browse(
            "وش منتجاتكم؟",
            intent_name="ask_product",
        ) is True
        assert is_catalog_browse_message(
            "وش منتجاتكم؟",
            intent_name="ask_product",
        ) is True
        assert is_catalog_browse_message(
            MSG_JACKETS_SHOW,
            intent_name="ask_product",
        ) is True


class TestImagePdfDeicticUntouched:
    def test_deictic_visual_helpers_unchanged(self) -> None:
        from modules.ai.brain.commerce.product_visual import (  # noqa: PLC0415
            is_deictic_visual_request,
            is_product_visual_request,
        )

        assert is_deictic_visual_request("وريني صورته") is True
        assert is_product_visual_request("وريني صورته") is True
        assert is_product_visual_request(MSG_LIVE) is False

    def test_pdf_extraction_gate_untouched(self) -> None:
        from modules.ai.media.pdf_extraction_completeness import (  # noqa: PLC0415
            assess_pdf_extraction_completeness,
        )

        assert callable(assess_pdf_extraction_completeness)
