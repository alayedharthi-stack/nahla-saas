"""English product query extraction and Sidr↔سدر catalog expansion."""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.clarification.resolved_product_guard import (  # noqa: E402
    extract_resolved_product_subject,
    extract_resolved_product_subject_from_message,
    search_retry_queries,
)
from modules.ai.brain.commerce.catalog_query_normalization import (  # noqa: E402
    expand_catalog_search_queries,
    extract_english_order_product_query,
)
from modules.ai.brain.commerce.honey_browse_strategy import (  # noqa: E402
    customer_specified_honey_type,
)
from modules.ai.brain.intent import rules  # noqa: E402
from modules.ai.brain.product_discovery_gate import _resolved_product_query  # noqa: E402
from modules.ai.brain.types import (  # noqa: E402
    BrainContext,
    CommerceFacts,
    Intent,
    MerchantConversationState,
)


class TestEnglishOrderProductExtraction:
    @pytest.mark.parametrize(
        "message,expected",
        (
            ("I want to order Sidr honey", "Sidr honey"),
            ("I want to buy Sidr honey", "Sidr honey"),
            ("Can I order Sidr honey please", "Sidr honey"),
            ("looking for Sidr honey", "Sidr honey"),
            ("do you have Sidr honey", "Sidr honey"),
        ),
    )
    def test_extracts_product_phrase(self, message, expected):
        assert extract_english_order_product_query(message) == expected

    def test_sidr_honey_type_detected(self):
        assert customer_specified_honey_type("", "Sidr honey") == "سدر"

    def test_search_retry_includes_arabic_variants(self):
        retries = search_retry_queries("Sidr honey")
        joined = " ".join(retries)
        assert "سدر" in joined
        assert "عسل سدر" in joined

    def test_expand_catalog_search_queries(self):
        variants = expand_catalog_search_queries("Sidr honey")
        assert "سدر" in variants
        assert "عسل سدر" in variants
        assert "عسل السدر" in variants


class TestEnglishProductIntentRouting:
    def _ctx(self, message: str) -> BrainContext:
        intent = rules.match(message)
        assert intent is not None
        return BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message=message,
            intent=intent,
            state=MerchantConversationState(greeted=True, stage="discovery"),
            facts=CommerceFacts(has_products=True, orderable=True, product_count=5),
        )

    def test_start_order_intent_for_english_phrase(self):
        intent = rules.match("I want to order Sidr honey")
        assert intent is not None
        assert intent.name == "start_order"

    def test_resolved_product_subject_from_message(self):
        subject = extract_resolved_product_subject_from_message(
            "I want to order Sidr honey",
        )
        assert "Sidr" in subject

    def test_resolved_product_query_not_empty_for_start_order(self):
        ctx = self._ctx("I want to order Sidr honey")
        query = _resolved_product_query(ctx)
        assert "Sidr" in query

    def test_context_subject_blocks_generic_clarify_path(self):
        ctx = self._ctx("I want to order Sidr honey")
        subject = extract_resolved_product_subject(ctx)
        assert subject
        assert "Sidr" in subject
