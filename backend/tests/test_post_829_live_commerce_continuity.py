"""Post-#829 live commerce continuity — platform ownership, not AI wording.

Tenant-agnostic: Brain remains the semantic owner of unstructured NL.
Structured buttons/CTAs are capability-driven UI chrome.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.ai_disabled_gate import (  # noqa: E402
    disabled_reason_for_conversation,
    is_ai_disabled_for_conversation,
)
from core.outbound_sanitizer import sanitize_outbound_payload  # noqa: E402
from core.wa_link_buttons import (  # noqa: E402
    customer_requested_textual_url,
    prepare_cta_body_text,
    strip_empty_markdown_links,
)
from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    stamp_structured_presented_products,
)
from modules.ai.brain.commerce.checkout_route_owner import (  # noqa: E402
    CHECKOUT_CHANNEL_SHOWROOM,
    CHECKOUT_CHANNEL_STORE,
    CHECKOUT_CHANNEL_WHATSAPP,
    CheckoutChannelCapabilities,
    available_channels,
    build_channel_choice_buttons,
    evaluate_checkout_route_owner,
    resolve_explicit_purchase_channel_payload,
)
from modules.ai.brain.commerce.sales_channel_capabilities import (  # noqa: E402
    resolve_merchant_sales_channels,
    store_url_evidence_activates_channel,
)
from modules.ai.brain.commerce.unstructured_turn_ownership import (  # noqa: E402
    unstructured_natural_language_requires_brain,
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


def _caps(*, store: bool = True, showroom: bool = True) -> CheckoutChannelCapabilities:
    return CheckoutChannelCapabilities(
        whatsapp_fast=True,
        store_link=store,
        showroom_visit=showroom,
        store_url="https://shop.example.sa" if store else "",
        store_name="متجر تجريبي عام",
    )


class TestPendingChoiceFreeTextReturnsToBrain:
    def test_unstructured_purchase_intent_does_not_preempt_brain(
        self,
        monkeypatch,
    ) -> None:
        _patch_brain_state(monkeypatch, {"stage": "discovery", "order_prep": {}})
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="ابي اشتري",
            )
        assert decision is None
        assert unstructured_natural_language_requires_brain(message="ابي اشتري")

    def test_store_link_ask_is_not_channel_chrome(
        self,
        monkeypatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {
                "stage": "discovery",
                "order_prep": {"checkout_channel": "whatsapp_fast"},
            },
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ):
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="ابي رابط المتجر",
            )
        assert decision is None
        assert resolve_explicit_purchase_channel_payload(
            "ابي رابط المتجر",
            caps=_caps(),
        ) is None
        assert unstructured_natural_language_requires_brain(message="ابي رابط المتجر")

    def test_pending_buttons_free_text_returns_none_for_brain(
        self,
        monkeypatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {"stage": "discovery", "order_prep": {"awaiting_checkout_channel": True}},
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ) as persist:
            decision = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="بالمتجر الالكتروني",
            )
        assert decision is None
        persist.assert_not_called()
        assert resolve_explicit_purchase_channel_payload(
            "بالمتجر الالكتروني",
            caps=_caps(),
        ) is None

    def test_explicit_whatsapp_button_still_deterministic(
        self,
        monkeypatch,
    ) -> None:
        _patch_brain_state(
            monkeypatch,
            {"stage": "discovery", "order_prep": {"awaiting_checkout_channel": True}},
        )
        monkeypatch.setenv("CHECKOUT_ROUTE_OWNER_ENABLED", "1")
        with patch(
            "modules.ai.brain.commerce.checkout_route_owner.load_channel_capabilities",
            return_value=_caps(),
        ), patch(
            "modules.ai.brain.commerce.checkout_route_owner.persist_checkout_route_state",
            return_value=True,
        ):
            by_title = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="طلب سريع واتساب",
            )
            by_id = evaluate_checkout_route_owner(
                _StubDB(),
                tenant_id=10,
                customer_phone="966500000001",
                message="ignored",
                inbound_metadata={"button_id": "checkout_whatsapp_fast"},
            )
        assert by_title is not None and by_title.reason == "whatsapp_fast_selected"
        assert by_id is not None and by_id.reason == "whatsapp_fast_selected"


class TestCapabilityDrivenPurchaseChannels:
    def test_three_real_channels_are_represented(self) -> None:
        sales = resolve_merchant_sales_channels(
            None,
            1,
            store_url="https://shop.example.sa",
            store_url_source="merchant_profile:merchant_override",
            maps_url="https://maps.app.goo.gl/example",
        )
        ids = sales.available_purchase_channel_ids()
        assert ids == ["online_store", "whatsapp_quick_order", "showroom_visit"]
        caps = CheckoutChannelCapabilities(
            whatsapp_fast=True,
            store_link=True,
            showroom_visit=True,
        )
        titles = [b["reply"]["title"] for b in build_channel_choice_buttons(caps)]
        assert titles == ["طلب سريع واتساب", "المتجر الإلكتروني", "زيارة المعرض"]
        assert CHECKOUT_CHANNEL_WHATSAPP in available_channels(caps)
        assert CHECKOUT_CHANNEL_STORE in available_channels(caps)
        assert CHECKOUT_CHANNEL_SHOWROOM in available_channels(caps)

    def test_missing_store_and_showroom_are_omitted(self) -> None:
        sales = resolve_merchant_sales_channels(None, 1, store_url="", maps_url="")
        ids = sales.available_purchase_channel_ids()
        assert "online_store" not in ids
        assert "showroom_visit" not in ids
        assert "whatsapp_quick_order" in ids
        caps = _caps(store=False, showroom=False)
        titles = [b["reply"]["title"] for b in build_channel_choice_buttons(caps)]
        assert titles == ["طلب سريع واتساب"]

    def test_kb_url_still_does_not_activate_store(self) -> None:
        assert not store_url_evidence_activates_channel(
            source="kb_free_text:about",
            found=True,
        )


class TestStructuredProductReferent:
    def test_catalog_order_stamp_survives_deictic_follow_up(self) -> None:
        state = SimpleNamespace(last_presented_products=[], last_recommended_products=[])
        stamped = stamp_structured_presented_products(
            state,
            [{
                "id": 77,
                "product_name": "حذاء رياضي أبيض",
                "product_retailer_id": "sku-white-sneaker",
                "price": 126,
            }],
            provenance="catalog_order_selected",
            customer_selected=True,
        )
        assert stamped
        assert state.last_presented_products[0]["id"] == 77
        assert state.last_presented_products[0]["customer_selected"] is True
        assert state.last_presented_products[0]["title"] == "حذاء رياضي أبيض"

    def test_native_catalog_empty_patch_does_not_wipe_referent(self) -> None:
        from modules.ai.brain.commerce.selection_context import (  # noqa: PLC0415
            apply_selection_context_patch,
        )

        state = SimpleNamespace(
            last_presented_products=[{"id": 77, "title": "حذاء رياضي أبيض"}],
            last_presented_collections=[],
            selected_collection="",
            catalog_navigation_source="",
        )
        apply_selection_context_patch(
            state,
            {
                "catalog_navigation_source": "native_catalog",
                "selected_collection": "",
            },
        )
        assert state.last_presented_products[0]["id"] == 77

    def test_cross_tenant_referent_rows_are_not_shared(self) -> None:
        a = SimpleNamespace(last_presented_products=[])
        b = SimpleNamespace(last_presented_products=[])
        stamp_structured_presented_products(
            a,
            [{"id": 1, "title": "عطر ورد 100ml", "external_id": "t1-sku"}],
            provenance="native_catalog_presented",
        )
        stamp_structured_presented_products(
            b,
            [{"id": 9, "title": "قميص قطني أزرق", "external_id": "t33-sku"}],
            provenance="native_catalog_presented",
        )
        assert a.last_presented_products[0]["id"] == 1
        assert b.last_presented_products[0]["id"] == 9
        assert a.last_presented_products[0]["external_id"] != b.last_presented_products[0]["external_id"]


class TestStoreAndLocationPresentation:
    def test_store_cta_keeps_authoritative_url_once(self) -> None:
        url = "https://shop.example.sa/ar"
        body = prepare_cta_body_text(
            f"هذا رابط المتجر الإلكتروني: {url}",
            url,
            keep_textual_url=False,
        )
        assert url not in body
        assert body

    def test_explicit_copy_link_keeps_textual_url(self) -> None:
        url = "https://shop.example.sa/ar"
        assert customer_requested_textual_url("انسخ رابط المتجر")
        body = prepare_cta_body_text(
            f"هذا رابط المتجر الإلكتروني: {url}",
            url,
            keep_textual_url=True,
        )
        assert url in body

    def test_location_with_url_strips_empty_markdown(self) -> None:
        cleaned = strip_empty_markdown_links("تفضل موقع المعرض\n[موقع المعرض]()")
        assert "[]" not in cleaned
        assert "]()" not in cleaned
        payload = {
            "messaging_product": "whatsapp",
            "to": "966500000001",
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": "موقع المعرض [موقع المعرض]()"},
                "action": {
                    "name": "cta_url",
                    "parameters": {
                        "display_text": "موقع المتجر",
                        "url": "https://maps.app.goo.gl/example",
                    },
                },
            },
        }
        with patch(
            "core.handoff_truth.resolve_handoff_truth_active",
            return_value=SimpleNamespace(active=False, source="no_handoff_truth", verify_failed=False),
        ):
            out, mutated = sanitize_outbound_payload(payload, tenant_id=10)
        assert mutated is True
        assert "]()" not in out["interactive"]["body"]["text"]

    def test_location_without_url_has_no_empty_markdown(self) -> None:
        cleaned = strip_empty_markdown_links("ما عندي رابط خريطة محفوظ [موقع المعرض]()")
        assert "]()" not in cleaned
        assert "ما عندي رابط خريطة محفوظ" in cleaned


class TestArrivalStaffContinuity:
    def test_staff_presence_unknown_does_not_disable_ai(self) -> None:
        convo = SimpleNamespace(
            id=26,
            ai_paused=False,
            ai_paused_reason=None,
            is_human_handoff=False,
            needs_human=True,
            handoff_active=False,
            paused_by_human=False,
            taken_over_at=None,
            status="active",
        )
        assert disabled_reason_for_conversation(convo) == ""

    def test_arrival_free_text_is_unstructured_brain_owned(self) -> None:
        assert unstructured_natural_language_requires_brain(message="انا وصلت")
        assert unstructured_natural_language_requires_brain(message="طيب فيه احد")

    def test_notify_only_handoff_session_keeps_ai_on(self) -> None:
        convo = SimpleNamespace(
            id=26,
            ai_paused=False,
            ai_paused_reason=None,
            is_human_handoff=False,
            needs_human=True,
            handoff_active=False,
            paused_by_human=False,
            taken_over_at=None,
            status="active",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
            id=9, status="active", handoff_reason="customer_request",
        )
        with patch(
            "core.ai_disabled_gate._find_conversations_for_phone",
            return_value=[convo],
        ), patch(
            "core.ai_disabled_gate.is_ai_allowed_by_store_mode",
            return_value=SimpleNamespace(allowed=True, mode="on"),
        ), patch(
            "core.ownership_state.conversation_handoff_active",
            return_value=False,
        ):
            decision = is_ai_disabled_for_conversation(
                db,
                tenant_id=33,
                customer_phone="966537970430",
            )
        assert decision.disabled is False


class TestInboundIdempotencyOwnership:
    def test_distinct_inbound_text_is_not_treated_as_button_chrome(self) -> None:
        caps = _caps()
        first = resolve_explicit_purchase_channel_payload("عندكم ؟", caps=caps)
        second = resolve_explicit_purchase_channel_payload("انا وصلت", caps=caps)
        assert first is None
        assert second is None

    def test_same_button_id_replays_to_same_channel(self) -> None:
        caps = _caps()
        a = resolve_explicit_purchase_channel_payload(
            "طلب سريع واتساب",
            caps=caps,
            inbound_metadata={"button_id": "checkout_whatsapp_fast"},
        )
        b = resolve_explicit_purchase_channel_payload(
            "طلب سريع واتساب",
            caps=caps,
            inbound_metadata={"button_id": "checkout_whatsapp_fast"},
        )
        assert a == b == CHECKOUT_CHANNEL_WHATSAPP
