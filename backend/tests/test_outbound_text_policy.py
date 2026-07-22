"""Phase 1 — outbound text policy instrumentation tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.outbound_text_allowlist import (
    ALLOWED_TECHNICAL_STRINGS,
    LEGACY_DETECTION_MARKERS,
    classify_string_literal,
    extract_arabic_string_literals,
    is_allowed_technical_string,
)
from core.outbound_text_policy import (
    OutboundDeliveryType,
    OutboundTextSource,
    OutboundTextTracker,
    attach_compose_provenance,
    infer_compose_provenance,
    mark_compose_llm,
    mark_compose_template,
    merge_policy_into_extra_metadata,
)


BACKEND_ROOT = Path(__file__).resolve().parents[1]


class TestComposeProvenance:
    def test_llm_reply_tags_llm_source(self):
        result = SimpleNamespace(data={"_compose_via_llm": True})
        decision = SimpleNamespace(action="llm_reply")
        ctx = SimpleNamespace(intent=SimpleNamespace(name="ask_product"))
        policy = attach_compose_provenance(result, decision=decision, ctx=ctx, text="مرحبا")
        assert policy["text_source"] == OutboundTextSource.LLM.value
        assert policy["customer_facing_text_debt"] is False
        assert policy["policy_path"] == "brain.compose._llm_compose"

    def test_faq_template_tags_deterministic_debt(self):
        result = SimpleNamespace(data={"_compose_via_template": True})
        decision = SimpleNamespace(action="faq_reply")
        ctx = SimpleNamespace(intent=SimpleNamespace(name="ask_store_info"))
        policy = attach_compose_provenance(
            result,
            decision=decision,
            ctx=ctx,
            text="ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً.",
        )
        assert policy["text_source"] == OutboundTextSource.DETERMINISTIC.value
        assert policy["customer_facing_text_debt"] is True
        assert policy["deterministic_text_detected"] is True

    def test_hybrid_when_llm_and_template_layers(self):
        result = SimpleNamespace(
            data={
                "_compose_via_llm": True,
                "_compose_via_template": True,
                "_compose_hybrid_layers": ["order_resume_hint"],
            }
        )
        decision = SimpleNamespace(action="faq_reply")
        ctx = SimpleNamespace(intent=SimpleNamespace(name="ask_shipping"))
        policy = attach_compose_provenance(result, decision=decision, ctx=ctx, text="body")
        assert policy["text_source"] == OutboundTextSource.HYBRID.value
        assert policy["customer_facing_text_debt"] is True
        assert "order_resume_hint" in policy["compose_hybrid_layers"]


class TestSafetyNetMutations:
    def test_mutation_records_layer_op_and_text_written(self):
        tracker = OutboundTextTracker(
            text_source=OutboundTextSource.LLM,
            policy_path="brain.compose._llm_compose",
        )
        tracker.record_mutation(
            layer="store_link_safety_net",
            op="replace",
            before="حاضر، ما عندي رابط",
            after="هذا رابط المتجر: https://shop.example",
        )
        meta = tracker.to_metadata()
        assert len(meta["postprocess_mutations"]) == 1
        mut = meta["postprocess_mutations"][0]
        assert mut["layer"] == "store_link_safety_net"
        assert mut["op"] == "replace"
        assert mut["text_written"] is True
        assert meta["text_source"] == OutboundTextSource.HYBRID.value
        assert meta["customer_facing_text_debt"] is True

    def test_noop_mutation_when_text_unchanged(self):
        tracker = OutboundTextTracker(text_source=OutboundTextSource.LLM)
        tracker.record_mutation(
            layer="noop_layer",
            op="noop",
            before="same",
            after="same",
            text_written=False,
        )
        mut = tracker.to_metadata()["postprocess_mutations"][0]
        assert mut["text_written"] is False
        assert tracker.text_source == OutboundTextSource.LLM


class TestDeliveryMetadata:
    def test_cta_delivery_metadata(self):
        tracker = OutboundTextTracker(text_source=OutboundTextSource.LLM)
        tracker.set_cta_delivery(
            pre_cta_body="هذا متجرنا",
            body_after_cta=".",
            cta_url="https://shop.example",
            cta_label="فتح الرابط",
        )
        meta = tracker.to_metadata()
        assert meta["final_delivery_type"] == OutboundDeliveryType.CTA_URL.value
        assert meta["cta_url"] == "https://shop.example"
        assert meta["cta_label"] == "فتح الرابط"
        assert meta["technical_body_reason"]

    def test_native_catalog_minimal_body(self):
        tracker = OutboundTextTracker(text_source=OutboundTextSource.LLM)
        tracker.set_native_catalog(body=".")
        meta = tracker.to_metadata()
        assert meta["final_delivery_type"] == OutboundDeliveryType.NATIVE_CATALOG.value
        assert meta["catalog_sent"] is True
        assert "native_catalog_minimal_body" in meta["audit_notes"]

    def test_vcard_delivery_metadata(self):
        tracker = OutboundTextTracker()
        tracker.set_vcard_delivery(gate={"allow": True, "reason": "explicit_contact_intent"})
        meta = tracker.to_metadata()
        assert meta["final_delivery_type"] == OutboundDeliveryType.VCARD.value
        assert meta["vcard_sent"] is True
        assert meta["contact_gate"]["allow"] is True


class TestBrainResultHydration:
    def test_from_brain_result_hydrates_compose_policy(self):
        brain = {
            "outbound_text_policy": {
                "text_source": "deterministic",
                "policy_path": "brain.compose.templates.faq_reply",
                "customer_facing_text_debt": True,
            },
            "decision_action": "faq_reply",
            "intent": "ask_store_info",
        }
        tracker = OutboundTextTracker.from_brain_result(brain)
        assert tracker.text_source == OutboundTextSource.DETERMINISTIC
        assert tracker.customer_facing_text_debt is True
        assert tracker.decision_action == "faq_reply"


class TestAllowlistAndAudit:
    def test_technical_cta_label_allowed(self):
        assert is_allowed_technical_string("فتح الرابط")
        assert classify_string_literal("فتح الرابط") == "allowed_technical"

    def test_catalog_minimal_body_allowed(self):
        assert is_allowed_technical_string(".")
        assert classify_string_literal(".") == "allowed_technical"

    def test_legacy_detection_not_allowed_as_prose(self):
        for marker in LEGACY_DETECTION_MARKERS:
            assert classify_string_literal(marker) == "legacy_detection_constant"

    def test_known_debt_string_classified_as_debt(self):
        debt = "ما عندي رابط المتجر الإلكتروني محفوظ في النظام حالياً."
        assert classify_string_literal(debt) == "deterministic_customer_facing_debt"

    def test_merge_policy_into_extra_metadata(self):
        merged = merge_policy_into_extra_metadata(
            {"phone": "9665"},
            {"text_source": "llm", "customer_facing_text_debt": False},
        )
        assert merged["phone"] == "9665"
        assert merged["outbound_text_policy"]["text_source"] == "llm"

    def test_audit_scan_finds_arabic_literals_in_templates(self):
        templates_path = BACKEND_ROOT / "modules" / "ai" / "brain" / "compose" / "templates.py"
        if not templates_path.exists():
            pytest.skip("templates.py not present")
        source = templates_path.read_text(encoding="utf-8")
        literals = list(extract_arabic_string_literals(source))
        assert literals, "expected Arabic literals in templates.py"
        debt_count = sum(
            1
            for lit in literals
            if classify_string_literal(lit, filepath=str(templates_path))
            == "deterministic_customer_facing_debt"
        )
        assert debt_count >= 5, "templates.py should contain known customer-facing debt"


class TestNewProseGuard:
    NEW_FORBIDDEN_SAMPLE = "نص عربي جديد محظور للاختبار فقط"

    def test_new_forbidden_prose_not_in_allowlist(self):
        assert not is_allowed_technical_string(self.NEW_FORBIDDEN_SAMPLE)
        assert (
            classify_string_literal(self.NEW_FORBIDDEN_SAMPLE)
            == "deterministic_customer_facing_debt"
        )

    def test_allowlist_covers_only_technical_set(self):
        for s in ALLOWED_TECHNICAL_STRINGS:
            assert is_allowed_technical_string(s)


class TestInferComposeProvenance:
    def test_mark_compose_helpers(self):
        result = MagicMock()
        result.data = {}
        mark_compose_llm(result)
        assert result.data["_compose_via_llm"] is True
        mark_compose_template(result, layer="faq_template")
        assert result.data["_compose_via_template"] is True
        src, path, debt = infer_compose_provenance(
            decision_action="faq_reply",
            used_llm=True,
            used_template=True,
        )
        assert src == OutboundTextSource.HYBRID
        assert debt is True

    def test_search_products_persona_llm_metadata_tags_llm_source(self):
        result = SimpleNamespace(
            data={
                "compose_source": "persona_llm",
                "chosen_path": "fact_bound_persona_compose",
                "llm_candidate_present": True,
                "response_mode": "grounded_persona_compose",
            }
        )
        decision = SimpleNamespace(action="search_products")
        ctx = SimpleNamespace(intent=SimpleNamespace(name="ask_price"))
        policy = attach_compose_provenance(
            result,
            decision=decision,
            ctx=ctx,
            text="حذاء رياضي أبيض سعره 220 ريال.",
        )
        assert policy["text_source"] == OutboundTextSource.LLM.value
        assert policy["customer_facing_text_debt"] is False
        assert policy["deterministic_text_detected"] is False
        assert "persona" in policy["policy_path"]

    def test_fallback_deterministic_on_search_products_stays_deterministic(self):
        src, path, debt = infer_compose_provenance(
            decision_action="search_products",
            used_llm=False,
            compose_source="fallback_deterministic",
            chosen_path="catalog_miss_resolved_subject",
            llm_candidate_present=False,
        )
        assert src == OutboundTextSource.DETERMINISTIC
        assert debt is True
        assert "fallback_deterministic" in path

    def test_merchant_template_retains_template_ownership(self):
        src, path, debt = infer_compose_provenance(
            decision_action="faq_reply",
            used_llm=False,
            compose_source="merchant_template",
            chosen_path="merchant_template",
            llm_candidate_present=False,
        )
        assert src == OutboundTextSource.DETERMINISTIC
        assert debt is True
        assert "merchant_template" in path

    def test_meta_template_retains_template_ownership(self):
        src, path, debt = infer_compose_provenance(
            decision_action="greet",
            used_llm=False,
            compose_source="meta_template",
            chosen_path="meta_template",
            llm_candidate_present=False,
        )
        assert src == OutboundTextSource.DETERMINISTIC
        assert debt is True
        assert "meta_template" in path

    def test_deletion_postprocess_final_source_stays_llm_owned(self):
        src, path, debt = infer_compose_provenance(
            decision_action="search_products",
            used_llm=False,
            compose_source="persona_llm",
            chosen_path="fact_bound_persona_compose",
            llm_candidate_present=True,
            final_customer_text_source="persona_llm_postprocess",
        )
        assert src == OutboundTextSource.LLM
        assert debt is False

    def test_unapproved_compose_source_cannot_self_assert_llm(self):
        src, path, debt = infer_compose_provenance(
            decision_action="search_products",
            used_llm=False,
            compose_source="arbitrary_runtime_source",
            chosen_path="fact_bound_persona_compose",
            llm_candidate_present=True,
        )
        assert src == OutboundTextSource.DETERMINISTIC
        assert debt is True
        assert "templates.search_products" in path

    def test_llm_source_without_candidate_fails_closed_to_action_mapping(self):
        src, path, debt = infer_compose_provenance(
            decision_action="search_products",
            used_llm=False,
            compose_source="llm",
            chosen_path="llm",
            llm_candidate_present=False,
        )
        assert src == OutboundTextSource.DETERMINISTIC
        assert debt is True
