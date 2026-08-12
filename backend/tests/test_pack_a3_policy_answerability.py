"""
Pack A3 — long-form MKS customer answerability (routing + projection + retrieval).

Asserts ownership, provenance, and honesty — not exact Arabic customer wording.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _facts(**kwargs: Any) -> SimpleNamespace:
    base: Dict[str, Any] = {
        "merchant_policy": {},
        "store_story_status": "UNKNOWN",
        "store_story_doc_ref": "",
        "merchant_capabilities": {},
        "store_description": "",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _fake_section(
    *,
    section_id: int,
    tenant_id: int,
    kind: str,
    body: str,
    title: str = "",
    source: str = "manual",
    product_links: Optional[List[Any]] = None,
) -> MagicMock:
    row = MagicMock()
    row.id = section_id
    row.tenant_id = tenant_id
    row.kind = kind
    row.body = body
    row.title = title or kind
    row.source = source
    row.priority = 10
    row.updated_at = None
    row.metadata_json = {"content_hash": f"h{section_id}"}
    row.product_links = product_links or []
    row.deleted_at = None
    row.is_active = True
    return row


class TestInformationalVsOperationalBoundary:
    def test_return_policy_question_not_complaint(self):
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (
            classify_complaint_refund,
            classify_complaint_refund_kind,
            try_complaint_refund_decision,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
            classify_merchant_policy_topic,
        )

        msg = "وش سياسة الاسترجاع؟"
        assert classify_complaint_refund_kind(msg) == "informational_policy"
        assert not classify_complaint_refund(msg)
        assert classify_merchant_policy_topic(msg) == "return_policy"
        assert try_complaint_refund_decision(SimpleNamespace(message=msg, tenant_id=1)) is None
        dec = build_merchant_policy_decision(message=msg, facts=_facts())
        assert dec is not None
        assert dec.args["topic"] == "merchant_knowledge_return_policy"
        assert dec.args["merchant_policy_status"] == "UNKNOWN"

    def test_operational_return_still_complaint(self):
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (
            classify_complaint_refund,
            try_complaint_refund_decision,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        for msg in (
            "أبغى أرجع طلبي",
            "أبي استرد فلوسي",
            "طلبي فيه مشكلة وأبي أرجعه",
            "أبغى أستبدل المنتج اللي طلبته",
            "ارجعوا لي فلوسي",
        ):
            assert classify_complaint_refund(msg), msg
            assert try_complaint_refund_decision(
                SimpleNamespace(message=msg, tenant_id=1)
            ) is not None, msg
            assert build_merchant_policy_decision(message=msg) is None, msg

    def test_sticky_complaint_yields_for_informational_without_clearing(self):
        from modules.ai.brain.commerce.complaint_refund_topic_guard import (
            apply_complaint_refund_session_flags,
            is_complaint_refund_active,
            mark_complaint_refund_active,
            should_block_order_draft_injection,
            try_complaint_refund_decision,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        state = SimpleNamespace(commerce_session={"complaint_refund_active": True})
        assert is_complaint_refund_active(state)
        info = "وش سياسة الاسترجاع؟"
        assert try_complaint_refund_decision(
            SimpleNamespace(message=info, tenant_id=1)
        ) is None
        dec = build_merchant_policy_decision(message=info, facts=_facts())
        assert dec is not None
        assert dec.args["topic"].startswith("merchant_knowledge_")
        # Sticky flag must remain (yield for turn, do not clear).
        apply_complaint_refund_session_flags(state, info, None)
        assert is_complaint_refund_active(state)
        assert not should_block_order_draft_injection(
            brain_state=state,
            customer_message=info,
        )
        # Genuine continuation still sticky-owned for draft blocking.
        cont = "كمان المنتج مغشوش"
        mark_complaint_refund_active(state, active=True)
        assert should_block_order_draft_injection(
            brain_state=state,
            customer_message=cont,
        )


class TestMerchantPolicyProjection:
    def test_projects_status_and_doc_ref_not_prose(self):
        from modules.ai.brain.truth_surface.contract import (
            TrustedContextSnapshot,
            TrustedDomain,
            TrustedFact,
            TruthSource,
        )
        from modules.ai.brain.truth_surface.trusted_context_brain_projection import (
            project_trusted_context_brain_facts,
        )

        facts = [
            TrustedFact(
                domain=TrustedDomain.MERCHANT_POLICY,
                key="policy_return_policy.status",
                value="KNOWN_PRESENT",
                source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
                path="pack_a1.policy_return_policy.status",
            ),
            TrustedFact(
                domain=TrustedDomain.MERCHANT_POLICY,
                key="policy_return_policy.doc_ref",
                value="mks:122",
                source=TruthSource.MERCHANT_KNOWLEDGE_SECTIONS,
                path="pack_a1.policy_return_policy.doc_ref",
            ),
            TrustedFact(
                domain=TrustedDomain.MERCHANT_POLICY,
                key="shipping_policy",
                value="legacy prose must not project",
                source=TruthSource.STORE_SNAPSHOT,
                path="commerce_facts.shipping_policy",
            ),
        ]
        snap = TrustedContextSnapshot(
            tenant_id=33,
            customer_phone="966500000033",
            conversation_id=1,
            facts=facts,
            loaded_domains=[TrustedDomain.MERCHANT_POLICY.value],
        )
        out = project_trusted_context_brain_facts(
            snapshot=snap,
            tenant_id=33,
            customer_phone="966500000033",
            conversation_id=1,
        )
        assert "merchant_policy" in out
        assert out["merchant_policy"]["return_policy"]["status"] == "KNOWN_PRESENT"
        assert out["merchant_policy"]["return_policy"]["doc_ref"] == "mks:122"
        # Legacy prose key must not appear as a projected policy body.
        assert "legacy prose" not in str(out)
        assert out["merchant_policy"].get("shipping_policy", {}).get("status") in (
            None,
            "UNKNOWN",
        ) or "shipping_policy" not in out["merchant_policy"]


class TestPresentAndUnknownPaths:
    def test_present_return_policy_decision_and_retrieval(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )
        from services.merchant_document_retrieval import retrieve_merchant_documents

        facts = _facts(
            merchant_policy={
                "return_policy": {
                    "status": "KNOWN_PRESENT",
                    "doc_ref": "mks:122",
                }
            }
        )
        msg = "وش سياسة الاسترجاع؟"
        dec = build_merchant_policy_decision(message=msg, facts=facts)
        assert dec is not None
        assert dec.args["merchant_policy_status"] == "KNOWN_PRESENT"
        assert dec.args["doc_ref"] == "mks:122"
        assert dec.args["knowledge_kind"] == "return_policy"

        row = _fake_section(
            section_id=122,
            tenant_id=33,
            kind="return_policy",
            body="يمكن الاسترجاع خلال 7 أيام وفق شروط المتجر التجريبي العام.",
            title="سياسة الاسترجاع",
        )
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
            row
        ]
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 33, msg)
        assert result.matched_intent == "return_family"
        assert len(result.sections) == 1
        assert result.sections[0].provenance["doc_ref"] == "mks:122"
        assert "7 أيام" in result.sections[0].body

    def test_unknown_return_policy_no_invention_signal(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        dec = build_merchant_policy_decision(
            message="وش سياسة الاسترجاع؟",
            facts=_facts(merchant_policy={"return_policy": {"status": "UNKNOWN"}}),
        )
        assert dec is not None
        assert dec.args["merchant_policy_status"] == "UNKNOWN"
        assert "Do NOT invent" in dec.args["response_goal"]
        assert dec.args.get("doc_ref") in (None, "")


class TestShippingOwnershipFork:
    def test_shipping_policy_vs_companies(self):
        from modules.ai.brain.commerce.merchant_capability_faq import (
            is_merchant_shipping_companies_question,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
            classify_merchant_policy_topic,
        )

        policy_q = "وش سياسة الشحن عندكم؟"
        companies_q = "وش شركات الشحن عندكم؟"
        assert classify_merchant_policy_topic(policy_q) == "shipping_policy"
        assert not is_merchant_shipping_companies_question(policy_q)
        assert is_merchant_shipping_companies_question(companies_q)
        assert classify_merchant_policy_topic(companies_q) is None
        assert build_merchant_policy_decision(message=companies_q) is None
        dec = build_merchant_policy_decision(message=policy_q, facts=_facts())
        assert dec is not None
        assert dec.args["knowledge_kind"] == "shipping_policy"


class TestWarrantyAndFaqVsCatalog:
    def test_warranty_policy_not_catalog_yield(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
            should_yield_catalog_for_merchant_policy,
        )

        msg = "هل عندكم سياسة ضمان؟"
        assert not should_yield_catalog_for_merchant_policy(message=msg)
        dec = build_merchant_policy_decision(message=msg, facts=_facts())
        assert dec is not None
        assert dec.args["knowledge_kind"] == "warranty"
        assert dec.args.get("block_catalog_navigation") is True

    def test_product_warranty_not_merchant_wide(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            classify_merchant_policy_topic,
        )

        assert classify_merchant_policy_topic("هل هذا المنتج عليه ضمان؟") is None

    def test_faq_deferred_no_customer_dump(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
            is_deferred_faq_customer_question,
            should_yield_catalog_for_merchant_policy,
        )
        from services.merchant_document_retrieval import retrieve_merchant_documents

        msg = "عندكم أسئلة شائعة؟"
        assert is_deferred_faq_customer_question(msg)
        assert not should_yield_catalog_for_merchant_policy(message=msg)
        dec = build_merchant_policy_decision(message=msg)
        assert dec is not None
        assert dec.args["topic"] == "merchant_knowledge_faq_deferred"
        assert dec.args["faq_visibility"] == "deferred"
        db = MagicMock()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, msg)
        assert len(result.sections) == 0


class TestStoreStoryVsA2About:
    def test_explicit_story_vs_ordinary_about(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
            classify_merchant_policy_topic,
        )
        from modules.ai.brain.commerce.merchant_profile_intents import (
            build_merchant_profile_decision,
            classify_store_profile_topic,
        )

        about = "حدثني عن المتجر"
        story = "وش قصة المتجر؟"
        assert classify_store_profile_topic(about) == "store_about"
        assert classify_merchant_policy_topic(about) is None
        about_dec = build_merchant_profile_decision(
            message=about,
            facts=_facts(store_description="وصف متجر تجريبي عام"),
        )
        assert about_dec is not None
        assert about_dec.args["topic"] == "store_about"

        assert classify_store_profile_topic(story) is None
        assert classify_merchant_policy_topic(story) == "store_story"
        story_dec = build_merchant_policy_decision(
            message=story,
            facts=_facts(
                store_story_status="KNOWN_PRESENT",
                store_story_doc_ref="mks:99",
            ),
        )
        assert story_dec is not None
        assert story_dec.args["knowledge_kind"] == "store_story"
        assert story_dec.args["doc_ref"] == "mks:99"


class TestDualTenantIsolation:
    def test_retrieval_bodies_and_doc_refs_isolated(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        a = _fake_section(
            section_id=10,
            tenant_id=10,
            kind="return_policy",
            body="BODY_TENANT_A_UNIQUE",
        )
        b = _fake_section(
            section_id=20,
            tenant_id=20,
            kind="return_policy",
            body="BODY_TENANT_B_UNIQUE",
        )

        def _run(tenant_id: int, row: MagicMock):
            db = MagicMock()
            q = db.query.return_value
            q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [
                row
            ]
            with patch(
                "core.knowledge.apply_ai_visible_kb_query_filters",
                side_effect=lambda query: query,
            ):
                return retrieve_merchant_documents(db, tenant_id, "وش سياسة الاسترجاع؟")

        ra = _run(10, a)
        rb = _run(20, b)
        assert ra.sections[0].provenance["doc_ref"] == "mks:10"
        assert rb.sections[0].provenance["doc_ref"] == "mks:20"
        assert "BODY_TENANT_A_UNIQUE" in ra.sections[0].body
        assert "BODY_TENANT_B_UNIQUE" in rb.sections[0].body
        assert "BODY_TENANT_B_UNIQUE" not in ra.sections[0].body
        assert "BODY_TENANT_A_UNIQUE" not in rb.sections[0].body


class TestPackBAndA2RegressionOwnership:
    def test_pack_b_payment_and_companies_not_stolen(self):
        from modules.ai.brain.commerce.merchant_capability_faq import (
            is_merchant_payment_methods_question,
            is_merchant_shipping_companies_question,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        assert is_merchant_payment_methods_question("وش طرق الدفع عندكم؟")
        assert is_merchant_shipping_companies_question("وش شركات الشحن عندكم؟")
        assert build_merchant_policy_decision(message="وش طرق الدفع عندكم؟") is None
        assert build_merchant_policy_decision(message="وش شركات الشحن عندكم؟") is None

    def test_pack_a2_url_contact_currency_status(self):
        from modules.ai.brain.commerce.merchant_profile_intents import (
            build_merchant_profile_decision,
        )

        assert build_merchant_profile_decision(message="وش رابط المتجر؟") is not None
        assert build_merchant_profile_decision(message="كيف أتواصل معكم؟") is not None
        assert build_merchant_profile_decision(message="وش عملة المتجر؟") is not None
        assert build_merchant_profile_decision(message="هل المتجر نشط؟") is not None


class TestTermsPrivacyExchangeRefund:
    def test_kinds_route_to_knowledge(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            classify_merchant_policy_topic,
        )

        assert classify_merchant_policy_topic("وش شروط وأحكام المتجر؟") == "terms_policy"
        assert classify_merchant_policy_topic("وش سياسة الخصوصية؟") == "privacy_policy"
        assert classify_merchant_policy_topic("وش شروط الاستبدال؟") == "exchange_policy"
        assert classify_merchant_policy_topic("كم سياسة الاسترداد؟") == "refund_policy"


class TestUnknownHonestyFactScope:
    def test_shipping_policy_suppresses_pack_b_neighbors(self):
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            apply_knowledge_turn_fact_scope,
            knowledge_turn_suppressed_fact_keys,
            should_inject_shipping_knowledge_facts,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        dec = build_merchant_policy_decision(
            message="وش سياسة الشحن عندكم؟",
            facts=_facts(merchant_policy={"shipping_policy": {"status": "UNKNOWN"}}),
        )
        assert dec is not None
        assert not should_inject_shipping_knowledge_facts(dec.args)
        keys = knowledge_turn_suppressed_fact_keys(dec.args)
        assert "shipping_methods" in keys
        assert "shipping_knowledge" in keys
        assert "shipping_policy" in keys
        assert "merchant_capability_answer" in keys
        scoped = apply_knowledge_turn_fact_scope(
            {
                "shipping_methods": ["Dev Company"],
                "shipping_knowledge": {"need_city": True},
                "shipping_policy": "legacy prose",
                "merchant_capability_answer": {"shipping_companies": ["Dev Company"]},
                "merchant_capabilities": {
                    "payments": {"status": "known"},
                    "shipping": {"companies": ["Dev Company"]},
                },
                "merchant_policy": {"shipping_policy": {"status": "UNKNOWN"}},
                "store_name": "متجر تجريبي عام",
            },
            dec.args,
        )
        assert "shipping_methods" not in scoped
        assert "shipping_knowledge" not in scoped
        assert "shipping_policy" not in scoped
        assert "merchant_capability_answer" not in scoped
        assert "shipping" not in (scoped.get("merchant_capabilities") or {})
        assert scoped["merchant_policy"]["shipping_policy"]["status"] == "UNKNOWN"
        assert scoped["store_name"] == "متجر تجريبي عام"

    def test_shipping_companies_not_suppressed(self):
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            knowledge_turn_suppressed_fact_keys,
            should_inject_shipping_knowledge_facts,
        )

        args = {"topic": "ask_shipping", "topic_hint": "shipping_companies"}
        assert should_inject_shipping_knowledge_facts(args)
        assert knowledge_turn_suppressed_fact_keys(args) == frozenset()

    def test_store_story_strips_profile_description(self):
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            scope_merchant_profiles_for_knowledge_turn,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        dec = build_merchant_policy_decision(
            message="وش قصة المتجر؟",
            facts=_facts(store_story_status="UNKNOWN"),
        )
        assert dec is not None
        mp, tp = scope_merchant_profiles_for_knowledge_turn(
            {"description": "وصف متجر تجريبي عام", "name": "متجر تجريبي عام"},
            {"description": "وصف متجر تجريبي عام", "store_name": "متجر تجريبي عام"},
            dec.args,
        )
        assert "description" not in mp
        assert "description" not in tp
        assert mp.get("name") == "متجر تجريبي عام"

    def test_about_keeps_description(self):
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            scope_merchant_profiles_for_knowledge_turn,
        )

        mp, tp = scope_merchant_profiles_for_knowledge_turn(
            {"description": "وصف متجر تجريبي عام"},
            {"description": "وصف متجر تجريبي عام"},
            {"topic": "store_about"},
        )
        assert mp["description"] == "وصف متجر تجريبي عام"
        assert tp["description"] == "وصف متجر تجريبي عام"

    def test_response_goal_outranks_checkout_next_goal(self):
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import SuggestionSnapshot

        dec = build_merchant_policy_decision(
            message="وش سياسة الشحن عندكم؟",
            facts=_facts(merchant_policy={"shipping_policy": {"status": "UNKNOWN"}}),
        )
        assert dec is not None
        goal = _compose_base_response_goal(
            dec,
            SuggestionSnapshot(),
            checkout_facts={
                "next_goal": "confirm_customer_and_shipping_details_once",
            },
        )
        assert "Do NOT invent" in goal
        assert "confirm_customer_and_shipping_details_once" not in goal

    def test_pending_city_survives_shipping_policy_skip(self):
        """Skipping A3 shipping_knowledge inject must not clear pending city."""
        from core.checkout_shipping_policy import (
            get_pending_shipping_city,
            pin_pending_shipping_city,
        )
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            should_inject_shipping_knowledge_facts,
        )
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )
        from modules.ai.brain.types import MerchantConversationState

        dec = build_merchant_policy_decision(
            message="وش سياسة الشحن عندكم؟",
            facts=_facts(merchant_policy={"shipping_policy": {"status": "UNKNOWN"}}),
        )
        assert dec is not None
        assert not should_inject_shipping_knowledge_facts(dec.args)
        state = MerchantConversationState(stage="browsing", greeted=True)
        pin_pending_shipping_city(state, source="ask_shipping")
        assert get_pending_shipping_city(state) is not None
        # Simulate A3 skip path: do not call clear/pin when knowledge surface owns turn.
        assert get_pending_shipping_city(state) is not None


class TestTruthHookFailureContracts:
    def test_shipping_scope_failure_does_not_restore_pack_b_neighbors(self):
        from modules.ai.brain.commerce import merchant_knowledge_fact_scope as scope_mod
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        dec = build_merchant_policy_decision(
            message="وش سياسة الشحن عندكم؟",
            facts=_facts(merchant_policy={"shipping_policy": {"status": "UNKNOWN"}}),
        )
        assert dec is not None
        fuel = {
            "shipping_methods": ["Dev Company"],
            "shipping_knowledge": {"need_city": True},
            "shipping_policy": "legacy prose",
            "merchant_capability_answer": {"shipping_companies": ["Dev Company"]},
            "merchant_capabilities": {"shipping": {"companies": ["Dev Company"]}},
            "merchant_policy": {"shipping_policy": {"status": "UNKNOWN"}},
            "store_name": "متجر تجريبي عام",
        }
        with patch.object(
            scope_mod,
            "apply_knowledge_turn_fact_scope",
            side_effect=RuntimeError("simulated_scope_failure"),
        ):
            scoped = scope_mod.safe_apply_knowledge_turn_fact_scope(fuel, dec.args)
        assert "shipping_methods" not in scoped
        assert "shipping_knowledge" not in scoped
        assert "shipping_policy" not in scoped
        assert "merchant_capability_answer" not in scoped
        assert "shipping" not in (scoped.get("merchant_capabilities") or {})
        assert scoped["merchant_policy"]["shipping_policy"]["status"] == "UNKNOWN"

    def test_story_scope_failure_strips_description(self):
        from modules.ai.brain.commerce import merchant_knowledge_fact_scope as scope_mod
        from modules.ai.brain.commerce.merchant_policy_intents import (
            build_merchant_policy_decision,
        )

        dec = build_merchant_policy_decision(
            message="وش قصة المتجر؟",
            facts=_facts(store_story_status="UNKNOWN"),
        )
        assert dec is not None
        with patch.object(
            scope_mod,
            "scope_merchant_profiles_for_knowledge_turn",
            side_effect=RuntimeError("simulated_story_scope_failure"),
        ):
            mp, tp = scope_mod.safe_scope_merchant_profiles_for_knowledge_turn(
                {"description": "وصف متجر تجريبي عام", "name": "متجر"},
                {"description": "وصف متجر تجريبي عام"},
                dec.args,
            )
        assert "description" not in mp
        assert "description" not in tp
        assert mp.get("name") == "متجر"

    def test_response_goal_deterministic_beats_checkout_without_helper(self):
        """Authoritative args read — no helper fallthrough to checkout."""
        from modules.ai.brain.pipeline import _compose_base_response_goal
        from modules.ai.brain.types import Decision, SuggestionSnapshot
        from modules.ai.brain.decision.actions import ACTION_LLM_REPLY

        dec = Decision(
            action=ACTION_LLM_REPLY,
            args={
                "topic": "merchant_knowledge_shipping_policy",
                "policy_surface": "merchant_knowledge_section",
                "knowledge_kind": "shipping_policy",
                "merchant_policy_status": "UNKNOWN",
                "response_goal": (
                    "No authoritative shipping_policy document is confirmed. "
                    "Do NOT invent fees or conditions."
                ),
            },
            reason="test",
        )
        goal = _compose_base_response_goal(
            dec,
            SuggestionSnapshot(),
            checkout_facts={
                "next_goal": "confirm_customer_and_shipping_details_once",
            },
        )
        assert "Do NOT invent" in goal
        assert "confirm_customer_and_shipping_details_once" not in goal

    def test_pack_b_control_not_scoped(self):
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            apply_conservative_knowledge_fact_scope_failsafe,
            safe_apply_knowledge_turn_fact_scope,
        )

        facts = {
            "shipping_methods": ["Dev Company"],
            "merchant_capability_answer": {
                "question_kind": "shipping_companies",
                "shipping_companies": ["Dev Company"],
            },
        }
        args = {"topic": "ask_shipping", "topic_hint": "shipping_companies"}
        assert safe_apply_knowledge_turn_fact_scope(facts, args)["shipping_methods"] == [
            "Dev Company"
        ]
        assert apply_conservative_knowledge_fact_scope_failsafe(facts, args)[
            "shipping_methods"
        ] == ["Dev Company"]

    def test_pack_a2_about_control_keeps_description(self):
        from modules.ai.brain.commerce.merchant_knowledge_fact_scope import (
            safe_scope_merchant_profiles_for_knowledge_turn,
        )

        mp, tp = safe_scope_merchant_profiles_for_knowledge_turn(
            {"description": "وصف متجر تجريبي عام"},
            {"description": "وصف متجر تجريبي عام"},
            {"topic": "store_about"},
        )
        assert mp["description"] == "وصف متجر تجريبي عام"
        assert tp["description"] == "وصف متجر تجريبي عام"


class TestUnknownTruthGuard:
    def test_scrubs_live_shipping_invention(self):
        from modules.ai.brain.postprocess.merchant_knowledge_unknown_truth_guard import (
            apply_merchant_knowledge_unknown_truth_guard,
            detect_unsupported_specificity_kinds,
        )

        invented = (
            "الشحن متوفر للمدن والمناطق المدعومة عبر شركات الشحن المتاحة. "
            "تكلفة الشحن تعتمد على المدينة، وتظهر قبل إتمام الطلب"
        )
        kinds = detect_unsupported_specificity_kinds(invented)
        assert "coverage_or_availability_assertion" in kinds
        assert "fee_or_cost_rule" in kinds
        result = apply_merchant_knowledge_unknown_truth_guard(
            invented,
            decision_args={
                "topic": "merchant_knowledge_shipping_policy",
                "policy_surface": "merchant_knowledge_section",
                "merchant_policy_status": "UNKNOWN",
                "retrieval_count": 0,
            },
        )
        assert result.scrubbed or result.replaced_with_fallback
        assert "تكلفة الشحن" not in result.reply
        assert "متوفر للمدن" not in result.reply

    def test_scrubs_fabricated_store_story(self):
        from modules.ai.brain.postprocess.merchant_knowledge_unknown_truth_guard import (
            apply_merchant_knowledge_unknown_truth_guard,
        )

        invented = (
            "متجر تجريبي هو منصة متخصصة في تقديم مجموعة متنوعة من المنتجات. "
            "نحن هنا لتلبية احتياجات العملاء وتقديم تجربة تسوق مميزة."
        )
        result = apply_merchant_knowledge_unknown_truth_guard(
            invented,
            decision_args={
                "topic": "merchant_knowledge_store_story",
                "policy_surface": "merchant_knowledge_section",
                "merchant_policy_status": "UNKNOWN",
                "retrieval_count": 0,
            },
        )
        assert result.scrubbed or result.replaced_with_fallback
        assert "منصة متخصصة" not in result.reply

    def test_honest_unknown_passes(self):
        from modules.ai.brain.postprocess.merchant_knowledge_unknown_truth_guard import (
            apply_merchant_knowledge_unknown_truth_guard,
        )

        honest = "ما عندي معلومات دقيقة عن سياسة الشحن حاليًا."
        result = apply_merchant_knowledge_unknown_truth_guard(
            honest,
            decision_args={
                "topic": "merchant_knowledge_shipping_policy",
                "policy_surface": "merchant_knowledge_section",
                "merchant_policy_status": "UNKNOWN",
                "retrieval_count": 0,
            },
        )
        assert not result.scrubbed
        assert result.reply == honest

    def test_present_path_not_guarded(self):
        from modules.ai.brain.postprocess.merchant_knowledge_unknown_truth_guard import (
            apply_merchant_knowledge_unknown_truth_guard,
        )

        body_grounded = "يمكن الاسترجاع خلال 7 أيام وفق شروط المتجر التجريبي العام."
        result = apply_merchant_knowledge_unknown_truth_guard(
            body_grounded,
            decision_args={
                "topic": "merchant_knowledge_return_policy",
                "policy_surface": "merchant_knowledge_section",
                "merchant_policy_status": "KNOWN_PRESENT",
                "retrieval_count": 1,
                "doc_ref": "mks:122",
            },
        )
        assert not result.scrubbed
        assert "7 أيام" in result.reply
