"""
P1-G1 — catalog visibility enforcement (AI, search, bundle, WhatsApp sender).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

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
    CatalogEligibility,
    is_catalog_active,
    is_catalog_eligible,
)
from modules.ai.brain.commerce.goal.bundle_composition import compose_regimen_bundle  # noqa: E402
from modules.ai.brain.commerce.goal.goal_retrieval import GoalKBEntry  # noqa: E402
from modules.ai.brain.commerce.goal.goal_schema import GoalKBMetadata  # noqa: E402
from modules.ai.brain.commerce.goal.goal_taxonomy import GoalTag  # noqa: E402


@dataclass
class _Product:
    id: int
    title: str
    external_id: str = "ext-1"
    meta_retailer_id: str | None = None
    sku: str = ""
    price: float = 10.0
    in_stock: bool = True
    catalog_status: str = CATALOG_STATUS_ACTIVE
    merchant_hidden_at: datetime | None = None
    source: str = "meta"
    extra_metadata: dict | None = None


class _FakeQuery:
    def __init__(self, rows: list):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def filter_by(self, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, products: list):
        self._products = products

    def query(self, model):
        return _FakeQuery(self._products)


def test_is_catalog_active_matrix() -> None:
    assert is_catalog_active(_Product(1, "ok"))
    assert not is_catalog_active(_Product(1, "hidden", catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN))
    assert not is_catalog_active(_Product(1, "removed", catalog_status=CATALOG_STATUS_REMOVED_FROM_META))
    assert not is_catalog_active(_Product(1, "oos", in_stock=False))
    assert not is_catalog_active(
        _Product(1, "mh", merchant_hidden_at=datetime.now(timezone.utc)),
    )


def test_search_products_excludes_inactive() -> None:
    from core import store_knowledge as sk  # noqa: PLC0415

    active = SimpleNamespace(
        id=1, title="Honey Active", external_id="E1", sku="", description="",
        price=10.0, in_stock=True, stock_quantity=None, extra_metadata={},
        catalog_status=CATALOG_STATUS_ACTIVE, merchant_hidden_at=None,
        has_variants=False, default_variant_id=None, variants=[],
    )
    hidden = SimpleNamespace(
        id=2, title="Honey Hidden", external_id="E2", sku="", description="",
        price=10.0, in_stock=True, stock_quantity=None, extra_metadata={},
        catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN, merchant_hidden_at=None,
        has_variants=False, default_variant_id=None, variants=[],
    )
    builder = sk.CatalogContextBuilder(None, tenant_id=1)  # type: ignore[arg-type]
    results = builder._filter_orderable([active, hidden], source="test")  # type: ignore[list-item]
    assert len(results) == 1
    assert results[0]["title"] == "Honey Active"


def test_bundle_skips_hidden_linked_product() -> None:
    meta = GoalKBMetadata.from_metadata_json(
        {
            "goal_tags": ["energy_daily"],
            "products": [{"product_id": 99, "role": "primary"}],
        }
    )
    entry = GoalKBEntry(section_id=1, title="t", body="", metadata=meta)
    hidden = _Product(
        99, "Honey", catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN,
    )
    bundle = compose_regimen_bundle(_FakeDB([hidden]), 1, GoalTag.ENERGY_DAILY.value, entry)
    assert bundle.resolved_count == 0
    assert bundle.unresolved_refs


def test_catalog_eligible_refuses_inactive_product() -> None:
    conn = SimpleNamespace(catalog_enabled=True, meta_catalog_id="CAT-1")
    ok = _Product(1, "ok", meta_retailer_id="R1")
    bad = _Product(
        2, "bad", meta_retailer_id="R2",
        catalog_status=CATALOG_STATUS_REMOVED_FROM_META,
    )
    assert is_catalog_eligible(conn, [ok]) == CatalogEligibility(ok=True, reason="ok")
    assert is_catalog_eligible(conn, [bad]).ok is False
    assert is_catalog_eligible(conn, [bad]).reason == "product_not_active"


def test_restore_merchant_hidden_reactivates() -> None:
    """Unit-level restore contract (router sets these fields)."""
    p = _Product(
        5, "Restore me",
        catalog_status=CATALOG_STATUS_MERCHANT_HIDDEN,
        merchant_hidden_at=datetime.now(timezone.utc),
    )
    p.catalog_status = CATALOG_STATUS_ACTIVE
    p.merchant_hidden_at = None
    assert is_catalog_active(p)
