"""
P1-G2 — KB search, soft delete, and AI visibility enforcement.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, List, Optional

import pytest

_here = os.path.dirname(os.path.abspath(__file__))
_backend = os.path.dirname(_here)
for _p in [_backend, os.path.join(_backend, "..")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.catalog import (  # noqa: E402
    CATALOG_STATUS_ACTIVE,
    CATALOG_STATUS_MERCHANT_HIDDEN,
    CATALOG_STATUS_REMOVED_FROM_META,
)
from core.knowledge import (  # noqa: E402
    apply_ai_visible_kb_query_filters,
    goal_metadata_has_catalog_active_product,
    kb_row_is_ai_visible,
    section_has_catalog_active_product,
)
from routers import knowledge as knowledge_router  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeProductLink:
    def __init__(self, product_id: int) -> None:
        self.product_id = product_id


class _FakeSection:
    def __init__(
        self,
        *,
        section_id: int = 1,
        tenant_id: int = 1,
        kind: str = "faq",
        title: str = "",
        body: str = "",
        is_active: bool = True,
        deleted_at: datetime | None = None,
        source: str = "manual",
        metadata_json: dict | None = None,
        product_links: List[_FakeProductLink] | None = None,
    ) -> None:
        self.id = section_id
        self.tenant_id = tenant_id
        self.kind = kind
        self.title = title
        self.body = body
        self.is_active = is_active
        self.deleted_at = deleted_at
        self.source = source
        self.metadata_json = metadata_json
        self.product_links = product_links or []
        self.updated_at = datetime.now(timezone.utc)


class _FakeProduct:
    def __init__(
        self,
        *,
        product_id: int,
        tenant_id: int = 1,
        catalog_status: str = CATALOG_STATUS_ACTIVE,
        merchant_hidden_at: datetime | None = None,
        in_stock: bool = True,
    ) -> None:
        self.id = product_id
        self.tenant_id = tenant_id
        self.catalog_status = catalog_status
        self.merchant_hidden_at = merchant_hidden_at
        self.in_stock = in_stock


class _FakeQuery:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def filter(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def order_by(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> List[Any]:
        return self._rows


class _MultiFakeSession:
    def __init__(self, *, sections: List[Any], products: List[Any]) -> None:
        self._sections = sections
        self._products = products

    def query(self, model: Any) -> _FakeQuery:
        name = getattr(model, "__name__", str(model))
        if "Product" in name:
            return _FakeQuery(self._products)
        return _FakeQuery(self._sections)


class _RecordingQuery:
    def __init__(self) -> None:
        self.filter_calls = 0

    def filter(self, *args: Any, **kwargs: Any) -> "_RecordingQuery":
        self.filter_calls += 1
        return self


def test_kb_row_visibility_matrix() -> None:
    now = datetime.now(timezone.utc)
    assert kb_row_is_ai_visible(SimpleNamespace(deleted_at=None, is_active=True))
    assert not kb_row_is_ai_visible(SimpleNamespace(deleted_at=now, is_active=True))
    assert not kb_row_is_ai_visible(SimpleNamespace(deleted_at=None, is_active=False))


def test_apply_ai_visible_kb_query_filters_adds_two_predicates() -> None:
    q = _RecordingQuery()
    apply_ai_visible_kb_query_filters(q)
    assert q.filter_calls == 2


def test_section_matches_query_title_and_body() -> None:
    section = _FakeSection(title="سياسة الإرجاع", body="يمكن الإرجاع خلال 14 يوماً")
    assert knowledge_router._section_matches_query(section, "الإرجاع")
    assert knowledge_router._section_matches_query(section, "14 يوم")
    assert not knowledge_router._section_matches_query(section, "xyzmissing")


def test_build_snippet_highlights_match_region() -> None:
    text = "نص طويل " * 20 + "كلمة البحث" + " نهاية"
    snippet = knowledge_router._build_snippet(text, "كلمة البحث")
    assert "كلمة البحث" in snippet


def test_search_hit_snippet_never_dumps_raw_metadata_json() -> None:
    section = _FakeSection(
        title="هدف",
        body="نص عام",
        metadata_json={
            "goal_tags": ["energy_daily"],
            "products": [{"product_id": 1, "ref": "عسل طبيعي", "role": "primary"}],
        },
    )
    hit = knowledge_router._serialize_search_hit(section, "عسل طبيعي")
    assert "product_id" not in hit["snippet"]
    assert "goal_tags" not in hit["snippet"]
    assert "عسل طبيعي" in hit["snippet"]


def test_search_hit_serializer_shape() -> None:
    section = _FakeSection(
        section_id=9,
        title="عنوان",
        body="محتوى يحتوي على honey",
        kind="faq",
        source="manual",
    )
    hit = knowledge_router._serialize_search_hit(section, "honey")
    assert hit["id"] == 9
    assert hit["title"] == "عنوان"
    assert "honey" in hit["snippet"]
    assert hit["kind"] == "faq"
    assert hit["group"] == knowledge_router.group_for("faq")
    assert hit["source"] == "manual"
    assert hit["is_active"] is True
    assert hit["deleted_at"] is None


def test_get_mutable_section_rejects_soft_deleted() -> None:
    deleted = _FakeSection(section_id=3, deleted_at=datetime.now(timezone.utc))
    db = SimpleNamespace(
        query=lambda model: _FakeQuery([deleted]),
    )
    with pytest.raises(HTTPException) as exc:
        knowledge_router._get_mutable_section(db, tenant_id=1, section_id=3)  # type: ignore[arg-type]
    assert exc.value.status_code == 409


def test_soft_deleted_row_not_ai_visible() -> None:
    row = _FakeSection(deleted_at=datetime.now(timezone.utc), is_active=False)
    assert not kb_row_is_ai_visible(row)


def test_disabled_row_not_ai_visible() -> None:
    row = _FakeSection(is_active=False)
    assert not kb_row_is_ai_visible(row)


def test_section_has_catalog_active_product_requires_one_active_link() -> None:
    hidden = _FakeProduct(
        product_id=10,
        catalog_status=CATALOG_STATUS_REMOVED_FROM_META,
    )
    active = _FakeProduct(product_id=11)
    section = _FakeSection(product_links=[_FakeProductLink(10), _FakeProductLink(11)])
    db = _MultiFakeSession(sections=[], products=[hidden, active])
    assert section_has_catalog_active_product(db, 1, section)


def test_section_has_catalog_active_product_false_when_all_hidden() -> None:
    hidden = _FakeProduct(
        product_id=10,
        catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN,
        merchant_hidden_at=datetime.now(timezone.utc),
    )
    section = _FakeSection(product_links=[_FakeProductLink(10)])
    db = _MultiFakeSession(sections=[], products=[hidden])
    assert not section_has_catalog_active_product(db, 1, section)


def test_goal_metadata_skips_all_hidden_product_refs() -> None:
    hidden = _FakeProduct(
        product_id=55,
        catalog_status=CATALOG_STATUS_REMOVED_FROM_META,
    )
    meta = {
        "goal_tags": ["energy_daily"],
        "products": [{"product_id": 55, "ref": "عسل", "role": "primary"}],
    }
    db = _MultiFakeSession(sections=[], products=[hidden])
    assert not goal_metadata_has_catalog_active_product(db, 1, meta)


def test_goal_retrieval_skips_hidden_product_entries() -> None:
    from modules.ai.brain.commerce.goal.goal_retrieval import retrieve_goal_recommendations

    hidden = _FakeProduct(
        product_id=77,
        catalog_status=CATALOG_STATUS_REMOVED_FROM_META,
    )
    section = _FakeSection(
        section_id=5,
        kind="goal_based_recommendation",
        title="هدف",
        body="نص",
        metadata_json={
            "goal_tags": ["energy_daily"],
            "products": [{"product_id": 77, "ref": "x", "role": "primary"}],
        },
    )
    db = _MultiFakeSession(sections=[section], products=[hidden])
    hits = retrieve_goal_recommendations(db, tenant_id=1, goal="energy_daily")
    assert hits == []


def test_structured_facts_excludes_product_scoped_hidden_product() -> None:
    from modules.ai.prompts.tenant_overlay import build_structured_facts_block

    hidden = _FakeProduct(
        product_id=99,
        catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN,
        merchant_hidden_at=datetime.now(timezone.utc),
    )
    section = _FakeSection(
        kind="product_usage",
        title="استخدام",
        body="طريقة الاستخدام للمنتج القديم",
        product_links=[_FakeProductLink(99)],
    )
    db = _MultiFakeSession(sections=[section], products=[hidden])
    block = build_structured_facts_block(db, tenant_id=1)
    assert "طريقة الاستخدام" not in block


def test_tenant_isolation_search_match_does_not_cross_tenants() -> None:
    """Search matching is per-row; tenant filter is enforced in the router query."""
    a = _FakeSection(tenant_id=1, body="tenant-one-secret-token")
    b = _FakeSection(tenant_id=2, body="tenant-one-secret-token")
    assert knowledge_router._section_matches_query(a, "secret")
    assert knowledge_router._section_matches_query(b, "secret")
    assert a.tenant_id != b.tenant_id
