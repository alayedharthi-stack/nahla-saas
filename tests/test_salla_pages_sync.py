"""
tests/test_salla_pages_sync.py
────────────────────────────────
Unit tests for Phase 2.6 — Salla Pages sync.

Covers:
  1. _strip_html() — correct tag removal, whitespace collapse, length cap.
  2. StoreSyncService.sync_pages() — normalisation, active-only filter,
     graceful fallback when the adapter is absent or raises.
  3. build_merchant_context() — "pages" key is always present in the
     returned dict (even when the store has no pages yet).
  4. UI contract — the pages list may be empty but must never be absent,
     so Intelligence.tsx never sees a KeyError / undefined.

None of these tests touch the real DB or the Salla network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    s = str(_p)
    if s not in sys.path:
        sys.path.insert(0, s)

# ── 1. _strip_html ────────────────────────────────────────────────────────────

from services.store_sync import _strip_html  # noqa: E402


class TestStripHtml:
    def test_removes_basic_tags(self) -> None:
        assert _strip_html("<p>مرحبا</p>") == "مرحبا"

    def test_collapses_whitespace(self) -> None:
        result = _strip_html("<p>  كلمة   أخرى  </p>")
        assert "  " not in result
        assert "كلمة أخرى" in result

    def test_empty_string(self) -> None:
        assert _strip_html("") == ""

    def test_none_safe(self) -> None:
        assert _strip_html(None) == ""  # type: ignore[arg-type]

    def test_caps_at_max_length(self) -> None:
        long_text = "أ" * 1000
        result = _strip_html(f"<p>{long_text}</p>", max_length=100)
        assert len(result) <= 100

    def test_strips_nested_tags(self) -> None:
        html = "<div><strong>عنوان</strong><p>فقرة</p></div>"
        result = _strip_html(html)
        assert "<" not in result
        assert "عنوان" in result
        assert "فقرة" in result

    def test_strips_script_tags(self) -> None:
        html = "<script>alert('xss')</script><p>نص آمن</p>"
        result = _strip_html(html)
        assert "alert" not in result
        assert "نص آمن" in result


# ── 2. sync_pages() normalisation ─────────────────────────────────────────────

import asyncio  # noqa: E402


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _FakeAdapter:
    """Minimal adapter stub that returns a fixed list of raw Salla pages."""

    platform = "salla"

    def __init__(self, pages: List[Dict[str, Any]]):
        self._pages = pages

    async def get_pages(self) -> List[Dict[str, Any]]:
        return self._pages


def _make_sync_service(adapter, store_settings: Dict | None = None):
    """Build a StoreSyncService with fake DB and the given adapter."""
    from services.store_sync import StoreSyncService  # noqa: PLC0415

    svc = StoreSyncService.__new__(StoreSyncService)
    svc.tenant_id = 1
    svc._adapter = adapter

    # Fake TenantSettings row
    fake_settings = MagicMock()
    fake_settings.store_settings = dict(store_settings or {})

    # Fake DB session
    fake_db = MagicMock()
    fake_db.query.return_value.filter_by.return_value.first.return_value = fake_settings
    fake_db.commit = MagicMock()
    fake_db.rollback = MagicMock()
    svc.db = fake_db

    return svc, fake_settings


class TestSyncPages:
    def test_active_pages_are_normalised(self) -> None:
        raw = [
            {
                "id": 1,
                "title": "عن المتجر",
                "slug": "about",
                "status": "active",
                "content": "<p>محتوى الصفحة</p>",
                "seo_description": "نبذة",
            }
        ]
        svc, saved = _make_sync_service(_FakeAdapter(raw))
        count = _run(svc.sync_pages())
        assert count == 1
        pages = saved.store_settings["pages"]
        assert len(pages) == 1
        assert pages[0]["title"] == "عن المتجر"
        assert "<p>" not in pages[0]["content"]

    def test_inactive_pages_are_excluded(self) -> None:
        raw = [
            {"id": 2, "title": "مسودة", "slug": "draft", "status": "disabled",
             "content": "<p>...</p>", "seo_description": ""},
            {"id": 3, "title": "سياسة الإرجاع", "slug": "return", "status": "active",
             "content": "<p>نص</p>", "seo_description": ""},
        ]
        svc, saved = _make_sync_service(_FakeAdapter(raw))
        count = _run(svc.sync_pages())
        assert count == 1
        assert saved.store_settings["pages"][0]["title"] == "سياسة الإرجاع"

    def test_empty_pages_from_salla_writes_empty_list(self) -> None:
        svc, saved = _make_sync_service(_FakeAdapter([]))
        count = _run(svc.sync_pages())
        assert count == 0
        assert saved.store_settings["pages"] == []

    def test_no_adapter_returns_zero(self) -> None:
        svc, _ = _make_sync_service(None)
        svc._adapter = None
        count = _run(svc.sync_pages())
        assert count == 0

    def test_adapter_without_get_pages_returns_zero(self) -> None:
        adapter = MagicMock(spec=[])  # no get_pages attribute
        adapter.platform = "salla"
        svc, _ = _make_sync_service(adapter)
        count = _run(svc.sync_pages())
        assert count == 0

    def test_adapter_exception_returns_zero_preserves_existing(self) -> None:
        class _FailingAdapter:
            platform = "salla"

            async def get_pages(self):
                raise RuntimeError("network error")

        existing_pages = [{"title": "صفحة يدوية", "content": "نص"}]
        svc, saved = _make_sync_service(_FailingAdapter(), store_settings={"pages": existing_pages})
        count = _run(svc.sync_pages())
        # Should return 0 (failure) but existing pages must be untouched.
        assert count == 0
        assert saved.store_settings["pages"] == existing_pages


# ── 3. build_merchant_context() always has "pages" key ───────────────────────

from core.store_knowledge import build_merchant_context  # noqa: E402


class _FakeSnap:
    last_full_sync_at = None
    store_profile = {}
    catalog_summary = {}
    shipping_summary = {}
    policy_summary = {}
    coupon_summary = {}


class TestBuildMerchantContextPages:
    def _make_db(self, pages: List[Dict] | None = None) -> MagicMock:
        fake_settings = MagicMock()
        fake_settings.ai_settings = {}
        fake_settings.store_settings = {"pages": pages or []}

        fake_snap = _FakeSnap()
        fake_snap.store_profile = {"pages": pages or []}

        db = MagicMock()

        def _query(model):
            q = MagicMock()
            name = getattr(model, "__name__", "") or getattr(model, "__tablename__", "")
            if "TenantSettings" in str(model):
                q.filter.return_value.first.return_value = fake_settings
                q.filter_by.return_value.first.return_value = fake_settings
            elif "StoreKnowledgeSnapshot" in str(model):
                q.filter_by.return_value.first.return_value = fake_snap
            else:
                q.filter_by.return_value.all.return_value = []
                q.filter_by.return_value.first.return_value = None
                q.filter.return_value.all.return_value = []
                q.filter.return_value.first.return_value = None
                q.filter_by.return_value.count.return_value = 0
                q.filter.return_value.count.return_value = 0
                q.filter_by.return_value.order_by.return_value.limit.return_value.all.return_value = []
                q.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
                q.limit.return_value.all.return_value = []
            return q

        db.query.side_effect = _query

        # Make scalar() calls return 0
        execute_result = MagicMock()
        execute_result.scalar.return_value = 0
        db.execute.return_value = execute_result

        return db

    def test_pages_key_always_present_when_empty(self) -> None:
        db = self._make_db(pages=[])
        ctx = build_merchant_context(db, tenant_id=1)
        assert "pages" in ctx
        assert ctx["pages"] == []

    def test_pages_key_contains_synced_pages(self) -> None:
        synced = [{"id": "1", "title": "عن المتجر", "slug": "about", "content": "نص"}]
        db = self._make_db(pages=synced)
        ctx = build_merchant_context(db, tenant_id=1)
        assert "pages" in ctx
        assert len(ctx["pages"]) == 1
        assert ctx["pages"][0]["title"] == "عن المتجر"
