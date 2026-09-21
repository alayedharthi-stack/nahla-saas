"""The availability the model is told is the synced one, not a stale column.

Tenant 1, September 2026: a dress row carried ``products.in_stock = true``
while its synced metadata said ``in_stock: false``. The reply correctly called
that dress unavailable, which is what this locks: the catalog row the read
tools project prefers the synced metadata and falls back to the column only
when the metadata is silent, so a column the sync left behind cannot make an
unavailable product look orderable.

The drift itself is a catalog-sync finding, not a runtime one: the two rows
with it were last written by an ingest that does not refresh the column
(``sync_status='blocked'``, no ``product_url``), while ``store_sync``'s own
upsert does write it. It is recorded in the pilot runbook.

Offline: the formatter runs on in-memory products, no session.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

from core.store_knowledge import CatalogContextBuilder  # noqa: E402
from models import Product  # noqa: E402


def formatted(**metadata: Any) -> dict:
    product = Product(id=22, tenant_id=1, external_id="E22", title="فستان", description="وصف",
                      price="149.0", stock_quantity=None, in_stock=True, has_variants=False,
                      extra_metadata=dict(metadata))
    builder = CatalogContextBuilder.__new__(CatalogContextBuilder)
    return builder._format(product)


def test_synced_metadata_wins_over_a_stale_available_column() -> None:
    row = formatted(in_stock=False, stock_qty=None, status="active")
    assert row["in_stock"] is False
    assert row["orderable"] is False


def test_the_column_is_used_only_when_the_metadata_is_silent() -> None:
    row = formatted(status="active")
    assert row["in_stock"] is True


def test_a_synced_quantity_wins_over_an_empty_column() -> None:
    row = formatted(in_stock=True, stock_qty=4, status="active")
    assert row["stock_qty"] == 4 and row["in_stock"] is True
