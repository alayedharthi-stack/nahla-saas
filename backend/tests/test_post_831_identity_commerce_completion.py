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

from dataclasses import asdict

from core.wa_native_catalog_order import persist_structured_catalog_order_referent  # noqa: E402
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
from modules.ai.brain.compose.prompt_payload_slim import (  # noqa: E402
    is_routine_social_turn,
    strip_state_dict_for_prompt,
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
from modules.ai.gender.context import (  # noqa: E402
    REPLY_STYLE_MASCULINE,
    CustomerGenderContext,
)
from modules.ai.gender.detector import GENDER_MALE, GENDER_UNKNOWN, detect_gender  # noqa: E402
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
    should_continue_structured_catalog_order,
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
        reply_state.persona_expression_mode = True
        reply_state.intent_name = "who_are_you"
        assert is_routine_social_turn(reply_state) is True
        slim = strip_state_dict_for_prompt(
            asdict(reply_state),
            reply_state,
            kb_in_prompt_block=False,
        )
        slim_facts = slim.get("known_facts") or {}
        assert slim_facts.get("customer_name") == GENERIC_CUSTOMER
        assert slim_facts.get("merchant_customer_record", {}).get("registered") is True
        assert "checkout_preparation" not in slim_facts
        assert "customer_order_evidence" not in slim_facts

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
    def test_catalog_frame_is_not_customer_language(self) -> None:
        semantic = resolve_semantic_customer_message(
            brain_text=CATALOG_FRAME,
            inbound_metadata=_CATALOG_META,
            inbound_normalized_type="text",
        )
        assert CATALOG_FRAME_MARKER not in (semantic or "")
        assert is_structured_catalog_order_inbound(_CATALOG_META, "")
        assert should_continue_structured_catalog_order(_CATALOG_META, "")

    def test_caption_strip_without_catalog_meta_still_drops_media_frame(self) -> None:
        framed = "[تصنيف صورة]\nوصف بصري للمنتج"
        semantic = resolve_semantic_customer_message(
            brain_text=framed,
            inbound_metadata={"source_type": "image"},
            inbound_normalized_type="image",
        )
        assert "تصنيف" not in semantic

    def test_empty_semantic_catalog_order_continues_from_structured_metadata(self) -> None:
        restored, meta = maybe_restore_catalog_order_semantic_text(
            semantic_text="",
            original_brain_text=CATALOG_FRAME,
            inbound_metadata=_CATALOG_META,
        )
        assert restored == ""
        assert CATALOG_FRAME_MARKER not in restored
        assert meta["catalog_order_structured_event"] is True
        assert meta["synthetic_customer_phrase"] is False
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


class _RecordingSession:
    def __init__(self, *, flush_error=None, commit_error=None):
        self.commits = 0
        self.flushes = 0
        self.rollbacks = 0
        self.added = []
        self.pending_unrelated = ["unrelated_write"]
        self.flush_error = flush_error
        self.commit_error = commit_error

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        if self.flush_error:
            raise self.flush_error
        self.flushes += 1

    def commit(self):
        if self.commit_error:
            raise self.commit_error
        self.commits += 1
        self.pending_unrelated.clear()

    def rollback(self):
        self.rollbacks += 1
        self.pending_unrelated.clear()


class TestCatalogReferentTransactionOwnership:
    def _payload(self):
        return {
            "source_type": "catalog_order",
            "catalog_id": "cat-1",
            "product_items": [{
                "product_retailer_id": "sku-white-sneaker",
                "quantity": 1,
                "item_price": 126,
                "currency": "SAR",
            }],
        }

    def _line(self):
        return {
            "product_id": "88",
            "product_name": GENERIC_PRODUCT,
            "title": GENERIC_PRODUCT,
            "quantity": 1,
            "product_retailer_id": "sku-white-sneaker",
            "match_status": "confirmed",
        }

    def test_helper_flushes_without_committing_unrelated_writes(self) -> None:
        conv = SimpleNamespace(extra_metadata={"brain_state": {"stage": "exploring"}})
        db = _RecordingSession()
        fake_resolution = SimpleNamespace(
            line_items=[self._line()],
            matched_count=1,
            unmatched_count=0,
            needs_review_count=0,
        )
        with patch(
            "core.wa_native_catalog_order.build_line_items_from_payload",
            return_value=fake_resolution,
        ), patch("sqlalchemy.orm.attributes.flag_modified"):
            ok = persist_structured_catalog_order_referent(
                db,
                tenant_id=10,
                phone="966500000001",
                inbound_metadata=self._payload(),
                conversation=conv,
            )
        assert ok is True
        assert db.flushes == 1
        assert db.commits == 0
        assert db.pending_unrelated == ["unrelated_write"]
        presented = conv.extra_metadata["brain_state"].get("last_presented_products") or []
        assert presented
        db.commit()
        assert db.commits == 1
        assert db.pending_unrelated == []

    def test_flush_failure_rolls_back_and_is_observable(self) -> None:
        conv = SimpleNamespace(extra_metadata={"brain_state": {"stage": "exploring"}})
        db = _RecordingSession(flush_error=RuntimeError("flush failed"))
        fake_resolution = SimpleNamespace(
            line_items=[self._line()],
            matched_count=1,
            unmatched_count=0,
            needs_review_count=0,
        )
        with patch(
            "core.wa_native_catalog_order.build_line_items_from_payload",
            return_value=fake_resolution,
        ), patch("sqlalchemy.orm.attributes.flag_modified"):
            ok = persist_structured_catalog_order_referent(
                db,
                tenant_id=10,
                phone="966500000001",
                inbound_metadata=self._payload(),
                conversation=conv,
            )
        assert ok is False
        assert db.commits == 0
        assert db.rollbacks == 1

    def test_caller_commit_failure_is_not_swallowed_by_helper(self) -> None:
        db = _RecordingSession(commit_error=RuntimeError("commit failed"))
        with pytest.raises(RuntimeError, match="commit failed"):
            db.commit()
        assert db.commits == 0

    def test_inbound_replay_stamp_is_idempotent(self) -> None:
        conv = SimpleNamespace(extra_metadata={"brain_state": {"stage": "exploring"}})
        db = _RecordingSession()
        fake_resolution = SimpleNamespace(
            line_items=[self._line()],
            matched_count=1,
            unmatched_count=0,
            needs_review_count=0,
        )
        with patch(
            "core.wa_native_catalog_order.build_line_items_from_payload",
            return_value=fake_resolution,
        ), patch("sqlalchemy.orm.attributes.flag_modified"):
            first = persist_structured_catalog_order_referent(
                db,
                tenant_id=10,
                phone="966500000001",
                inbound_metadata=self._payload(),
                conversation=conv,
            )
            second = persist_structured_catalog_order_referent(
                db,
                tenant_id=10,
                phone="966500000001",
                inbound_metadata=self._payload(),
                conversation=conv,
            )
        assert first is True
        assert second is True
        assert db.commits == 0
        presented = conv.extra_metadata["brain_state"].get("last_presented_products") or []
        assert len(presented) >= 1
        assert presented[0]["customer_selected"] is True


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

    def test_two_token_first_slot_satisfies_last_name(self) -> None:
        from modules.ai.brain.execution.orders import (  # noqa: PLC0415
            _missing_checkout_fields,
            _prep_has_real_name,
        )
        from modules.ai.brain.types import OrderPreparationState  # noqa: PLC0415

        first_only = OrderPreparationState(customer_first_name="هيثم")
        assert _prep_has_real_name(first_only) is False
        missing = _missing_checkout_fields(first_only, is_sa=True)
        assert "customer_last_name" in missing

        full = OrderPreparationState(customer_first_name="هيثم الحارثي")
        missing_full = _missing_checkout_fields(full, is_sa=True)
        assert "customer_last_name" not in missing_full
        assert "customer_first_name" not in missing_full
        assert full.customer_last_name == "الحارثي"

        generic = OrderPreparationState(customer_first_name="أحمد سالم")
        missing_generic = _missing_checkout_fields(generic, is_sa=True)
        assert "customer_last_name" not in missing_generic


class TestExplicitCustomerCorrection:
    def test_self_identification_phrases_are_not_a_semantic_parser(self) -> None:
        from modules.ai.gender import detector as gender_detector  # noqa: PLC0415

        src = open(gender_detector.__file__, encoding="utf-8").read()
        assert "_EXPLICIT_MALE_PATTERNS" not in src
        assert "_from_explicit_self_identification" not in src
        hint = detect_gender("انا هيثم الحارثي رجل ولست امرأة")
        assert hint.value == GENDER_UNKNOWN
        assert hint.source in {"none", "unknown"}

    def test_grammar_guard_consumes_structured_masculine_state(self) -> None:
        ctx = CustomerGenderContext(
            gender=GENDER_MALE,
            confidence="high",
            confidence_score=0.95,
            source="profile",
            reply_style=REPLY_STYLE_MASCULINE,
        )
        result = apply_gender_agreement_guard(
            "شكرًا لتوضيحك، هيثم. نكمل طلبك الآن! كملي لي اسم العائلة",
            gender_context=ctx,
            message="انا هيثم الحارثي رجل ولست امرأة",
        )
        assert "كملي" not in result.reply
        assert result.replaced is True
        assert result.reply_style == REPLY_STYLE_MASCULINE

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
        assert "should_continue_structured_catalog_order" in src
        assert "maybe_restore_catalog_order_semantic_text" in src
        assert "empty_text_no_fallback" in src
        assert "synthetic_customer_phrase" in src
        assert "CATALOG_FRAME_MARKER" not in src
        helper = os.path.join(_BACKEND, "core", "wa_native_catalog_order.py")
        helper_src = open(helper, encoding="utf-8").read()
        persist_fn = helper_src.split("def persist_structured_catalog_order_referent", 1)[1]
        persist_fn = persist_fn.split("\n__all__", 1)[0]
        assert "db.commit()" not in persist_fn
        assert "commit()" not in persist_fn


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
            gender_context=CustomerGenderContext(
                gender=GENDER_MALE,
                confidence="high",
                confidence_score=0.95,
                source="profile",
                reply_style=REPLY_STYLE_MASCULINE,
            ),
        )
        correction_ms = (time.perf_counter() - t3) * 1000
        assert identity_ms < 50
        assert catalog_ms < 50
        assert purchase_ms < 50
        assert correction_ms < 50
