from __future__ import annotations

import os
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.outbound_final_boundary import (  # noqa: E402
    commerce_delivery_expected,
    outbound_body_kind,
    should_suppress_final_outbound,
)


def test_symbol_only_commerce_remnant_is_suppressed() -> None:
    required = commerce_delivery_expected(decision_action="search_products")

    assert required is True
    assert outbound_body_kind("🛒") == "symbols_only"
    assert should_suppress_final_outbound(
        "🛒",
        require_substantive_commerce_text=required,
    ) is True


def test_symbol_only_body_is_allowed_with_structured_product_delivery() -> None:
    assert should_suppress_final_outbound(
        "✨",
        pending_attachments=[{"kind": "product"}],
        require_substantive_commerce_text=True,
    ) is False


def test_non_commerce_symbol_only_expression_is_not_style_policed() -> None:
    assert should_suppress_final_outbound(
        "👍",
        require_substantive_commerce_text=False,
    ) is False


def test_substantive_commerce_text_remains_model_owned() -> None:
    assert outbound_body_kind("Available in XL") == "substantive"
    assert should_suppress_final_outbound(
        "Available in XL",
        require_substantive_commerce_text=True,
    ) is False


def test_prior_product_candidate_is_structured_commerce_evidence() -> None:
    assert commerce_delivery_expected(had_product_delivery_candidate=True) is True


def test_explicit_no_product_presentation_does_not_create_commerce_signal() -> None:
    assert commerce_delivery_expected(
        brain_result={"product_presentation_kind": "none"}
    ) is False
