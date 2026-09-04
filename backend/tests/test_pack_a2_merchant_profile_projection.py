"""
Pack A2 — structured merchant profile projection + customer answerability.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _settings(store: Dict[str, Any]) -> MagicMock:
    row = MagicMock()
    row.store_settings = dict(store)
    return row


def _db_with_settings(store: Dict[str, Any]) -> MagicMock:
    db = MagicMock()
    settings = _settings(store)

    def _query(model):
        q = MagicMock()
        name = getattr(model, "__name__", str(model))
        if "TenantSettings" in name:
            q.filter_by.return_value.first.return_value = settings
            q.filter.return_value.first.return_value = settings
        else:
            q.filter_by.return_value.first.return_value = None
            q.filter.return_value.first.return_value = None
        return q

    db.query.side_effect = _query
    return db


class TestResolveMerchantProfile:
    def test_fixture_a_fields(self):
        from core.merchant_profile import resolve_merchant_profile

        db = _db_with_settings({
            "salla_store_info": {
                "name": "Store A",
                "description": "Description A",
                "email": "a@example.com",
                "domain": "https://a.example",
                "social_links": {"instagram": "https://ig/a"},
                "currency": "SAR",
                "store_status": "active",
            },
        })
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={},
        ):
            p = resolve_merchant_profile(db, 11)
        assert p.description == "Description A"
        assert p.domain == "https://a.example"
        assert p.email == "a@example.com"
        assert p.phone == ""
        assert p.field_status("phone") == "UNKNOWN"
        assert p.social_links["instagram"]

    def test_absent_phone_not_invented_from_wa_owner(self):
        from core.merchant_profile import resolve_merchant_profile

        db = _db_with_settings({
            "salla_store_info": {
                "name": "Store A",
                "email": "a@example.com",
                "domain": "https://a.example",
            },
        })
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={"contact_phone": "966500000000"},
        ):
            p = resolve_merchant_profile(db, 11)
        assert p.phone == ""
        assert "966500000000" not in (p.phone or "")

    def test_manual_override_wins_for_domain(self):
        from core.merchant_profile import resolve_merchant_profile

        db = _db_with_settings({
            "store_url": "https://manual.example",
            "salla_store_info": {"domain": "https://salla.example"},
        })
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={},
        ):
            p = resolve_merchant_profile(db, 11)
        assert p.domain == "https://manual.example"
        assert p.field_sources["domain"] == "manual_override"

    def test_salla_used_when_manual_absent(self):
        from core.merchant_profile import resolve_merchant_profile

        db = _db_with_settings({
            "salla_store_info": {"domain": "https://salla.example", "description": "Salla desc"},
        })
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={},
        ):
            p = resolve_merchant_profile(db, 11)
        assert p.domain == "https://salla.example"
        assert p.description == "Salla desc"
        assert p.field_sources["domain"] == "salla_store_info"

    def test_manual_working_hours_honored(self):
        from core.merchant_profile import resolve_merchant_profile

        db = _db_with_settings({
            "working_hours": "9-5",
            "salla_store_info": {"domain": "https://a.example"},
        })
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={},
        ):
            p = resolve_merchant_profile(db, 11)
        assert p.working_hours == "9-5"
        assert p.field_sources["working_hours"] == "manual_override"


class TestDualTenantIsolation:
    def test_domains_and_emails_isolated_shared_resolver(self):
        from core.merchant_profile import resolve_merchant_profile

        stores = {
            1: {
                "salla_store_info": {
                    "name": "A",
                    "domain": "https://a.example",
                    "email": "a@example.com",
                    "description": "Desc A",
                },
            },
            2: {
                "salla_store_info": {
                    "name": "B",
                    "domain": "https://b.example",
                    "email": "b@example.com",
                    "description": "Desc B",
                },
            },
        }

        db = MagicMock()

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", str(model))

            def _filter_by(**kwargs):
                tid = int(kwargs.get("tenant_id") or 0)
                row = MagicMock()
                row.store_settings = dict(stores.get(tid) or {})
                q2 = MagicMock()
                q2.first.return_value = row if "TenantSettings" in name else None
                return q2

            q.filter_by.side_effect = _filter_by
            q.filter.side_effect = lambda *a, **k: _filter_by(tenant_id=1)
            return q

        db.query.side_effect = _query
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={},
        ):
            a = resolve_merchant_profile(db, 1)
            b = resolve_merchant_profile(db, 2)
        assert a.domain == "https://a.example"
        assert b.domain == "https://b.example"
        assert a.email != b.email
        assert a.description == "Desc A"
        assert b.description == "Desc B"


class TestProfileIntents:
    def test_about_url_contact_classification(self):
        from modules.ai.brain.commerce.merchant_profile_intents import (
            classify_store_profile_topic,
        )

        assert classify_store_profile_topic("حدثني عن المتجر") == "store_about"
        assert classify_store_profile_topic("من أنتم؟") == "store_about"
        assert classify_store_profile_topic("وش رابط المتجر؟") == "store_info"
        assert classify_store_profile_topic("ما موقع المتجر الإلكتروني؟") == "store_info"
        assert classify_store_profile_topic("كيف أتواصل معكم؟") == "owner_contact"
        assert classify_store_profile_topic("وش إيميلكم؟") == "owner_contact"
        assert classify_store_profile_topic("هل عندكم حسابات تواصل؟") == "owner_contact"
        assert classify_store_profile_topic("وش عملة المتجر؟") == "store_currency"
        assert classify_store_profile_topic("هل المتجر نشط؟") == "store_status"
        # Open-now must NOT be owned by profile account-status.
        assert classify_store_profile_topic("هل المتجر شغال؟") is None
        assert classify_store_profile_topic("هل المتجر مفتوح؟") is None

    def test_contact_does_not_steal_order_number(self):
        from modules.ai.brain.commerce.merchant_profile_intents import (
            classify_store_profile_topic,
        )

        assert classify_store_profile_topic("وش رقم الطلب؟") is None
        assert classify_store_profile_topic("رقم الشحنة") is None
        assert classify_store_profile_topic("رقم الحساب") is None
        assert classify_store_profile_topic("وش رقم الجوال المسجل في طلبي؟") is None
        assert classify_store_profile_topic("جوال العميل") is None
        assert classify_store_profile_topic("وش جوالكم؟") == "owner_contact"

    def test_sanitize_strips_nested_salla(self):
        from modules.ai.brain.pipeline import _sanitize_tenant_profile_for_prompt

        out = _sanitize_tenant_profile_for_prompt(
            {
                "contact_phone": "966500000000",
                "salla_store_info": {"phone": "966511111111", "domain": "raw.example"},
            },
            {"phone": "", "phone_status": "UNKNOWN", "domain": "https://a.example"},
        )
        assert "salla_store_info" not in out
        assert out["contact_phone"] == ""
        assert out["store_url"] == "https://a.example"

    def test_build_decision_routes(self):
        from modules.ai.brain.commerce.merchant_profile_intents import (
            build_merchant_profile_decision,
        )
        from modules.ai.brain.decision.actions import ACTION_FAQ_REPLY, ACTION_LLM_REPLY

        about = build_merchant_profile_decision(
            message="حدثني عن المتجر",
            store_description="Description A",
        )
        about_missing = build_merchant_profile_decision(
            message="حدثني عن المتجر",
            store_description="",
        )
        url = build_merchant_profile_decision(message="وش رابط المتجر؟")
        contact = build_merchant_profile_decision(message="كيف أتواصل معكم؟")
        currency = build_merchant_profile_decision(message="وش عملة المتجر؟")
        status = build_merchant_profile_decision(message="هل المتجر نشط؟")
        open_now = build_merchant_profile_decision(message="هل المتجر شغال؟")

        assert about is not None and about.action == ACTION_FAQ_REPLY
        assert about.args["topic"] == "store_about"
        assert about_missing is None
        assert url is not None and url.args["topic"] == "store_info"
        assert url.action == ACTION_LLM_REPLY
        assert contact is not None and contact.args["topic"] == "owner_contact"
        assert contact.action == ACTION_LLM_REPLY
        assert currency is not None and currency.action == ACTION_LLM_REPLY
        assert currency.args["question_kind"] == "currency"
        assert status is not None and status.action == ACTION_LLM_REPLY
        assert status.args["question_kind"] == "account_status"
        assert open_now is None

    def test_decision_uses_prepared_facts_not_ctx_db(self):
        from modules.ai.brain.commerce.merchant_profile_intents import (
            build_merchant_profile_decision,
        )
        from modules.ai.brain.types import CommerceFacts

        prepared = CommerceFacts()
        prepared.store_description = "Description A"
        prepared.store_url = "https://a.example"

        class _Ctx:
            message = "حدثني عن المتجر"
            merchant_context = {"merchant_profile": {"description": "Description A"}}

        ctx = _Ctx()
        ctx.facts = prepared
        assert not hasattr(ctx, "db")
        decision = build_merchant_profile_decision(
            message=ctx.message,
            facts=ctx.facts,
            merchant_context=ctx.merchant_context,
        )
        assert decision is not None
        assert decision.args["topic"] == "store_about"


class TestFaqGrounding:
    def test_store_about_uses_description(self):
        from modules.ai.brain.compose.templates import faq_store_info

        text = faq_store_info(
            store_name="Store A",
            store_url="",
            store_description="Description A",
        )
        assert "Description A" in text
        assert "لا أعرف" not in text

    def test_store_url_uses_domain(self):
        from modules.ai.brain.compose.templates import faq_store_info

        text = faq_store_info(store_url="https://a.example")
        assert "a.example" in text

    def test_contact_no_phone_invention(self):
        from modules.ai.brain.compose.templates import faq_owner_contact

        text = faq_owner_contact(
            contact_phone="",
            contact_email="a@example.com",
            social_links={"instagram": "https://ig/a"},
            store_url="https://a.example",
        )
        assert "a@example.com" in text
        assert "ig/a" in text
        assert "966" not in text

    def test_snapshot_does_not_override_salla(self):
        from core.merchant_profile import resolve_merchant_profile

        db = _db_with_settings({
            "salla_store_info": {
                "domain": "https://salla.example",
                "description": "Salla Desc",
            },
        })
        with patch(
            "core.merchant_profile._load_snapshot_profile",
            return_value={
                "store_url": "https://legacy.example",
                "description": "Legacy Desc",
            },
        ):
            p = resolve_merchant_profile(db, 11)
        assert p.domain == "https://salla.example"
        assert p.description == "Salla Desc"
        assert p.field_sources["domain"] == "salla_store_info"


class TestPromptLeakGuards:
    def test_sanitize_tenant_profile_strips_wa_phone(self):
        from modules.ai.brain.pipeline import _sanitize_tenant_profile_for_prompt

        out = _sanitize_tenant_profile_for_prompt(
            {"contact_phone": "966500000000", "store_name": "X"},
            {"phone": "", "phone_status": "UNKNOWN", "domain": "https://a.example"},
        )
        assert out["contact_phone"] == ""
        assert out["store_url"] == "https://a.example"

    def test_build_ai_context_no_wa_phone_leak(self):
        from core.store_knowledge import build_ai_context

        db = MagicMock()
        loader = MagicMock()
        loader.store_profile.return_value = {
            "store_name": "Store A",
            "store_url": "https://a.example",
            "description": "Description A",
            "contact_phone": "966500000000",
        }
        loader.is_fresh.return_value = True
        with patch("core.store_knowledge.StoreKnowledgeLoader", return_value=loader), patch(
            "core.merchant_profile.resolve_merchant_profile",
        ) as resolve:
            from core.merchant_profile import ResolvedMerchantProfile

            resolve.return_value = ResolvedMerchantProfile(
                tenant_id=1,
                name="Store A",
                description="Description A",
                domain="https://a.example",
                email="a@example.com",
                phone="",
            )
            text = build_ai_context(db, 1, include_sections=["store_profile"])
        assert "966500000000" not in text
        assert "Description A" in text
        assert "a.example" in text


class TestStoreUrlResolverUsesProfile:
    def test_resolver_prefers_salla_domain(self):
        from core.merchant_profile import ResolvedMerchantProfile
        from modules.ai.brain.commerce import store_url_resolver as sur

        db = MagicMock()
        profile = ResolvedMerchantProfile(
            tenant_id=1,
            domain="https://a.example",
            field_sources={"domain": "salla_store_info"},
        )
        with patch(
            "core.merchant_profile.resolve_merchant_profile",
            return_value=profile,
        ):
            res = sur.resolve_store_url(db, 1)
        assert res.found is True
        assert "a.example" in res.url
        assert "merchant_profile" in res.source


class TestTrustedProfileDomain:
    def test_merchant_profile_domain_registered(self):
        from modules.ai.brain.truth_surface.contract import TrustedDomain
        from modules.ai.brain.truth_surface.layer2.domain_registry import (
            get_domain_definition,
        )

        assert TrustedDomain.MERCHANT_PROFILE.value == "merchant_profile"
        d = get_domain_definition(TrustedDomain.MERCHANT_PROFILE)
        assert d.loader_id.endswith("_load_merchant_profile_facts")


class TestCommerceFactsOverlay:
    def test_apply_profile_clears_wa_phone(self):
        from core.merchant_profile import (
            ResolvedMerchantProfile,
            apply_resolved_profile_to_commerce_facts,
        )

        facts = MagicMock()
        facts.store_contact_phone = "966500000000"
        profile = ResolvedMerchantProfile(
            tenant_id=1,
            description="Desc",
            domain="https://a.example",
            email="a@example.com",
            phone="",
            social_links={"instagram": "https://ig/a"},
            currency="SAR",
            status="active",
        )
        apply_resolved_profile_to_commerce_facts(facts, profile)
        assert facts.store_url == "https://a.example"
        assert facts.store_description == "Desc"
        assert facts.store_contact_email == "a@example.com"
        assert facts.store_contact_phone == ""
