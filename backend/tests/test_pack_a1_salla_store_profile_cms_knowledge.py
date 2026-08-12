"""
Pack A1 profile-only — Salla store profile + source-agnostic MKS retrieval.

No Salla CMS /pages dependency. Pack B capability truth untouched.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _run(coro):
    return asyncio.run(coro)


def _fake_section(
    *,
    section_id: int,
    tenant_id: int,
    kind: str,
    body: str,
    title: str = "",
    source: str = "manual",
    is_active: bool = True,
    deleted_at: Any = None,
    priority: int = 10,
) -> MagicMock:
    row = MagicMock()
    row.id = section_id
    row.tenant_id = tenant_id
    row.kind = kind
    row.body = body
    row.title = title or kind
    row.source = source
    row.is_active = is_active
    row.deleted_at = deleted_at
    row.priority = priority
    row.updated_at = None
    row.metadata_json = {"content_hash": f"hash-{section_id}"}
    row.product_links = []
    row.media_links = []
    return row


def _make_sync_service(adapter, store_settings: Optional[Dict] = None):
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    svc = StoreSyncService.__new__(StoreSyncService)
    svc.tenant_id = 1
    svc._adapter = adapter
    fake_settings = MagicMock()
    fake_settings.store_settings = dict(store_settings or {})
    fake_db = MagicMock()
    fake_db.query.return_value.filter_by.return_value.first.return_value = fake_settings
    fake_db.commit = MagicMock()
    fake_db.rollback = MagicMock()
    svc.db = fake_db
    return svc, fake_settings


# ── A. Store profile ─────────────────────────────────────────────────────────


class TestStoreInfoNormalize:
    def test_proven_fields_normalized(self):
        from store_adapters.salla_adapter import SallaAdapter

        profile = SallaAdapter._normalize_store_info_profile({
            "id": 42,
            "name": "متجر تجريبي عام",
            "description": "وصف مختصر",
            "email": "shop@example.test",
            "domain": "https://example.test",
            "avatar": {"url": "https://cdn/logo.png"},
            "social": {"instagram": "https://ig/test"},
            "currency": {"code": "SAR"},
            "status": "active",
        })
        assert profile["name"] == "متجر تجريبي عام"
        assert profile["description"] == "وصف مختصر"
        assert profile["email"] == "shop@example.test"
        assert profile["domain"] == "https://example.test"
        assert profile["logo_url"] == "https://cdn/logo.png"
        assert profile["social_links"]["instagram"]
        assert profile["currency"] == "SAR"
        assert profile["store_status"] == "active"
        assert profile["source_endpoint"] == "/store/info"

    def test_missing_optional_fields_remain_absent(self):
        from store_adapters.salla_adapter import SallaAdapter

        profile = SallaAdapter._normalize_store_info_profile({
            "name": "متجر تجريبي عام",
            "description": "وصف",
        })
        assert "phone" not in profile
        assert "location" not in profile
        assert "default_branch" not in profile
        assert "working_hours" not in profile


class TestSyncStoreInfo:
    def test_profile_namespaced_under_salla_store_info(self):
        class _Adapter:
            platform = "salla"

            async def get_store_info_profile(self):
                return {
                    "ok": True,
                    "profile": {
                        "name": "متجر تجريبي عام",
                        "description": "وصف المتجر",
                        "domain": "https://example.test",
                        "logo_url": "https://cdn/logo.png",
                        "email": "shop@example.test",
                        "social_links": {"instagram": "https://ig/test"},
                        "currency": "SAR",
                        "store_status": "active",
                    },
                    "http_status": 200,
                    "error_class": None,
                    "fetched_at": "2026-08-11T00:00:00+00:00",
                }

        svc, saved = _make_sync_service(_Adapter())
        ok = _run(svc.sync_store_info())
        assert ok is True
        profile = saved.store_settings["salla_store_info"]
        assert profile["description"] == "وصف المتجر"
        assert profile["email"] == "shop@example.test"
        assert "phone" not in profile or not profile.get("phone")
        assert profile["sync_ok"] is True
        # Manual override surface remains separate
        assert "store_name" not in saved.store_settings or saved.store_settings.get("store_name") != profile["name"]

    def test_failed_store_info_preserves_prior(self):
        prior = {"name": "Old", "description": "kept", "sync_ok": True}

        class _Adapter:
            platform = "salla"

            async def get_store_info_profile(self):
                return {
                    "ok": False,
                    "profile": {},
                    "http_status": 500,
                    "error_class": "http_error",
                    "fetched_at": None,
                }

        svc, saved = _make_sync_service(
            _Adapter(),
            store_settings={"salla_store_info": prior},
        )
        ok = _run(svc.sync_store_info())
        assert ok is False
        assert saved.store_settings["salla_store_info"]["description"] == "kept"
        assert saved.store_settings["salla_store_info"]["sync_ok"] is False

    def test_manual_override_not_silently_flattened(self):
        class _Adapter:
            platform = "salla"

            async def get_store_info_profile(self):
                return {
                    "ok": True,
                    "profile": {"name": "Salla Name", "description": "from salla"},
                    "http_status": 200,
                    "error_class": None,
                    "fetched_at": "2026-08-11T00:00:00+00:00",
                }

        svc, saved = _make_sync_service(
            _Adapter(),
            store_settings={
                "store_name": "Manual Nahla Name",
                "store_description": "manual description",
            },
        )
        _run(svc.sync_store_info())
        assert saved.store_settings["store_name"] == "Manual Nahla Name"
        assert saved.store_settings["store_description"] == "manual description"
        assert saved.store_settings["salla_store_info"]["name"] == "Salla Name"


# ── B. Manual MKS retrieval (source-agnostic) ────────────────────────────────


class TestMerchantDocumentRetrieval:
    def _query_returns(self, db: MagicMock, rows: list) -> None:
        q = db.query.return_value
        q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows

    def test_manual_story_retrievable(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        story = _fake_section(
            section_id=1, tenant_id=1, kind="store_story",
            body="Story A full text", title="قصة المتجر", source="manual",
        )
        db = MagicMock()
        self._query_returns(db, [story])
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "وش قصة المتجر؟")
        assert result.matched_intent == "store_story"
        assert len(result.sections) == 1
        assert result.sections[0].provenance["source"] == "manual"

    def test_manual_return_policy_retrievable(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        ret = _fake_section(
            section_id=2, tenant_id=1, kind="return_policy",
            body="Return body 7 days merchant text", title="الاسترجاع",
        )
        db = MagicMock()
        self._query_returns(db, [ret])
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "ما سياسة الاسترجاع؟")
        assert result.matched_intent == "return_family"
        assert result.sections[0].kind == "return_policy"

    def test_manual_shipping_policy_retrievable(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        ship = _fake_section(
            section_id=3, tenant_id=1, kind="shipping_policy",
            body="Shipping policy prose for generic merchant", title="سياسة الشحن",
        )
        db = MagicMock()
        self._query_returns(db, [ship])
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "ما سياسة الشحن؟")
        assert result.matched_intent == "shipping_policy"
        assert result.sections[0].kind == "shipping_policy"

    def test_manual_faq_not_customer_retrieved_pack_a3_deferred(self):
        """Pack A3: FAQ rows are AI-visible but not customer-retrieved yet."""
        from services.merchant_document_retrieval import (
            detect_document_retrieval_intent,
            retrieve_merchant_documents,
        )

        faq = _fake_section(
            section_id=4, tenant_id=1, kind="faq",
            body="FAQ body: shipping times vary by city.", title="أسئلة شائعة",
        )
        db = MagicMock()
        self._query_returns(db, [faq])
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "أسئلة شائعة؟")
        assert detect_document_retrieval_intent("أسئلة شائعة؟") is None
        assert result.matched_intent == ""
        assert len(result.sections) == 0

    def test_does_not_require_imported_or_salla_origin(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        row = _fake_section(
            section_id=9, tenant_id=1, kind="return_policy",
            body="manual policy", title="returns", source="manual",
        )
        row.metadata_json = {}  # no origin=salla
        db = MagicMock()
        self._query_returns(db, [row])
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "ما سياسة الاسترجاع؟")
        assert len(result.sections) == 1

    def test_max_sections_and_char_cap(self):
        from services.merchant_document_retrieval import (
            HARD_CHARACTER_CAP,
            MAX_SECTIONS_PER_TURN,
            retrieve_merchant_documents,
        )

        rows = [
            _fake_section(
                section_id=i, tenant_id=1, kind="return_policy",
                body=("X" * 2000), title=f"p{i}",
            )
            for i in range(1, 5)
        ]
        db = MagicMock()
        self._query_returns(db, rows)
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "ما سياسة الاسترجاع؟")
        assert len(result.sections) <= MAX_SECTIONS_PER_TURN
        assert result.total_chars <= HARD_CHARACTER_CAP
        assert result.truncated is True

    def test_shipping_companies_skips_pack_a_retrieval(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("وش شركات الشحن عندكم؟") is None

    def test_payment_methods_skips_pack_a_retrieval(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("وش طرق الدفع عندكم؟") is None
        assert detect_document_retrieval_intent("هل عندكم دفع عند الاستلام؟") is None

    def test_structured_profile_question_skips_long_form(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("وين موقعكم؟") is None
        assert detect_document_retrieval_intent("إيميل المتجر؟") is None

    def test_dual_tenant_isolation(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        story_a = _fake_section(
            section_id=1, tenant_id=10, kind="store_story",
            body="Story Tenant A", title="A",
        )
        story_b = _fake_section(
            section_id=2, tenant_id=20, kind="store_story",
            body="Story Tenant B", title="B",
        )
        db = MagicMock()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            self._query_returns(db, [story_a])
            a = retrieve_merchant_documents(db, 10, "وش قصة المتجر؟")
            assert a.sections[0].body == "Story Tenant A"
            self._query_returns(db, [story_b])
            b = retrieve_merchant_documents(db, 20, "وش قصة المتجر؟")
            assert b.sections[0].body == "Story Tenant B"

    def test_dual_tenant_return_policy_no_cross_leak(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        ret_a = _fake_section(
            section_id=11, tenant_id=10, kind="return_policy",
            body="Return policy A only", title="A",
        )
        ret_b = _fake_section(
            section_id=22, tenant_id=20, kind="return_policy",
            body="Return policy B only", title="B",
        )
        db = MagicMock()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            self._query_returns(db, [ret_a])
            a = retrieve_merchant_documents(db, 10, "ما سياسة الاسترجاع؟")
            assert "A only" in a.sections[0].body
            assert "B only" not in a.sections[0].body
            self._query_returns(db, [ret_b])
            b = retrieve_merchant_documents(db, 20, "ما سياسة الاسترجاع؟")
            assert "B only" in b.sections[0].body


# ── C. Policy PRESENT / UNKNOWN ──────────────────────────────────────────────


class TestPolicyExistenceMap:
    def test_present_when_active_mks_exists(self):
        from services.merchant_policy_existence import build_policy_existence_map

        row = _fake_section(
            section_id=7, tenant_id=1, kind="return_policy",
            body="policy body", title="returns",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [row]
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            m = build_policy_existence_map(db, 1)
        assert m["return_policy"]["status"] == "KNOWN_PRESENT"
        assert m["return_policy"]["doc_ref"] == "mks:7"
        assert "body" not in m["return_policy"]

    def test_unknown_when_no_section(self):
        from services.merchant_policy_existence import build_policy_existence_map

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            m = build_policy_existence_map(db, 1)
        assert m["return_policy"]["status"] == "UNKNOWN"
        assert m["shipping_policy"]["status"] == "UNKNOWN"

    def test_never_known_absent(self):
        from services.merchant_policy_existence import build_policy_existence_map

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            # Even if caller passes legacy pages_sync_ok=True, ABSENT is forbidden.
            m = build_policy_existence_map(db, 1, pages_sync_ok=True)
        for kind, payload in m.items():
            assert payload["status"] != "KNOWN_ABSENT", kind


# ── D. No Salla CMS dependency ───────────────────────────────────────────────


class TestNoSallaCmsRuntimeDependency:
    def test_sync_pages_is_noop_without_calling_get_pages(self):
        called = {"get_pages": False}

        class _Adapter:
            platform = "salla"

            async def get_pages(self):
                called["get_pages"] = True
                return {"ok": True, "pages": [{"id": 1, "title": "x"}], "http_status": 200}

        svc, saved = _make_sync_service(
            _Adapter(),
            store_settings={"pages": [{"title": "prior"}]},
        )
        count = _run(svc.sync_pages())
        assert count == 0
        assert called["get_pages"] is False
        # Prior index untouched
        assert saved.store_settings["pages"] == [{"title": "prior"}]

    def test_profile_path_healthy_when_pages_404(self):
        class _Adapter:
            platform = "salla"

            async def get_pages(self):
                return {
                    "ok": False,
                    "pages": [],
                    "http_status": 404,
                    "error_class": "route_not_found",
                    "partial": False,
                }

            async def get_store_info_profile(self):
                return {
                    "ok": True,
                    "profile": {"name": "OK", "description": "alive"},
                    "http_status": 200,
                    "error_class": None,
                    "fetched_at": "2026-08-11T00:00:00+00:00",
                }

        svc, saved = _make_sync_service(_Adapter())
        assert _run(svc.sync_pages()) == 0
        assert _run(svc.sync_store_info()) is True
        assert saved.store_settings["salla_store_info"]["description"] == "alive"

    def test_retrieval_works_without_cms_import(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        row = _fake_section(
            section_id=1, tenant_id=1, kind="store_story",
            body="Merchant authored story", title="story", source="manual",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [row]
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "حدثني عن المتجر")
        assert len(result.sections) == 1


# ── Overlay: long-form excluded from always-on ───────────────────────────────


class TestLongFormExcludedFromAlwaysOnOverlay:
    def test_is_long_form_document_section(self):
        from core.knowledge import is_long_form_document_section

        row = MagicMock()
        row.kind = "return_policy"
        row.source = "manual"
        assert is_long_form_document_section(row) is True
        other = MagicMock()
        other.kind = "payment_method"
        assert is_long_form_document_section(other) is False

    def test_build_structured_facts_drops_long_form(self):
        from modules.ai.prompts.tenant_overlay import build_structured_facts_block

        long_form = _fake_section(
            section_id=1, tenant_id=1, kind="return_policy",
            body="LONG POLICY SHOULD NOT APPEAR IN ALWAYS-ON FACTS",
            title="returns",
        )
        manual = MagicMock()
        manual.id = 2
        manual.kind = "payment_method"
        manual.title = "مدى"
        manual.body = "نقبل مدى"
        manual.source = "manual"
        manual.metadata_json = {}
        manual.product_links = []
        manual.media_links = []
        manual.priority = 10
        manual.updated_at = None

        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value.order_by.return_value.all.return_value = [long_form, manual]
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ), patch(
            "core.knowledge.section_has_catalog_active_product",
            return_value=True,
        ):
            block = build_structured_facts_block(db, tenant_id=1)
        assert "LONG POLICY SHOULD NOT APPEAR" not in block
        assert "نقبل مدى" in block


# ── Kind registry remains source-independent ─────────────────────────────────


class TestKnowledgeSectionKinds:
    def test_policy_kinds_registered(self):
        from services.knowledge_section_kinds import is_valid_kind

        for kind in (
            "store_story",
            "return_policy",
            "refund_policy",
            "exchange_policy",
            "shipping_policy",
            "terms_policy",
            "privacy_policy",
            "warranty",
            "faq",
            "custom",
        ):
            assert is_valid_kind(kind), kind
