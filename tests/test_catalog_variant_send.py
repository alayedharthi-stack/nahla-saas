"""tests/test_catalog_variant_send.py
─────────────────────────────────
Phase 3 coverage for the catalog refactor:

  * The catalog sender's section builder picks per-variant
    ``retailer_id`` via ``effective_variant_retailer_id`` —
    a parent with a hydrated ``default_variant`` ships the
    variant id, not the legacy parent id.
  * ``ask_product_variants`` template renders a numbered Arabic
    prompt with the per-variant option summary + price.
  * The decision engine's variant-choice gate fires when
    ``order_prep.awaiting_variant_choice=True`` and the customer
    sends a digit — routing to ACTION_PROPOSE_DRAFT_ORDER with
    the picked variant on ``args``.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ─────────────────────────────────────────────────────────────────────
# Sender: per-variant retailer_id wins
# ─────────────────────────────────────────────────────────────────────


class TestProductsToSectionPicksVariantRetailerId:

    def test_parent_with_default_variant_ships_variant_id(self):
        """When a parent ORM-like row has a populated ``default_variant``
        relationship the sender MUST use the variant's ``retailer_id``,
        not the parent's external_id (legacy fallback)."""
        from services.whatsapp_platform.catalog_sender import products_to_section

        class Variant:
            retailer_id = "ext-v1"
            product_id = 7

        class Parent:
            id = 7
            external_id = "ext"
            meta_retailer_id = None
            default_variant = Variant()

        section = products_to_section("الأكثر مبيعاً", [Parent()])
        assert section.retailer_ids == ("ext-v1",), (
            "section must carry the variant's retailer_id, not the parent's"
        )

    def test_legacy_dict_falls_back_to_parent_external_id(self):
        """Old code paths that pass a bare dict (no default_variant)
        keep working — fallback to the legacy ``effective_retailer_id``
        chain."""
        from services.whatsapp_platform.catalog_sender import products_to_section
        section = products_to_section("الأكثر مبيعاً", [
            {"id": 1, "external_id": "salla_1"},
        ])
        assert section.retailer_ids == ("salla_1",)

    def test_products_with_no_retailer_id_are_skipped(self):
        from services.whatsapp_platform.catalog_sender import products_to_section
        section = products_to_section("X", [
            {"id": 1, "external_id": ""},        # skipped
            {"id": 2, "external_id": "ok"},      # kept
        ])
        assert section.retailer_ids == ("ok",)


# ─────────────────────────────────────────────────────────────────────
# Template: ask_product_variants
# ─────────────────────────────────────────────────────────────────────


class TestAskProductVariantsTemplate:

    def test_renders_numbered_arabic_prompt(self):
        from modules.ai.brain.compose.templates import ask_product_variants
        text = ask_product_variants(
            {"title": "فستان"},
            [
                {"option_summary": "S", "price": "120", "in_stock": True},
                {"option_summary": "M", "price": "130", "in_stock": True},
                {"option_summary": "L", "price": "130", "in_stock": False},
            ],
        )
        assert "فستان" in text
        assert "1." in text and "S" in text
        assert "2." in text and "M" in text
        # Out-of-stock variants must NOT appear in the prompt — we only
        # ask about sellable choices.
        assert "L" not in text or "L" in "اختر"  # belt+braces
        # The closing line tells the customer they can answer by number
        # OR name.
        assert "رقم" in text or "اسم" in text

    def test_falls_back_when_no_sellable_variants(self):
        """An edge case: all variants out of stock. The template MUST
        produce a sane message rather than render an empty numbered
        list (which would look broken in the chat)."""
        from modules.ai.brain.compose.templates import ask_product_variants
        text = ask_product_variants(
            {"title": "حذاء"},
            [{"option_summary": "X", "in_stock": False}],
        )
        assert "حذاء" in text
        assert "1." not in text

    def test_default_variant_excluded_from_choices(self):
        """A synthetic ``is_default=True`` row is never a real choice —
        the template must skip it so the merchant doesn't see a phantom
        first option."""
        from modules.ai.brain.compose.templates import ask_product_variants
        text = ask_product_variants(
            {"title": "P"},
            [
                {"is_default": True, "option_summary": "Default",
                 "in_stock": True},
                {"option_summary": "Red", "in_stock": True},
                {"option_summary": "Blue", "in_stock": True},
            ],
        )
        assert "Default" not in text
        assert "Red" in text and "Blue" in text


# ─────────────────────────────────────────────────────────────────────
# Decision engine: variant-choice gate
# ─────────────────────────────────────────────────────────────────────


def _make_ctx(*, message: str, awaiting: bool, parent_pid: str = "7"):
    from modules.ai.brain.types import (
        BrainContext, CommerceFacts, Intent,
        MerchantConversationState, OrderPreparationState,
        INTENT_PICK_LIST_ITEM,
    )
    state = MerchantConversationState()
    state.greeted = True
    state.stage = "deciding"
    op = OrderPreparationState()
    op.awaiting_variant_choice = awaiting
    op.pending_variant_product_id = parent_pid if awaiting else ""
    state.order_prep = op
    facts = CommerceFacts(
        has_products=True, product_count=5, in_stock_count=5,
        has_active_integration=True, orderable=True, has_coupons=False,
        snapshot_fresh=True, store_name="T",
        store_url="https://t.example.com",
        within_working_hours=True,
    )
    intent = Intent(name=INTENT_PICK_LIST_ITEM, confidence=0.9,
                    slots={"list_index": 1})
    return BrainContext(
        tenant_id=1, customer_phone="+966500000000", customer_id=1,
        message=message, history=[], profile={}, intent=intent,
        state=state, facts=facts,
    )


class TestVariantChoiceGate:

    def test_digit_routes_to_propose_draft_with_pick(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
        decision = DefaultDecisionEngine().decide(
            _make_ctx(message="2", awaiting=True, parent_pid="42"),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        args = decision.args or {}
        pick = args.get("variant_pick") or {}
        assert pick.get("index_one_based") == 2
        assert args.get("pending_variant_product_id") == "42"

    def test_arabic_digit_also_routes(self):
        """٢ must be recognised as a numeric pick."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
        decision = DefaultDecisionEngine().decide(
            _make_ctx(message="٢", awaiting=True),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        pick = (decision.args or {}).get("variant_pick") or {}
        assert pick.get("index_one_based") == 2

    def test_free_text_label_routes_with_label_pick(self):
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
        decision = DefaultDecisionEngine().decide(
            _make_ctx(message="أحمر", awaiting=True),
        )
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        pick = (decision.args or {}).get("variant_pick") or {}
        assert pick.get("label") == "أحمر"

    def test_not_awaiting_does_not_short_circuit(self):
        """The gate must NOT fire when awaiting_variant_choice is
        False — otherwise every legitimate "2" reply would be hijacked
        across all turns of the conversation."""
        from modules.ai.brain.decision.engine import DefaultDecisionEngine
        from modules.ai.brain.decision.actions import ACTION_PROPOSE_DRAFT_ORDER
        decision = DefaultDecisionEngine().decide(
            _make_ctx(message="2", awaiting=False),
        )
        # Without the gate, the digit may still produce
        # ACTION_PROPOSE_DRAFT_ORDER via the existing PICK_LIST_ITEM
        # path — but the args MUST NOT carry the variant pick.
        args = decision.args or {}
        assert "variant_pick" not in args
