"""Post-#831 identity projection + catalog-turn completion — platform-wide.

Protects semantic/state contracts, not exact Arabic prose.
Tenant 33 is a reproduction fixture only.
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.commerce.assistant_presented_provenance import (  # noqa: E402
    restore_selected_product_focus,
    stamp_structured_presented_products,
    structured_selected_referent,
)
from modules.ai.brain.commerce.catalog_checkout_customer_identity import (  # noqa: E402
    filter_missing_for_known_catalog_customer,
    merchant_customer_record_facts,
    resolve_catalog_checkout_customer_identity,
)
from modules.ai.brain.current_turn_social_non_commerce import (  # noqa: E402
    resolve_current_turn_social_non_commerce,
)
from modules.ai.brain.decision.actions import (  # noqa: E402
    ACTION_PROPOSE_DRAFT_ORDER,
    ACTION_SEARCH_PRODUCTS,
)
from modules.ai.brain.decision.engine import DefaultDecisionEngine  # noqa: E402
from modules.ai.brain.pipeline import _build_reply_state  # noqa: E402
from modules.ai.brain.postprocess.gender_agreement_guard import (  # noqa: E402
    apply_gender_agreement_guard,
)
from modules.ai.brain.postprocess.product_availability_truth_guard import (  # noqa: E402
    apply_product_availability_truth_guard,
)
from modules.ai.brain.types import (  # noqa: E402
    INTENT_START_ORDER,
    BrainContext,
    CommerceFacts,
    Decision,
    Intent,
    MerchantConversationState,
    SuggestionSnapshot,
)
from modules.ai.gender.detector import GENDER_MALE, detect_gender  # noqa: E402
from modules.ai.media.customer_turn_completion import (  # noqa: E402
    AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS,
    CATALOG_FRAME_MARKER,
    COMPLETION_ORPHAN,
    COMPLETION_PROTOCOL,
    COMPLETION_STRUCTURED_AND_CONTINUATION,
    catalog_order_must_not_orphan,
    classify_empty_text_early_return,
    is_structured_catalog_order_inbound,
    maybe_restore_catalog_order_semantic_text,
)
from modules.ai.media.routing_guard import resolve_semantic_customer_message  # noqa: E402


GENERIC_MERCHANT = "متجر تجريبي عام"
GENERIC_CUSTOMER = "أحمد سالم"
GENERIC_PRODUCT = "حذاء رياضي أبيض"
T33_CUSTOMER = "هيثم الحارثي"
T33_PRODUCT = "250 جرام عسل سمر الحجاز البلدي إنتاج مناحلنا من شمال الطايف"
CATALOG_FRAME = (
    f"{CATALOG_FRAME_MARKER}\n"
    "عدد أسطر الطلب: 1\n"
    "إجمالي الكمية: 1\n"
    "الإجمالي: 126 SAR\n"
    "رمز المنتج (SKU): 86bqzca62a"
)
_CATALOG_META = {
    "source_type": "catalog_order",
    "catalog_id": "1430031051699225",
    "product_items": [{
        "product_retailer_id": "86bqzca62a",
        "quantity": 1,
        "item_price": 126,
        "currency": "SAR",
    }],
}


def _facts(**kwargs) -> CommerceFacts:
    payload = dict(
        store_name=GENERIC_MERCHANT,
        has_products=True,
        product_count=4,
        in_stock_count=4,
        orderable=True,
        snapshot_fresh=True,
    )
    payload.update(kwargs)
    return CommerceFacts(**payload)


def _stamp_selected(state: MerchantConversationState, *, product_id=143, title=GENERIC_PRODUCT, sku="sku-white") -> None:
    stamp_structured_presented_products(
        state,
        [{
            "id": product_id,
            "title": title,
            "product_retailer_id": sku,
            "price": 126,
        }],
        provenance="catalog_order_selected",
        customer_selected=True,
    )


class TestGreetingControlPreserved:
    def test_availability_guard_does_not_rewrite_greeting(self, monkeypatch) -> None:
        monkeypatch.setenv("NAHLA_PRODUCT_AVAILABILITY_TRUTH_GUARD_MODE", "enforce")
        original = "وعليكم السلام! حياك الله."
        result = apply_product_availability_truth_guard(
            reply=original,
            inbound_text="السلام عليكم",
            decision_topic="persona_social",
        )
        assert result.replaced is False
        assert result.reply == original


class TestCustomerIdentityProjection:
    def test_registered_customer_name_available_on_identity_topic(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            profile={"name": T33_CUSTOMER, "customer_name": T33_CUSTOMER},
            phone="966542980511",
            customer=SimpleNamespace(id=8222, name=T33_CUSTOMER, full_name=""),
        )
        facts = merchant_customer_record_facts(identity)
        assert identity.customer_name_known is True
        assert facts["customer_name"] == T33_CUSTOMER
        assert facts["merchant_customer_record"]["registered"] is True
        assert facts["merchant_customer_record"]["personal_familiarity"] is False
        assert facts["personal_familiarity"] is False

    def test_stored_name_uses_same_authoritative_source(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            profile={"name": GENERIC_CUSTOMER},
            customer=SimpleNamespace(id=11, name=GENERIC_CUSTOMER, full_name=""),
        )
        assert identity.known_facts["customer_name"] == GENERIC_CUSTOMER
        assert identity.known_facts["customer_id"] == 11

    def test_identity_compose_projects_merchant_record_not_order_evidence(self) -> None:
        state = MerchantConversationState(
            stage="ordering",
            current_product_focus={"id": 77, "title": GENERIC_PRODUCT},
        )
        ctx = BrainContext(
            tenant_id=1,
            customer_phone="966500000001",
            message="هل تعرفني؟",
            intent=Intent(name="who_are_you", confidence=0.95, raw_message="هل تعرفني؟"),
            state=state,
            facts=_facts(),
            profile={"name": GENERIC_CUSTOMER, "customer_name": GENERIC_CUSTOMER},
        )
        reply_state = _build_reply_state(
            ctx=ctx,
            previous_state=state,
            current_state=state,
            suggestion=SuggestionSnapshot(),
            decision=Decision(
                action="llm_reply",
                args={"topic": "persona_identity", "block_commerce_escalation": True},
                reason="identity",
            ),
            merchant_context={},
            db=None,
        )
        facts = reply_state.known_facts or {}
        assert facts.get("customer_name_known") is True
        assert facts.get("customer_name") == GENERIC_CUSTOMER
        assert facts.get("personal_familiarity") is False
        assert "customer_order_evidence" not in facts
        assert facts.get("checkout_preparation") == {}

    def test_order_evidence_does_not_manufacture_identity_intent(self) -> None:
        intent = Intent(name="who_are_you", confidence=0.95, raw_message="تعرفني؟")
        state = MerchantConversationState(
            stage="ordering",
            current_product_focus={"id": 77, "title": GENERIC_PRODUCT},
        )
        verdict = resolve_current_turn_social_non_commerce(
            "تعرفني؟",
            intent=intent,
            state=state,
        )
        assert verdict.matched is True
        assert verdict.category == "persona_identity"

    def test_genuine_order_intent_not_classified_as_identity(self) -> None:
        intent = Intent(name="track_order", confidence=0.92, raw_message="وين طلبي")
        verdict = resolve_current_turn_social_non_commerce(
            "وين طلبي",
            intent=intent,
        )
        assert verdict.matched is False or verdict.category != "persona_identity"


class TestStructuredCatalogSelectionCompletion:
    def test_catalog_frame_is_kept_as_semantic_text(self) -> None:
        semantic = resolve_semantic_customer_message(
            brain_text=CATALOG_FRAME,
            inbound_metadata=_CATALOG_META,
            inbound_normalized_type="text",
        )
        assert CATALOG_FRAME_MARKER in semantic
        assert semantic.strip()

    def test_caption_strip_without_catalog_meta_still_drops_media_frame(self) -> None:
        framed = "[تصنيف صورة]\nوصف بصري للمنتج"
        semantic = resolve_semantic_customer_message(
            brain_text=framed,
            inbound_metadata={"source_type": "image"},
            inbound_normalized_type="image",
        )
        assert "تصنيف" not in semantic

    def test_empty_semantic_catalog_order_is_restored(self) -> None:
        restored, meta = maybe_restore_catalog_order_semantic_text(
            semantic_text="",
            original_brain_text=CATALOG_FRAME,
            inbound_metadata=_CATALOG_META,
        )
        assert CATALOG_FRAME_MARKER in restored
        assert meta["customer_turn_completion"]["completion_class"] == (
            COMPLETION_STRUCTURED_AND_CONTINUATION
        )
        assert catalog_order_must_not_orphan(_CATALOG_META, CATALOG_FRAME)

    def test_catalog_order_empty_text_is_not_protocol_silence(self) -> None:
        cls = classify_empty_text_early_return(
            inbound_metadata=_CATALOG_META,
            normalized_type="text",
            message=CATALOG_FRAME,
        )
        assert cls == COMPLETION_ORPHAN
        assert is_structured_catalog_order_inbound(_CATALOG_META, "")

    def test_uncaptioned_image_remains_protocol_only(self) -> None:
        cls = classify_empty_text_early_return(
            inbound_metadata={"source_type": "image"},
            normalized_type="image",
            message="",
        )
        assert cls == COMPLETION_PROTOCOL

    def test_selected_product_persists_from_retailer_id(self) -> None:
        state = MerchantConversationState()
        _stamp_selected(state, product_id=143, title=T33_PRODUCT, sku="86bqzca62a")
        ref = structured_selected_referent(state)
        assert ref is not None
        assert ref["id"] == 143
        assert ref["customer_selected"] is True
        assert ref["external_id"] == "86bqzca62a"


class TestPurchaseContinuity:
    def _start_order_ctx(self, *, tenant_id: int, with_referent: bool) -> BrainContext:
        state = MerchantConversationState(greeted=True, stage="discovery")
        if with_referent:
            _stamp_selected(state, product_id=88, title=GENERIC_PRODUCT, sku="sku-white")
            restore_selected_product_focus(state)
        return BrainContext(
            tenant_id=tenant_id,
            customer_phone="966500000001",
            message="أبي اشتري",
            intent=Intent(
                name=INTENT_START_ORDER,
                confidence=0.93,
                raw_message="أبي اشتري",
            ),
            state=state,
            facts=_facts(),
        )

    def test_selected_product_reused_on_natural_purchase(self) -> None:
        ctx = self._start_order_ctx(tenant_id=33, with_referent=True)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action != ACTION_SEARCH_PRODUCTS
        assert "top_products" not in str(decision.reason or "")
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert ctx.state.current_product_focus
        assert ctx.state.current_product_focus.get("id") == 88

    def test_tenant1_control_same_purchase_reuse(self) -> None:
        ctx = self._start_order_ctx(tenant_id=1, with_referent=True)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER
        assert decision.action != ACTION_SEARCH_PRODUCTS

    def test_generic_non_salla_same_contract(self) -> None:
        ctx = self._start_order_ctx(tenant_id=77, with_referent=True)
        ctx.facts = _facts(store_name="متجر ملابس تجريبي", has_active_integration=False)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_PROPOSE_DRAFT_ORDER

    def test_bare_start_without_selection_still_discovers(self) -> None:
        ctx = self._start_order_ctx(tenant_id=1, with_referent=False)
        decision = DefaultDecisionEngine().decide(ctx)
        assert decision.action == ACTION_SEARCH_PRODUCTS


class TestKnownCustomerFactReuse:
    def test_full_name_satisfies_last_name_slot(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            profile={"name": T33_CUSTOMER},
            order_prep={},
        )
        assert identity.customer_name_known is True
        assert identity.prep_patch.get("customer_first_name")
        assert identity.prep_patch.get("customer_last_name")
        missing = filter_missing_for_known_catalog_customer(
            ["customer_first_name", "customer_last_name", "city"],
            known_facts=identity.known_facts,
        )
        assert "customer_last_name" not in missing
        assert "customer_first_name" not in missing
        assert "city" in missing

    def test_generic_customer_name_reused_for_checkout_slots(self) -> None:
        identity = resolve_catalog_checkout_customer_identity(
            profile={"name": "نورة عبدالله"},
            phone="966511111111",
        )
        missing = filter_missing_for_known_catalog_customer(
            ["name", "customer_last_name", "phone"],
            known_facts=identity.known_facts,
            phone="966511111111",
        )
        assert missing == []


class TestExplicitCustomerCorrection:
    def test_explicit_male_self_id_is_detected(self) -> None:
        hint = detect_gender("انا هيثم الحارثي رجل ولست امرأة")
        assert hint.value == GENDER_MALE
        assert hint.confidence >= 0.9
        assert hint.source == "verb"

    def test_same_turn_does_not_keep_feminine_continue(self) -> None:
        result = apply_gender_agreement_guard(
            "شكرًا لتوضيحك، هيثم. نكمل طلبك الآن! كملي لي اسم العائلة",
            message="انا هيثم الحارثي رجل ولست امرأة",
        )
        assert "كملي" not in result.reply
        assert result.replaced is True

    def test_no_name_to_gender_exception_added(self) -> None:
        from modules.ai.gender import detector as gender_detector  # noqa: PLC0415

        names = getattr(gender_detector, "_MALE_NAMES", frozenset())
        assert "هيثم" not in names
        assert "heitham" not in {n.lower() for n in names}


class TestCompletionContractSweep:
    def test_catalog_order_path_is_repaired(self) -> None:
        repaired = [
            row for row in AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS if row.get("repaired")
        ]
        assert repaired
        assert all(row["after"] != COMPLETION_ORPHAN for row in repaired)
        catalog = next(
            row
            for row in AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS
            if row["input_type"] == "catalog_order"
        )
        assert catalog["before"] == COMPLETION_ORPHAN
        assert catalog["after"] == COMPLETION_STRUCTURED_AND_CONTINUATION

    def test_human_takeover_and_protocol_silence_preserved(self) -> None:
        classes = {row.get("class") for row in AUDITED_CUSTOMER_ORIGIN_EARLY_RETURNS}
        assert "human_owned" in classes
        assert COMPLETION_PROTOCOL in classes

    def test_webhook_continues_catalog_order_past_empty_text(self) -> None:
        webhook = os.path.join(_BACKEND, "routers", "whatsapp_webhook.py")
        src = open(webhook, encoding="utf-8").read()
        assert "catalog_order_must_not_orphan" in src
        assert "maybe_restore_catalog_order_semantic_text" in src
        assert "empty_text_no_fallback" in src


class TestTenantIsolation:
    def test_selected_product_does_not_leak_across_states(self) -> None:
        t1 = MerchantConversationState()
        t33 = MerchantConversationState()
        stamp_structured_presented_products(
            t1,
            [{"id": 1, "title": "قميص قطني أزرق", "product_retailer_id": "t1-sku"}],
            provenance="native_catalog_presented",
            customer_selected=False,
        )
        _stamp_selected(t33, product_id=143, title=T33_PRODUCT, sku="86bqzca62a")
        assert structured_selected_referent(t1) is None
        ref = structured_selected_referent(t33)
        assert ref is not None
        assert ref["external_id"] == "86bqzca62a"
        assert ref["id"] != t1.last_presented_products[0]["id"]

    def test_no_tenant_specific_runtime_branches(self) -> None:
        files = [
            os.path.join(_BACKEND, "modules", "ai", "media", "routing_guard.py"),
            os.path.join(_BACKEND, "modules", "ai", "media", "customer_turn_completion.py"),
            os.path.join(_BACKEND, "modules", "ai", "brain", "pipeline.py"),
            os.path.join(_BACKEND, "modules", "ai", "brain", "decision", "engine.py"),
            os.path.join(_BACKEND, "modules", "ai", "gender", "detector.py"),
        ]
        for path in files:
            src = open(path, encoding="utf-8").read()
            assert "tenant_id == 33" not in src
            assert "tenant_id==33" not in src


class TestLatencyHelpers:
    def test_identity_and_catalog_helpers_are_fast(self) -> None:
        t0 = time.perf_counter()
        resolve_catalog_checkout_customer_identity(profile={"name": GENERIC_CUSTOMER})
        identity_ms = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        resolve_semantic_customer_message(
            brain_text=CATALOG_FRAME,
            inbound_metadata=_CATALOG_META,
        )
        catalog_ms = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        state = MerchantConversationState()
        _stamp_selected(state)
        restore_selected_product_focus(state)
        purchase_ms = (time.perf_counter() - t2) * 1000
        t3 = time.perf_counter()
        apply_gender_agreement_guard(
            "كملي الطلب",
            message="انا رجل ولست امرأة",
        )
        correction_ms = (time.perf_counter() - t3) * 1000
        assert identity_ms < 50
        assert catalog_ms < 50
        assert purchase_ms < 50
        assert correction_ms < 50
