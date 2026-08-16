"""Post-#830 live acceptance recovery — platform ownership, not AI wording.

Covers the four proven live classes:
  A/M stale commerce cannot own a new social/greeting turn
  B/C order evidence does not manufacture identity intent; genuine order still works
  D/E/F/G structured catalog referent persists, resolves, isolates
  H/I/J canonical showroom location outranks stale/inactive records
  K/L purchase-channel and store CTA regressions
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.wa_native_catalog_order import (  # noqa: E402
    persist_structured_catalog_order_referent,
)
from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    stamp_structured_presented_products,
    structured_selected_referent,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_SHOWROOM,
    CHECKOUT_CHANNEL_STORE,
    CHECKOUT_CHANNEL_WHATSAPP,
    CheckoutChannelCapabilities,
    available_channels,
    build_channel_choice_buttons,
    evaluate_checkout_route_owner,
)
from modules.ai.brain.commerce.product_visual import (  # noqa: E402
    resolve_trusted_focus_for_deictic,
)
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
)
from modules.ai.brain.turn_owner_contract import (  # noqa: E402
    POSTPROCESS_ORDER_SLOT_REPLAY,
    build_turn_owner_contract,
)
from modules.ai.brain.types import Decision, Intent, MerchantConversationState  # noqa: E402
from core.wa_link_buttons import prepare_cta_body_text  # noqa: E402


def _caps(*, store: bool = True, showroom: bool = True) -> CheckoutChannelCapabilities:
    return CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=store,
        showroom_visit=showroom,
        store_url="https://shop.example.sa" if store else "",
        store_name="متجر تجريبي عام",
    )


def _patch_brain_state(monkeypatch, state: dict) -> None:
    monkeypatch.setattr(
        "core.order_flow._load_brain_state",
        lambda _db, tenant_id, phone: (None, state),
    )


class _StubDB:
    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return None

    def add(self, *_args, **_kwargs):
        return None

    def flush(self):
        return None


class TestGreetingOwnsStaleCommerce:
    def test_greeting_social_owner_despite_stale_product_state(self) -> None:
        intent = Intent(name="greeting", confidence=0.96, raw_message="هلا")
        state = MerchantConversationState(
            stage="ordering",
            current_product_focus={"id": 77, "title": "حذاء رياضي أبيض"},
        )
        verdict = resolve_current_turn_social_non_commerce(
            "هلا",
            intent=intent,
            state=state,
        )
        assert verdict.matched is True
        assert verdict.category == "greeting"

    def test_availability_guard_does_not_rewrite_greeting_topic(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
        original = "هذا الخيار يحتاج تأكيد السعر."
        result = apply_product_availability_truth_guard(
            reply=original,
            inbound_text="هلا",
            decision_topic="persona_social",
        )
        assert result.replaced is False
        assert result.reply == original

    def test_turn_owner_blocks_order_slot_replay_on_identity(self) -> None:
        decision = Decision(
            action="llm_reply",
            args={"topic": "persona_identity", "block_commerce_escalation": True},
            reason="identity probe",
        )
        contract = build_turn_owner_contract(decision)
        assert contract.blocks(POSTPROCESS_ORDER_SLOT_REPLAY)
        assert contract.pause_order_slot_collection is True


class TestEvidenceDoesNotManufactureIntent:
    def test_identity_question_is_social_even_with_order_prep(self) -> None:
        intent = Intent(name="who_are_you", confidence=0.95, raw_message="تعرفني؟")
        state = MerchantConversationState(
            stage="ordering",
            current_product_focus={"id": 77, "title": "قميص قطني أزرق"},
        )
        verdict = resolve_current_turn_social_non_commerce(
            "تعرفني؟",
            intent=intent,
            state=state,
        )
        assert verdict.matched is True
        assert verdict.category == "persona_identity"

    def test_availability_guard_does_not_rewrite_identity_topic(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
        original = "هذا الخيار يحتاج تأكيد السعر."
        result = apply_product_availability_truth_guard(
            reply=original,
            inbound_text="تعرفني؟",
            decision_topic="persona_identity",
        )
        assert result.replaced is False
        assert result.reply == original

    def test_genuine_order_topic_is_not_classified_as_identity(self) -> None:
        intent = Intent(name="track_order", confidence=0.92, raw_message="وين طلبي")
        verdict = resolve_current_turn_social_non_commerce(
            "وين طلبي",
            intent=intent,
        )
        assert verdict.matched is False or verdict.category != "persona_identity"


class TestStructuredCatalogReferent:
    def test_catalog_order_stamp_is_customer_selected(self) -> None:
        state = SimpleNamespace(last_presented_products=[], last_recommended_products=[])
        stamped = stamp_structured_presented_products(
            state,
            [{
                "id": 88,
                "product_name": "عطر ورد 100ml",
                "product_retailer_id": "sku-rose-100",
                "price": 95,
            }],
            provenance="catalog_order_selected",
            customer_selected=True,
        )
        assert stamped
        ref = structured_selected_referent(state)
        assert ref is not None
        assert ref["id"] == 88
        assert ref["customer_selected"] is True
        assert ref["external_id"] == "sku-rose-100"

    def test_deictic_follow_up_prefers_customer_selected_over_thumbnail(self) -> None:
        state = MerchantConversationState(
            turn=4,
            last_presented_products=[
                {
                    "id": 140,
                    "title": "زيت تجريبي",
                    "provenance": "native_catalog_presented",
                    "customer_selected": False,
                },
                {
                    "id": 88,
                    "title": "عطر ورد 100ml",
                    "provenance": "catalog_order_selected",
                    "customer_selected": True,
                    "external_id": "sku-rose-100",
                },
            ],
        )
        trusted = resolve_trusted_focus_for_deictic(state, "ابي هذا")
        assert trusted.title == "عطر ورد 100ml"
        assert str(trusted.product_id) == "88"

    def test_referent_survives_save_load_roundtrip(self) -> None:
        original = MerchantConversationState()
        stamp_structured_presented_products(
            original,
            [{"id": 88, "title": "قميص قطني أزرق", "external_id": "sku-shirt"}],
            provenance="catalog_order_selected",
            customer_selected=True,
        )
        loaded = MerchantConversationState.from_dict(original.to_dict())
        ref = structured_selected_referent(loaded)
        assert ref is not None
        assert ref["id"] == 88
        assert ref["customer_selected"] is True

    def test_tenant_isolation_of_referents(self) -> None:
        t1 = SimpleNamespace(last_presented_products=[])
        t33 = SimpleNamespace(last_presented_products=[])
        stamp_structured_presented_products(
            t1,
            [{"id": 1, "title": "حذاء رياضي أبيض", "external_id": "t1-sku"}],
            provenance="native_catalog_presented",
        )
        stamp_structured_presented_products(
            t33,
            [{"id": 9, "title": "عطر ورد 100ml", "external_id": "t33-sku"}],
            provenance="catalog_order_selected",
            customer_selected=True,
        )
        assert structured_selected_referent(t1) is None
        t33_ref = structured_selected_referent(t33)
        assert t33_ref is not None
        assert t33_ref["external_id"] == "t33-sku"
        assert t33_ref["external_id"] != t1.last_presented_products[0]["external_id"]

    def test_persist_only_helper_stamps_brain_state(self) -> None:
        conv = SimpleNamespace(extra_metadata={"brain_state": {"stage": "exploring"}})
        payload_meta = {
            "source_type": "catalog_order",
            "catalog_id": "cat-1",
            "product_items": [{
                "product_retailer_id": "sku-white-sneaker",
                "quantity": 1,
                "item_price": 126,
                "currency": "SAR",
            }],
        }
        line = {
            "product_id": "88",
            "product_name": "حذاء رياضي أبيض",
            "title": "حذاء رياضي أبيض",
            "quantity": 1,
            "product_retailer_id": "sku-white-sneaker",
            "match_status": "confirmed",
        }
        fake_resolution = SimpleNamespace(
            line_items=[line],
            matched_count=1,
            unmatched_count=0,
            needs_review_count=0,
        )
        with patch(
            "core.wa_native_catalog_order.build_line_items_from_payload",
            return_value=fake_resolution,
        ), patch(
            "sqlalchemy.orm.attributes.flag_modified",
        ):
            ok = persist_structured_catalog_order_referent(
                _StubDB(),
                tenant_id=10,
                phone="966500000001",
                inbound_metadata=payload_meta,
                conversation=conv,
            )
        assert ok is True
        bs = conv.extra_metadata["brain_state"]
        presented = bs.get("last_presented_products") or []
        assert presented
        assert presented[0]["customer_selected"] is True
        assert presented[0]["provenance"] == "catalog_order_selected"


class TestCanonicalShowroomLocation:
    def test_showroom_button_delivers_active_primary_branch(self, monkeypatch) -> None:
        _patch_brain_state(
            monkeypatch,
            {"stage": "discovery", "order_prep": {"awaiting_checkout_channel": True}},
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        branch = SimpleNamespace(
            id=1,
            name="المعرض الرئيسي",
            city="الرياض",
            district="العليا",
            address="شارع التحلية",
            maps_url="https://maps.app.goo.gl/canonical-showroom",
            sort_order=0,
        )
        stale = SimpleNamespace(
            id=9,
            name="فرع قديم",
            city="مكة",
            district="بطحاء قريش",
            address="حي بطحاء قريش",
            maps_url="https://maps.app.goo.gl/stale",
            sort_order=9,
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ), patch(
            "modules.operations.branch_contact_evidence.load_active_branches",
            return_value=(branch,),
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="ignored",
                inbound_metadata={"button_id": "checkout_showroom_visit"},
            )
        assert decision is not None
        assert decision.reason == "showroom_location_delivered"
        assert decision.cta_url == branch.maps_url
        assert "بطحاء قريش" not in (decision.reply_text or "")
        assert stale.district not in (decision.reply_text or "")

    def test_inactive_wrong_location_cannot_outrank_active(self, monkeypatch) -> None:
        _patch_brain_state(
            monkeypatch,
            {"stage": "discovery", "order_prep": {"awaiting_checkout_channel": True}},
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        active = SimpleNamespace(
            id=1,
            name="المعرض",
            city="الطائف",
            district="الحلقة الغربية",
            address="حي الحلقة الغربية",
            maps_url="https://maps.app.goo.gl/taif-showroom",
            sort_order=0,
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ), patch(
            "modules.operations.branch_contact_evidence.load_active_branches",
            return_value=(active,),
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                inbound_metadata={"button_id": "checkout_showroom_visit"},
                message="زيارة المعرض",
            )
        assert decision is not None
        assert "الحلقة الغربية" in (decision.reply_text or "") or decision.cta_url == active.maps_url
        assert "بطحاء قريش" not in (decision.reply_text or "")

    def test_multiple_active_branches_use_primary_sort_order(self, monkeypatch) -> None:
        _patch_brain_state(
            monkeypatch,
            {"stage": "discovery", "order_prep": {"awaiting_checkout_channel": True}},
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        primary = SimpleNamespace(
            id=1, name="فرع الرياض", city="الرياض", district="العليا",
            address="", maps_url="https://maps.app.goo.gl/riyadh", sort_order=0,
        )
        secondary = SimpleNamespace(
            id=2, name="فرع جدة", city="جدة", district="الحمراء",
            address="", maps_url="https://maps.app.goo.gl/jeddah", sort_order=1,
        )
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ), patch(
            "modules.operations.branch_contact_evidence.load_active_branches",
            return_value=(primary, secondary),
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                inbound_metadata={"button_id": "checkout_showroom_visit"},
                message="ignored",
            )
        assert decision is not None
        assert decision.cta_url == primary.maps_url
        assert decision.cta_url != secondary.maps_url

    def test_showroom_button_is_not_staff_contact_social_owner(self) -> None:
        verdict = resolve_current_turn_social_non_commerce(
            "زيارة المعرض",
            inbound_metadata={"button_id": "checkout_showroom_visit"},
        )
        assert verdict.matched is False
        assert verdict.reason == "explicit_purchase_channel_payload"


class TestPreservedPurchaseControls:
    def test_three_channel_capability_regression(self) -> None:
        titles = [b["reply"]["title"] for b in build_channel_choice_buttons(_caps())]
        assert titles == ["طلب سريع واتساب", "المتجر الإلكتروني", "زيارة المعرض"]
        assert available_channels(_caps()) == [
            CHECKOUT_CHANNEL_WHATSAPP,
            CHECKOUT_CHANNEL_STORE,
            CHECKOUT_CHANNEL_SHOWROOM,
        ]

    def test_electronic_store_cta_regression(self) -> None:
        url = "https://shop.example.sa/ar"
        body = prepare_cta_body_text(
            f"هذا رابط المتجر الإلكتروني: {url}",
            url,
            keep_textual_url=False,
        )
        assert url not in body
        assert body
