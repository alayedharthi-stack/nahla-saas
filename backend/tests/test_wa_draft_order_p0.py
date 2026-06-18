"""P0 regression — silent draft orders, catalog matching, draft confirmation."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_cart_catalog_resolver import (  # noqa: E402
    ITEM_STATUS_CONFIRMED,
    ITEM_STATUS_CUSTOM_UNMATCHED,
    ITEM_STATUS_NEEDS_REVIEW,
    resolve_cart_line_item,
    resolve_cart_line_items,
)
from core.wa_draft_confirmation import (  # noqa: E402
    compose_wa_order_flow_reply,
    maybe_inject_draft_flow_reply,
    reply_covers_order_flow,
)
from core.wa_cart_line_items import normalize_line_item  # noqa: E402
from modules.ai.brain.commerce.cart_state import (  # noqa: E402
    apply_cart_intents_to_state,
    maybe_apply_cart_message,
)
from modules.ai.brain.intent.cart_intent_extractor import (  # noqa: E402
    extract_cart_intents,
    extract_cart_intents_with_context,
)
from modules.ai.brain.types import OrderPreparationState  # noqa: E402
from services.product_resolver import ProductResolution  # noqa: E402


def _resolution(**kwargs) -> ProductResolution:
    defaults = dict(
        id=1,
        external_id="ext-1",
        title="عسل طلح نجد",
        price="120",
        sale_price=None,
        image_url=None,
        product_url=None,
        description=None,
        in_stock=True,
        can_checkout=True,
        variants=[
            {"id": 10, "option_summary": "1kg", "salla_variant_id": "v1kg", "price": "120"},
            {"id": 11, "option_summary": "500g", "salla_variant_id": "v500", "price": "70"},
        ],
        needs_variant_choice=True,
        default_variant_id=10,
        default_variant_retailer_id="v1kg",
        has_variants=True,
        matched_query="عسل طلح",
        confidence="fts",
    )
    defaults.update(kwargs)
    return ProductResolution(**defaults)


# ── Scenario 1: طلح أسود → كبير → ٤ حبات ────────────────────────────────

def test_talh_aswad_flow_no_free_text_product_on_unknown_catalog() -> None:
    db = MagicMock()
    with patch(
        "services.product_resolver.resolve_best_effort",
        return_value=_resolution(title="عسل طلح نجد"),
    ):
        intents = extract_cart_intents("أحتاج عسل طلح أسود")
        assert intents and "طلح" in intents[0]["product_name"]

        state = SimpleNamespace(cart_items=[], current_product_focus={})
        prep = OrderPreparationState()
        cart, _, changed = apply_cart_intents_to_state(state=state, prep=prep, intents=intents)
        assert changed

        follow_large = extract_cart_intents_with_context("كبير", cart_items=state.cart_items)
        assert follow_large[0]["action"] == "update_variant"
        assert follow_large[0]["new_variant"] == "1kg"
        apply_cart_intents_to_state(state=state, prep=prep, intents=follow_large)

        from core.wa_cart_catalog_resolver import resolve_and_enrich_cart_state  # noqa: PLC0415

        resolution = resolve_and_enrich_cart_state(db, 33, state, prep)
        assert resolution.items[0]["match_status"] == ITEM_STATUS_CONFIRMED
        assert resolution.items[0].get("product_id")
        assert state.cart_items[0]["variant"] == "1kg"

        follow_qty = extract_cart_intents_with_context("٤ حبات", cart_items=state.cart_items)
        assert follow_qty[0]["quantity"] == 4
        apply_cart_intents_to_state(state=state, prep=prep, intents=follow_qty)

        reply = compose_wa_order_flow_reply(
            order_prep=prep,
            brain_state={"cart_items": state.cart_items},
            cart_changed=True,
            existing_reply="",
        )
        assert reply
        assert "موقع" in reply or "عنوان" in reply or "مدينة" in reply


def test_ambiguous_samr_asks_single_clarification() -> None:
    db = MagicMock()
    item = {"product_name": "عسل سمر", "query_hint": "عسل سمر", "quantity": 1}
    with patch("services.product_resolver.resolve_best_effort", return_value=None):
        with patch(
            "core.wa_cart_catalog_resolver._find_catalog_candidates",
            return_value=[],
        ):
            enriched, side = resolve_cart_line_item(db, 33, item)
    assert side.needs_clarification
    assert "تقصد" in side.clarification_question
    assert enriched["match_status"] == ITEM_STATUS_NEEDS_REVIEW
    assert not enriched.get("product_id")


# ── Scenario 2: سمر → 10 كيلo سطل ───────────────────────────────────────

def test_samr_then_10kg_bucket_variant_unavailable_reply() -> None:
    db = MagicMock()
    state = SimpleNamespace(cart_items=[], current_product_focus={})
    prep = OrderPreparationState()

    add = extract_cart_intents("سمر")
    assert add
    apply_cart_intents_to_state(state=state, prep=prep, intents=add)

    bucket = extract_cart_intents_with_context("10 كيلo سطل؟", cart_items=state.cart_items)
    assert bucket[0]["new_variant"] == "10kg"
    apply_cart_intents_to_state(state=state, prep=prep, intents=bucket)

    with patch(
        "services.product_resolver.resolve_best_effort",
        return_value=_resolution(
            title="عسل سمر الحجاز",
            variants=[
                {"id": 20, "option_summary": "1kg", "salla_variant_id": "s1"},
                {"id": 21, "option_summary": "500g", "salla_variant_id": "s500"},
            ],
        ),
    ):
        from core.wa_cart_catalog_resolver import resolve_and_enrich_cart_state  # noqa: PLC0415

        resolution = resolve_and_enrich_cart_state(db, 33, state, prep)

    assert resolution.variant_unavailable
    reply = compose_wa_order_flow_reply(
        order_prep=prep,
        brain_state={"cart_items": state.cart_items},
        catalog_resolution=resolution,
        cart_changed=True,
        existing_reply="",
    )
    assert reply
    assert "10" in reply or "سطل" in reply
    assert "كيلo" in reply or "كيلو" in reply
    assert state.cart_items[0]["match_status"] in (
        ITEM_STATUS_NEEDS_REVIEW,
        ITEM_STATUS_CONFIRMED,
    )


def test_free_text_only_item_not_confirmed() -> None:
    db = MagicMock()
    with patch("services.product_resolver.resolve_best_effort", return_value=None):
        with patch("core.wa_cart_catalog_resolver._find_catalog_candidates", return_value=[]):
            out = resolve_cart_line_items(db, 33, [{"product_name": "عسل رجال", "quantity": 1}])
    assert out.items[0]["match_status"] == ITEM_STATUS_CUSTOM_UNMATCHED
    assert not out.items[0].get("product_id")


# ── Scenario 3: no silent draft — outbound reply required ───────────────

def test_draft_sync_eligible_turn_injects_reply_when_compose_empty() -> None:
    prep = OrderPreparationState(
        line_items=[{
            "product_name": "عسل طلح نجد",
            "product_id": "ext-1",
            "variant": "1kg",
            "quantity": 1,
            "match_status": ITEM_STATUS_CONFIRMED,
            "unit_price": "120",
        }],
        cart_deltas=[{"op": "add"}],
    )
    state = SimpleNamespace(
        cart_items=prep.line_items,
        stage="ordering",
        to_dict=lambda: {"stage": "ordering", "cart_items": prep.line_items},
    )
    reply = maybe_inject_draft_flow_reply(
        reply="",
        order_prep=prep,
        brain_state=state,
        cart_changed=True,
    )
    assert reply.strip()
    assert not reply_covers_order_flow("")  # sanity


def test_draft_sync_does_not_override_good_existing_reply() -> None:
    existing = "تمام، سجلت لك الطلب مبدئيًا. أرسل الموقع أو المدينة والحي عشان نكمل الطلب."
    prep = OrderPreparationState(
        line_items=[{"product_name": "عسل", "product_id": "p1", "match_status": "confirmed"}],
        cart_deltas=[{"op": "add"}],
    )
    out = maybe_inject_draft_flow_reply(
        reply=existing,
        order_prep=prep,
        brain_state=SimpleNamespace(to_dict=lambda: {}),
        cart_changed=True,
    )
    assert out == existing


def test_message_event_expectation_helper() -> None:
    """After draft creation the pipeline must produce non-empty outbound text."""
    prep = OrderPreparationState(
        line_items=[{"product_name": "x", "product_id": "p", "match_status": "confirmed", "unit_price": "1"}],
        cart_deltas=[{"op": "add"}],
    )
    outbound = maybe_inject_draft_flow_reply(
        reply="",
        order_prep=prep,
        brain_state=SimpleNamespace(to_dict=lambda: {"stage": "ordering"}),
        cart_changed=True,
    )
    assert outbound, "draft created without outbound reply — P0 regression"


# ── Scenario 4: order_items status contract ───────────────────────────────

def test_confirmed_item_has_product_id() -> None:
    item = normalize_line_item({
        "product_name": "عسل طلح",
        "product_id": "ext-99",
        "variant_id": "v1",
        "variant": "1kg",
        "match_status": ITEM_STATUS_CONFIRMED,
    })
    assert item["product_id"] == "ext-99"
    assert item["match_status"] == ITEM_STATUS_CONFIRMED


def test_unmatched_item_has_needs_review_not_confirmed() -> None:
    item = normalize_line_item({
        "product_name": "عسل رجال",
        "query_hint": "عسل رجال",
        "match_status": ITEM_STATUS_CUSTOM_UNMATCHED,
    })
    assert item["match_status"] == ITEM_STATUS_CUSTOM_UNMATCHED
    assert "product_id" not in item or not item.get("product_id")


def test_no_random_free_text_product_name_from_extractor() -> None:
    assert extract_cart_intents("عسل رجال") == []
    assert extract_cart_intents("رجال") == []


def test_unknown_price_prompt() -> None:
    prep = OrderPreparationState(
        line_items=[{
            "product_name": "عسل طلح",
            "product_id": "ext-1",
            "match_status": ITEM_STATUS_CONFIRMED,
            "quantity": 1,
        }],
        cart_deltas=[{"op": "add"}],
    )
    reply = compose_wa_order_flow_reply(
        order_prep=prep,
        brain_state={},
        cart_changed=True,
        existing_reply="",
    )
    assert reply and "السعر" in reply
