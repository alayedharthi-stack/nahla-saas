"""tests/test_google_merchant_dry_run.py
─────────────────────────────────────
Read-only, tenant-scoped Google Merchant dry-run collector.

* Off by default: no DB access at all when ``GOOGLE_MERCHANT_DRY_RUN`` is
  unset.
* Tenant isolation at the query: another tenant's rows never appear.
* Read-only: the preview runs under ``no_autoflush`` — a caller's pending
  and dirty state is neither flushed nor committed by it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "backend", REPO_ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from models import Base, Product, ProductVariant, Tenant  # noqa: E402
from services.google_merchant_dry_run import (  # noqa: E402
    DRY_RUN_FLAG_ENV,
    build_dry_run_preview,
    google_merchant_dry_run_enabled,
)
from services.google_merchant_products_v1 import (  # noqa: E402
    CrossTenantRowError,
    GoogleMerchantTarget,
)


def _make_db():
    engine = create_engine("sqlite:///:memory:")
    saved: list = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            if isinstance(col.type, JSONB):
                saved.append((col, col.type))
                col.type = JSON()
    Base.metadata.create_all(engine)
    for col, orig in saved:
        col.type = orig
    return sessionmaker(bind=engine)()


def _tenant(db, name):
    t = Tenant(name=name, is_active=True)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _product(db, tenant, *, external_id, title, price, meta=None):
    base_meta = {
        "status": "active", "source_status": "sale", "currency": "SAR",
        "image_url": f"https://cdn.shop.test/{external_id}.jpg",
        "product_url": f"https://shop.test/p/{external_id}",
    }
    base_meta.update(meta or {})
    p = Product(
        tenant_id=tenant.id, title=title, description="وصف", external_id=external_id,
        price=price, in_stock=True, source="salla", extra_metadata=base_meta,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _variant(db, product, **kw):
    v = ProductVariant(
        tenant_id=product.tenant_id, product_id=product.id,
        in_stock=kw.pop("in_stock", True), is_default=kw.pop("is_default", False), **kw,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


@pytest.fixture
def seeded():
    db = _make_db()
    honey_tenant = _tenant(db, "honey-store")
    generic_tenant = _tenant(db, "متجر تجريبي عام")
    honey = _product(db, honey_tenant, external_id="7001", title="عسل سدر", price="85")
    _variant(db, honey, retailer_id="7001", price="85", currency="SAR", is_default=True)
    shirt = _product(db, honey_tenant, external_id="5550", title="قميص قطني أزرق", price="120")
    for svid, size in (("901", "M"), ("902", "L")):
        _variant(db, shirt, salla_variant_id=svid, retailer_id=f"5550-{svid}",
                 price="120", currency="SAR", options={"size": size})
    hidden = _product(db, honey_tenant, external_id="7002", title="منتج مخفي", price="10",
                      meta={"source_status": "hidden", "status": "hidden"})
    _variant(db, hidden, retailer_id="7002", price="10", currency="SAR", is_default=True)
    other = _product(db, generic_tenant, external_id="8001", title="حذاء رياضي أبيض", price="199")
    _variant(db, other, retailer_id="8001", price="199", currency="SAR", is_default=True)
    return db, honey_tenant, generic_tenant


class _ExplodingSession:
    def __getattr__(self, name):
        raise AssertionError(f"db.{name} touched while dry-run flag is off")


def test_flag_off_by_default_and_db_untouched(monkeypatch):
    monkeypatch.delenv(DRY_RUN_FLAG_ENV, raising=False)
    assert google_merchant_dry_run_enabled() is False
    target = GoogleMerchantTarget(tenant_id=1, content_language="ar", feed_label="SA")
    assert build_dry_run_preview(_ExplodingSession(), target) == {"enabled": False, "tenant_id": 1}


def test_preview_is_tenant_scoped(monkeypatch, seeded):
    monkeypatch.setenv(DRY_RUN_FLAG_ENV, "true")
    db, honey_tenant, generic_tenant = seeded
    target = GoogleMerchantTarget(tenant_id=honey_tenant.id, content_language="ar", feed_label="SA")
    preview = build_dry_run_preview(db, target)

    assert preview["enabled"] is True
    assert preview["google_review"] == "not_performed_offline"
    assert sorted(o["offer_id"] for o in preview["offers"]) == ["5550-901", "5550-902", "7001"]
    assert all(o["product_input"]["feedLabel"] == "SA" for o in preview["offers"])
    assert [e["reason"] for e in preview["exclusions"]] == ["hidden_at_source"]
    assert "8001" not in {o["offer_id"] for o in preview["offers"]}
    summary = preview["summary"]
    assert summary["offers_total"] == 3
    assert summary["offers_blocked"] + summary["offers_passing_offline_checks"] == 3
    assert {g["code"] for g in preview["data_gaps"]} >= {
        "gtin_mpn_barcode_not_ingested", "vanished_variants_soft_pruned_to_out_of_stock",
    }

    other = build_dry_run_preview(
        db, GoogleMerchantTarget(tenant_id=generic_tenant.id, content_language="ar", feed_label="SA"),
    )
    assert [o["offer_id"] for o in other["offers"]] == ["8001"]


def test_blocked_offers_counted_as_blocked(monkeypatch, seeded):
    monkeypatch.setenv(DRY_RUN_FLAG_ENV, "1")
    db, honey_tenant, _ = seeded
    p = _product(db, honey_tenant, external_id="7003", title="بدون وصف", price="5")
    p.description = None
    db.commit()
    preview = build_dry_run_preview(
        db, GoogleMerchantTarget(tenant_id=honey_tenant.id, content_language="ar", feed_label="SA"),
    )
    offer = next(o for o in preview["offers"] if o["offer_id"] == "7003")
    assert offer["status"] == "blocked"
    assert preview["summary"]["offers_blocked"] >= 1
    assert preview["summary"]["issue_counts"]["blocking:description_missing"] >= 1


def test_preview_never_flushes_or_commits_caller_state(monkeypatch, seeded):
    monkeypatch.setenv(DRY_RUN_FLAG_ENV, "true")
    db, honey_tenant, _ = seeded
    existing = db.query(Product).filter(Product.external_id == "7001").one()
    pending = Product(
        tenant_id=honey_tenant.id, title="منتج معلق", description="وصف", external_id="7999",
        price="1", in_stock=True, source="salla",
        extra_metadata={"currency": "SAR", "status": "active"},
    )
    db.add(pending)
    existing.title = "عنوان غير محفوظ"
    assert existing in db.dirty and pending in db.new

    preview = build_dry_run_preview(
        db, GoogleMerchantTarget(tenant_id=honey_tenant.id, content_language="ar", feed_label="SA"),
    )

    assert "7999" not in {o["offer_id"] for o in preview["offers"]}
    assert existing in db.dirty
    assert pending in db.new
    db.rollback()
    assert db.query(Product).filter(Product.external_id == "7999").count() == 0
    assert db.query(Product).filter(Product.external_id == "7001").one().title == "عسل سدر"


def test_malformed_product_does_not_break_healthy_preview(monkeypatch, seeded):
    monkeypatch.setenv(DRY_RUN_FLAG_ENV, "true")
    db, honey_tenant, _ = seeded
    bad = _product(db, honey_tenant, external_id="7666", title="رابط تالف", price="40",
                   meta={"product_url": "https://[bad", "additional_images": ["https://[bad"]})
    _variant(db, bad, retailer_id="7666", price="40", currency="SAR", is_default=True)

    preview = build_dry_run_preview(
        db, GoogleMerchantTarget(tenant_id=honey_tenant.id, content_language="ar", feed_label="SA"),
    )
    offers = {o["offer_id"]: o for o in preview["offers"]}
    assert offers["7666"]["status"] == "blocked"
    codes = {i["code"] for i in offers["7666"]["issues"]}
    assert {"link_invalid", "additional_image_link_invalid"} <= codes
    assert offers["7001"]["status"] == "passes_offline_checks"
    assert {"5550-901", "5550-902"} <= set(offers)


def test_cross_tenant_error_is_not_suppressed(monkeypatch, seeded):
    monkeypatch.setenv(DRY_RUN_FLAG_ENV, "true")
    db, honey_tenant, _ = seeded

    def _leak(*_args, **_kwargs):
        raise CrossTenantRowError("variant_tenant_mismatch")

    monkeypatch.setattr("services.google_merchant_dry_run.map_product", _leak)
    with pytest.raises(CrossTenantRowError):
        build_dry_run_preview(
            db, GoogleMerchantTarget(tenant_id=honey_tenant.id, content_language="ar", feed_label="SA"),
        )


def test_mixed_batch_isolation_and_duplicate_identities(monkeypatch, seeded):
    """Malformed, conflicting and colliding rows are blocked; healthy rows are not."""
    monkeypatch.setenv(DRY_RUN_FLAG_ENV, "true")
    db, honey_tenant, generic_tenant = seeded
    for title in ("نسخة أولى", "نسخة ثانية"):
        dup = _product(db, honey_tenant, external_id="DUP", title=title, price="50")
        _variant(db, dup, retailer_id="DUP", price="50", currency="SAR", is_default=True)
    clash = _product(db, honey_tenant, external_id="5550-901", title="يتعارض مع متغير", price="60")
    _variant(db, clash, retailer_id="5550-901", price="60", currency="SAR", is_default=True)
    conflict = _product(db, honey_tenant, external_id="7444", title="سعر متعارض", price="120",
                        meta={"regular_price": "150", "sale_price": "0"})
    _variant(db, conflict, retailer_id="7444", price="120", currency="SAR", is_default=True)
    bad = _product(db, honey_tenant, external_id="7666", title="رابط تالف", price="40",
                   meta={"image_url": "https://shop.test:99999/x.jpg"})
    _variant(db, bad, retailer_id="7666", price="40", currency="SAR", is_default=True)
    # The same id in ANOTHER tenant is not a collision for this tenant.
    other = _product(db, generic_tenant, external_id="7001", title="منتج مستأجر آخر", price="9")
    _variant(db, other, retailer_id="7001", price="9", currency="SAR", is_default=True)

    preview = build_dry_run_preview(
        db, GoogleMerchantTarget(tenant_id=honey_tenant.id, content_language="ar", feed_label="SA"),
    )
    by_status: dict = {}
    for offer in preview["offers"]:
        by_status.setdefault(offer["status"], []).append(offer["offer_id"])
        codes = {i["code"] for i in offer["issues"]}
        if offer["offer_id"] in ("DUP", "5550-901"):
            assert "duplicate_offer_identity" in codes
        if offer["offer_id"] == "7444":
            assert "price_conflict_unresolved" in codes
            assert "price" not in offer["product_input"]["productAttributes"]
        if offer["offer_id"] == "7666":
            assert "image_link_invalid" in codes

    assert sorted(by_status["blocked"]) == ["5550-901", "5550-901", "7444", "7666", "DUP", "DUP"]
    assert sorted(by_status["passes_offline_checks"]) == ["5550-902", "7001"]
    summary = preview["summary"]
    assert summary["offers_total"] == 8
    assert summary["offers_blocked"] == 6
    assert summary["offers_passing_offline_checks"] == 2
    assert summary["duplicate_identity_offers"] == 4
    assert summary["issue_counts"]["blocking:duplicate_offer_identity"] == 4
