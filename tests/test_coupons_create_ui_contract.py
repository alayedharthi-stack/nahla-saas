"""Lightweight contract checks for centralized store coupon create UI."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COUPONS_PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "Coupons.tsx"
INTELLIGENCE_LIBS = REPO_ROOT / "dashboard" / "src" / "pages" / "IntelligenceLibraries.tsx"


def test_coupons_page_exposes_add_store_coupon_button():
    text = COUPONS_PAGE.read_text(encoding="utf-8")
    assert "إضافة كوبون" in text
    assert "CreateStoreCouponModal" in text
    assert "featureRealityApi.createCoupon" in text or "createCoupon" in text


def test_intelligence_manual_coupons_renamed_as_ai_templates():
    libs = INTELLIGENCE_LIBS.read_text(encoding="utf-8")
    intel = (REPO_ROOT / "dashboard" / "src" / "pages" / "Intelligence.tsx").read_text(encoding="utf-8")
    assert "قوالب كوبونات الذكاء" in intel
    assert "إضافة قالب" in libs
    assert 'to="/coupons"' in libs
