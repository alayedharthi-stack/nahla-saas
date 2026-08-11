"""
Pack A1 — Salla store profile + CMS pages + long-form merchant knowledge.

Covers required contracts without touching Pack B capability truth.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)


def _run(coro):
    return asyncio.run(coro)


class TestSallaCmsPageClassifier:
    def test_return_policy_ar(self):
        from services.salla_cms_page_classifier import classify_salla_cms_page

        assert classify_salla_cms_page(title="سياسة الاسترجاع", slug="return") == "return_policy"

    def test_terms_and_privacy(self):
        from services.salla_cms_page_classifier import classify_salla_cms_page

        assert classify_salla_cms_page(title="الشروط والأحكام", slug="terms") == "terms_policy"
        assert classify_salla_cms_page(title="Privacy Policy", slug="privacy") == "privacy_policy"

    def test_shipping_policy_not_bare_shipping_companies(self):
        from services.salla_cms_page_classifier import classify_salla_cms_page

        assert classify_salla_cms_page(title="سياسة الشحن", slug="shipping-policy") == "shipping_policy"
        assert classify_salla_cms_page(title="شركات الشحن", slug="shipping-companies") == "custom"

    def test_store_story(self):
        from services.salla_cms_page_classifier import classify_salla_cms_page

        assert classify_salla_cms_page(title="عن المتجر", slug="about-us") == "store_story"

    def test_uncertain_is_custom(self):
        from services.salla_cms_page_classifier import classify_salla_cms_page

        assert classify_salla_cms_page(title="صفحة عامة", slug="misc") == "custom"

    def test_return_exchange_compatible(self):
        from services.salla_cms_page_classifier import classify_salla_cms_page

        assert (
            classify_salla_cms_page(title="سياسة الاسترجاع والاستبدال", slug="returns")
            == "return_policy"
        )


class TestSallaCmsKnowledgeImport:
    def test_content_hash_stable(self):
        from services.salla_cms_knowledge_import import content_hash_for_text

        assert content_hash_for_text("abc") == content_hash_for_text("abc")
        assert content_hash_for_text("abc") != content_hash_for_text("abcd")

    def test_unchanged_hash_avoids_rewrite(self):
        from services.salla_cms_knowledge_import import (
            content_hash_for_text,
            upsert_salla_cms_page_section,
        )

        body = "سياسة الاسترجاع: مدة غير محددة في هذا الاختبار."
        digest = content_hash_for_text(body)
        existing = MagicMock()
        existing.id = 11
        existing.body = body
        existing.title = "الاسترجاع"
        existing.metadata_json = {
            "origin": "salla",
            "source_type": "cms_page",
            "salla_page_id": "99",
            "content_hash": digest,
        }
        existing.kind = "return_policy"

        db = MagicMock()
        with patch(
            "services.salla_cms_knowledge_import.find_imported_salla_page_section",
            return_value=existing,
        ):
            section, created, rewritten = upsert_salla_cms_page_section(
                db,
                tenant_id=1,
                page_id="99",
                title="الاسترجاع",
                slug="return",
                kind="return_policy",
                body=body,
            )
        assert created is False
        assert rewritten is False
        assert section is existing

    def test_hash_change_rewrites_body(self):
        from services.salla_cms_knowledge_import import upsert_salla_cms_page_section

        existing = MagicMock()
        existing.id = 11
        existing.body = "old"
        existing.title = "الاسترجاع"
        existing.metadata_json = {
            "origin": "salla",
            "source_type": "cms_page",
            "salla_page_id": "99",
            "content_hash": "deadbeef",
        }
        existing.kind = "return_policy"

        db = MagicMock()
        with patch(
            "services.salla_cms_knowledge_import.find_imported_salla_page_section",
            return_value=existing,
        ):
            _, created, rewritten = upsert_salla_cms_page_section(
                db,
                tenant_id=1,
                page_id="99",
                title="الاسترجاع",
                slug="return",
                kind="return_policy",
                body="new body text for returns",
            )
        assert created is False
        assert rewritten is True
        assert existing.body == "new body text for returns"

    def test_deactivate_missing_only_for_seen_set(self):
        from services.salla_cms_knowledge_import import deactivate_missing_salla_pages

        keep = MagicMock()
        keep.is_active = True
        keep.metadata_json = {
            "origin": "salla",
            "source_type": "cms_page",
            "salla_page_id": "1",
        }
        drop = MagicMock()
        drop.is_active = True
        drop.metadata_json = {
            "origin": "salla",
            "source_type": "cms_page",
            "salla_page_id": "2",
        }
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value.all.return_value = [keep, drop]
        n = deactivate_missing_salla_pages(db, tenant_id=7, seen_page_ids={"1"})
        assert n == 1
        assert drop.is_active is False
        assert keep.is_active is True

    def test_overlapping_external_page_id_isolated_by_tenant_lookup(self):
        from services.salla_cms_knowledge_import import find_imported_salla_page_section

        row_a = MagicMock()
        row_a.tenant_id = 10
        row_a.metadata_json = {
            "origin": "salla",
            "source_type": "cms_page",
            "salla_page_id": "same-id",
        }
        db = MagicMock()
        q = db.query.return_value
        q.filter.return_value.all.return_value = [row_a]
        found = find_imported_salla_page_section(db, tenant_id=10, page_id="same-id")
        assert found is row_a
        q.filter.return_value.all.return_value = []
        assert find_imported_salla_page_section(db, tenant_id=20, page_id="same-id") is None


class TestGetPagesOutcome:
    def test_scope_denied_is_not_empty_ok(self):
        import httpx
        from store_adapters.salla_adapter import SallaAdapter

        adapter = SallaAdapter.__new__(SallaAdapter)
        adapter._tenant_id = 1
        adapter.store_id = "x"
        adapter._log_error = MagicMock()

        req = httpx.Request("GET", "https://api.salla.dev/admin/v2/pages")
        resp = httpx.Response(403, request=req)
        err = httpx.HTTPStatusError("denied", request=req, response=resp)

        async def _fail_get(*_a, **_k):
            raise err

        adapter._get = _fail_get  # type: ignore[method-assign]
        outcome = _run(adapter.get_pages())
        assert outcome["ok"] is False
        assert outcome["pages"] == []
        assert outcome["http_status"] == 403
        assert outcome["error_class"] == "scope_denied"


def _fake_section(*, section_id: int, tenant_id: int, kind: str, body: str, title: str):
    row = MagicMock()
    row.id = section_id
    row.tenant_id = tenant_id
    row.kind = kind
    row.body = body
    row.title = title
    row.source = "imported"
    row.is_active = True
    row.deleted_at = None
    row.priority = 100
    row.updated_at = None
    row.metadata_json = {
        "origin": "salla",
        "source_type": "cms_page",
        "salla_page_id": str(section_id),
        "content_hash": "abc",
    }
    return row


class TestMerchantDocumentRetrieval:
    def _query_returns(self, db: MagicMock, rows: list) -> None:
        q = db.query.return_value
        q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = rows

    def test_story_retrieves_story_only(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        story = _fake_section(
            section_id=1, tenant_id=1, kind="store_story",
            body="Story A full text", title="قصة المتجر",
        )
        db = MagicMock()
        self._query_returns(db, [story])
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            result = retrieve_merchant_documents(db, 1, "حدثني عن المتجر")
        assert result.matched_intent == "store_story"
        assert len(result.sections) == 1
        assert result.sections[0].kind == "store_story"
        assert "Story A" in result.sections[0].body

    def test_return_retrieves_return_policy(self):
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

    def test_terms_and_privacy_isolated(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        terms = _fake_section(
            section_id=3, tenant_id=1, kind="terms_policy",
            body="Terms only body", title="الشروط",
        )
        privacy = _fake_section(
            section_id=4, tenant_id=1, kind="privacy_policy",
            body="Privacy only body", title="الخصوصية",
        )
        db = MagicMock()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            self._query_returns(db, [terms])
            t = retrieve_merchant_documents(db, 1, "ما شروط المتجر؟")
            assert t.matched_intent == "terms_policy"
            assert t.sections[0].kind == "terms_policy"
            self._query_returns(db, [privacy])
            p = retrieve_merchant_documents(db, 1, "ما سياسة الخصوصية؟")
            assert p.matched_intent == "privacy_policy"
            assert p.sections[0].kind == "privacy_policy"

    def test_max_sections_and_char_cap(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

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
            result = retrieve_merchant_documents(
                db, 1, "ما سياسة الاسترجاع؟",
                max_sections=2,
                hard_character_cap=2500,
            )
        assert len(result.sections) <= 2
        assert result.total_chars <= 2500
        assert result.truncated is True

    def test_shipping_companies_skips_pack_a_retrieval(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("وش شركات الشحن عندكم؟") is None

    def test_shipping_policy_uses_pack_a(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("ما سياسة الشحن؟") == "shipping_policy"

    def test_payment_methods_unaffected(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("وش طرق الدفع عندكم؟") is None

    def test_structured_profile_question_skips_long_form(self):
        from services.merchant_document_retrieval import detect_document_retrieval_intent

        assert detect_document_retrieval_intent("وين موقعكم؟") is None
        assert detect_document_retrieval_intent("متى تفتحون؟") is None

    def test_dual_tenant_isolation(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        story_a = _fake_section(
            section_id=1, tenant_id=101, kind="store_story",
            body="Story A", title="A",
        )
        story_b = _fake_section(
            section_id=2, tenant_id=202, kind="store_story",
            body="Story B", title="B",
        )
        db = MagicMock()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            self._query_returns(db, [story_a])
            a = retrieve_merchant_documents(db, 101, "حدثني عن المتجر")
            self._query_returns(db, [story_b])
            b = retrieve_merchant_documents(db, 202, "حدثني عن المتجر")
        assert a.sections[0].body == "Story A"
        assert b.sections[0].body == "Story B"
        assert "Story B" not in a.sections[0].body
        assert "Story A" not in b.sections[0].body

    def test_dual_tenant_return_policy(self):
        from services.merchant_document_retrieval import retrieve_merchant_documents

        ret_a = _fake_section(
            section_id=1, tenant_id=101, kind="return_policy",
            body="7 days", title="A",
        )
        ret_b = _fake_section(
            section_id=2, tenant_id=202, kind="return_policy",
            body="14 days", title="B",
        )
        db = MagicMock()
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            self._query_returns(db, [ret_a])
            a = retrieve_merchant_documents(db, 101, "ما سياسة الاسترجاع؟")
            self._query_returns(db, [ret_b])
            b = retrieve_merchant_documents(db, 202, "ما سياسة الاسترجاع؟")
        assert a.sections[0].body == "7 days"
        assert b.sections[0].body == "14 days"


class TestPolicyExistenceMap:
    def test_failed_sync_does_not_claim_absent(self):
        from services.salla_cms_knowledge_import import build_policy_existence_map

        db = MagicMock()
        q = db.query.return_value
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            q.filter.return_value.all.return_value = []
            m = build_policy_existence_map(db, 1, pages_sync_ok=False)
        assert m["return_policy"]["status"] == "UNKNOWN"

    def test_successful_empty_is_known_absent(self):
        from services.salla_cms_knowledge_import import build_policy_existence_map

        db = MagicMock()
        q = db.query.return_value
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            q.filter.return_value.all.return_value = []
            m = build_policy_existence_map(db, 1, pages_sync_ok=True)
        assert m["return_policy"]["status"] == "KNOWN_ABSENT"
        assert m["privacy_policy"]["status"] == "KNOWN_ABSENT"

    def test_present_has_doc_ref_no_prose(self):
        from services.salla_cms_knowledge_import import build_policy_existence_map

        row = _fake_section(
            section_id=55, tenant_id=1, kind="return_policy",
            body="7 days ONLY in body not in fact", title="returns",
        )
        db = MagicMock()
        q = db.query.return_value
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            q.filter.return_value.all.return_value = [row]
            m = build_policy_existence_map(db, 1, pages_sync_ok=True)
        assert m["return_policy"]["status"] == "KNOWN_PRESENT"
        assert m["return_policy"]["doc_ref"] == "mks:55"
        assert "7" not in str(m["return_policy"])

    def test_manual_section_prevents_known_absent(self):
        from services.salla_cms_knowledge_import import build_policy_existence_map

        manual = MagicMock()
        manual.id = 88
        manual.kind = "return_policy"
        manual.source = "manual"
        manual.body = "استرجاع يدوي من لوحة نحلة"
        db = MagicMock()
        q = db.query.return_value
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ):
            q.filter.return_value.all.return_value = [manual]
            m = build_policy_existence_map(db, 1, pages_sync_ok=True)
        assert m["return_policy"]["status"] == "KNOWN_PRESENT"
        assert m["return_policy"]["doc_ref"] == "mks:88"


class _OutcomeAdapter:
    platform = "salla"

    def __init__(self, outcome: Dict[str, Any]):
        self._outcome = outcome

    async def get_pages(self):
        return self._outcome


def _make_sync_service(adapter, store_settings: Optional[Dict] = None):
    from services.store_sync import StoreSyncService

    svc = StoreSyncService.__new__(StoreSyncService)
    svc.tenant_id = 1
    svc._adapter = adapter
    fake_settings = MagicMock()
    fake_settings.store_settings = dict(store_settings or {})
    fake_db = MagicMock()
    fake_db.query.return_value.filter_by.return_value.first.return_value = fake_settings
    fake_db.commit = MagicMock()
    fake_db.rollback = MagicMock()
    fake_db.add = MagicMock()
    fake_db.flush = MagicMock()
    svc.db = fake_db
    return svc, fake_settings


class TestSyncPagesPackA1:
    def test_full_body_preserved_in_section_not_index(self):
        long_body = "ب" * 800
        outcome = {
            "ok": True,
            "pages": [{
                "id": 10,
                "title": "سياسة الاسترجاع",
                "slug": "return",
                "status": "active",
                "content": f"<p>{long_body}</p>",
            }],
            "http_status": 200,
            "error_class": None,
            "partial": False,
        }
        svc, saved = _make_sync_service(_OutcomeAdapter(outcome))
        section = MagicMock()
        section.id = 77
        section.metadata_json = {"content_hash": "h1"}

        with patch(
            "services.salla_cms_knowledge_import.upsert_salla_cms_page_section",
            return_value=(section, True, True),
        ) as upsert, patch(
            "services.salla_cms_knowledge_import.deactivate_missing_salla_pages",
            return_value=0,
        ):
            count = _run(svc.sync_pages())

        assert count == 1
        kwargs = upsert.call_args.kwargs
        assert len(kwargs["body"]) == 800
        pages = saved.store_settings["pages"]
        assert "content" not in pages[0]
        assert pages[0]["kind"] == "return_policy"
        assert pages[0]["doc_ref"] == "mks:77"

    def test_failed_fetch_preserves_prior_index(self):
        prior = [{"page_id": "1", "title": "قديم", "kind": "custom"}]
        outcome = {
            "ok": False,
            "pages": [],
            "http_status": 403,
            "error_class": "scope_denied",
            "partial": False,
        }
        svc, saved = _make_sync_service(
            _OutcomeAdapter(outcome),
            store_settings={"pages": prior},
        )
        count = _run(svc.sync_pages())
        assert count == 0
        assert saved.store_settings["pages"] == prior
        assert saved.store_settings["salla_pages_sync"]["ok"] is False
        assert saved.store_settings["salla_pages_sync"]["error_class"] == "scope_denied"

    def test_successful_empty_writes_empty_index(self):
        outcome = {
            "ok": True,
            "pages": [],
            "http_status": 200,
            "error_class": None,
            "partial": False,
        }
        svc, saved = _make_sync_service(_OutcomeAdapter(outcome))
        with patch(
            "services.salla_cms_knowledge_import.deactivate_missing_salla_pages",
            return_value=0,
        ):
            count = _run(svc.sync_pages())
        assert count == 0
        assert saved.store_settings["pages"] == []
        assert saved.store_settings["salla_pages_sync"]["ok"] is True


class TestSyncStoreInfo:
    def test_profile_fields_ingested(self):
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
                        "phone": "966500000000",
                        "location": {"address": "الرياض"},
                        "default_branch": {"name": "الفرع الرئيسي"},
                        "working_hours": [{"day": "sun", "from": "09:00", "to": "17:00"}],
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
        assert profile["phone"] == "966500000000"
        assert profile["location"]["address"] == "الرياض"
        assert profile["social_links"]["instagram"]
        assert profile["working_hours"]
        assert profile["sync_ok"] is True

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


class TestImportedDocsExcludedFromAlwaysOnOverlay:
    def test_is_imported_document_section(self):
        from core.knowledge import is_imported_document_section

        row = MagicMock()
        row.source = "imported"
        row.metadata_json = {"origin": "salla", "source_type": "cms_page"}
        assert is_imported_document_section(row) is True
        manual = MagicMock()
        manual.source = "manual"
        manual.metadata_json = {}
        assert is_imported_document_section(manual) is False

    def test_build_structured_facts_drops_imported(self):
        from modules.ai.prompts.tenant_overlay import build_structured_facts_block

        imported = _fake_section(
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
        with patch(
            "core.knowledge.apply_ai_visible_kb_query_filters",
            side_effect=lambda query: query,
        ), patch(
            "core.knowledge.section_has_catalog_active_product",
            return_value=True,
        ):
            q.filter.return_value.order_by.return_value.all.return_value = [
                imported, manual,
            ]
            block = build_structured_facts_block(db, 1)
        assert "LONG POLICY SHOULD NOT APPEAR" not in block
        assert "نقبل مدى" in block
