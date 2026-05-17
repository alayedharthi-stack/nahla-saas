"""tests/test_product_studio_variants_view.py
─────────────────────────────────
Phase 4 backend contract test for the parent-grouped ProductStudio
listing: ``GET /merchant/catalog/products`` must return:

  * Per-row ``variants`` array (one entry per ProductVariant),
  * Per-row ``has_variants`` / ``default_variant_id``,
  * A tenant-wide ``variants_summary`` block with five counters
    (products / variants / variants_in_stock / whatsapp_ready /
    meta_ready / google_ready).

We exercise ``_product_diag_rows`` directly so the contract is
pinned without spinning the FastAPI app — same pattern used by
the paid-filter and product-variants-schema suites.
"""
from __future__ import annotations

# Test-harness wart (pre-existing): an earlier test in the same
# collection run may have inserted ``backend/`` at sys.path[0] AND
# cached ``backend/core/secrets.py`` under the bare name
# ``secrets``. That shadows the stdlib's ``secrets`` module, which
# Starlette later relies on (``from secrets import token_hex``).
#
# We repair it BEFORE touching sys.path:
#   1. Pop any project-shadowed ``secrets`` from sys.modules.
#   2. Force-load the stdlib version from its canonical path via
#      importlib.util so we bypass whatever sits at sys.path[0].
#   3. Pin the result in sys.modules so the rest of the collection
#      run finds the right one.
import sys
import importlib.util as _ilu
_cached = sys.modules.get("secrets")
if _cached is None or not hasattr(_cached, "token_hex"):
    sys.modules.pop("secrets", None)
    import secrets as _stdlib_secrets  # noqa: F401
    if not hasattr(_stdlib_secrets, "token_hex"):
        # Still shadowed (pytest sys.path order beat us). Resolve
        # the stdlib path directly and load that file.
        import sysconfig as _sysconfig
        from pathlib import Path as _Path
        _stdlib_dir = _sysconfig.get_paths().get("stdlib")
        if _stdlib_dir:
            _spec = _ilu.spec_from_file_location(
                "secrets", str(_Path(_stdlib_dir) / "secrets.py"),
            )
            if _spec and _spec.loader:
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                sys.modules["secrets"] = _mod

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
DATABASE_DIR = REPO_ROOT / "database"
for _p in (REPO_ROOT, BACKEND_DIR, DATABASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Product, ProductVariant, Tenant  # noqa: E402
from routers.catalog import _product_diag_rows  # noqa: E402


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    _saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                _saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in _saved:
        col.type = orig
    Session = sessionmaker(bind=engine)
    return Session(), engine


def _seed(db):
    t = Tenant(name=f"T-{id(db)}", is_active=True)
    db.add(t); db.commit(); db.refresh(t)
    # Parent A — 2 variants, both in stock, parent published.
    a = Product(tenant_id=t.id, title="فستان", external_id="ext_a",
                price="120", in_stock=True, source="salla",
                has_variants=True,
                meta_catalog_published_at=datetime.now(timezone.utc))
    db.add(a); db.commit(); db.refresh(a)
    db.add(ProductVariant(tenant_id=t.id, product_id=a.id,
                          salla_variant_id="v1", retailer_id="ext_a-v1",
                          price="120", currency="SAR",
                          in_stock=True, is_default=False,
                          options={"size": "S"}, option_summary="S"))
    db.add(ProductVariant(tenant_id=t.id, product_id=a.id,
                          salla_variant_id="v2", retailer_id="ext_a-v2",
                          price="130", currency="SAR",
                          in_stock=True, is_default=False,
                          options={"size": "M"}, option_summary="M"))
    db.commit()
    # Parent B — synthetic default only (one SKU), not published.
    b = Product(tenant_id=t.id, title="عسل", external_id="ext_b",
                price="55", in_stock=True, source="salla",
                has_variants=False)
    db.add(b); db.commit(); db.refresh(b)
    db.add(ProductVariant(tenant_id=t.id, product_id=b.id,
                          salla_variant_id=None,
                          retailer_id="ext_b",
                          price="55", currency="SAR",
                          in_stock=True, is_default=True))
    db.commit()
    return t, a, b


class TestProductStudioContract:

    def test_each_row_carries_variants_array(self):
        db, _ = _make_db()
        t, a, b = _seed(db)
        resp = _product_diag_rows(db, t.id, limit=50, offset=0)
        by_id = {r["id"]: r for r in resp["rows"]}
        assert a.id in by_id and b.id in by_id

        row_a = by_id[a.id]
        assert row_a["has_variants"] is True
        assert row_a["variants_count"] == 2
        assert row_a["sellable_variants_count"] == 2
        assert {v["salla_variant_id"] for v in row_a["variants"]} == {"v1", "v2"}
        # Per-variant retailer_id is present (sender uses this)
        rids = {v["retailer_id"] for v in row_a["variants"]}
        assert rids == {"ext_a-v1", "ext_a-v2"}

        row_b = by_id[b.id]
        assert row_b["has_variants"] is False
        # The synthetic default is in the variants array but not counted
        # in sellable_variants_count.
        assert row_b["variants_count"] == 1
        assert row_b["sellable_variants_count"] == 0
        assert row_b["variants"][0]["is_default"] is True

    def test_variants_summary_counts_are_tenant_wide(self):
        db, _ = _make_db()
        t, _a, _b = _seed(db)
        resp = _product_diag_rows(db, t.id, limit=50, offset=0)
        s = resp["variants_summary"]
        assert s["products"] == 2
        # Real variants = 2 (synthetic default excluded)
        assert s["variants"] == 2
        assert s["variants_in_stock"] == 2
        # All 3 variant rows (incl. default) have retailer ids AND
        # are in stock → all WhatsApp-ready.
        assert s["whatsapp_ready"] == 3
        # Meta-ready needs the parent's meta_catalog_published_at —
        # only parent A's variants qualify.
        assert s["meta_ready"] == 2
        # Google-ready needs price + retailer_id + in_stock → all 3.
        assert s["google_ready"] == 3

    def test_filter_doesnt_break_variants_summary(self):
        """Filters narrow ``rows`` and ``total`` but
        ``variants_summary`` stays tenant-wide (it's a stat, not a
        view)."""
        db, _ = _make_db()
        t, _a, _b = _seed(db)
        resp = _product_diag_rows(db, t.id, limit=50, offset=0,
                                  q="عسل")
        assert resp["total"] == 1, "filter narrowed rows"
        assert resp["variants_summary"]["products"] == 2, (
            "summary must remain tenant-wide regardless of filter"
        )
