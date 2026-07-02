"""Lightweight contract checks for centralized store coupon create UI."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COUPONS_PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "Coupons.tsx"
INTELLIGENCE_LIBS = REPO_ROOT / "dashboard" / "src" / "pages" / "IntelligenceLibraries.tsx"
STORE_COUPON_UTILS = REPO_ROOT / "dashboard" / "src" / "utils" / "storeCouponCreate.ts"

CODE_ALPHABET_NO_AMBIG = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
COUPON_CODE_VALIDATION_AR_MSG = "استخدم حروف إنجليزية كبيرة وأرقام فقط، بدون مسافات."


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


def test_create_modal_code_field_and_generate_button():
    text = COUPONS_PAGE.read_text(encoding="utf-8")
    assert "كود الكوبون" in text
    assert "توليد تلقائي" in text
    assert "generateStoreCouponCode" in text
    assert "validateCouponCode" in text


def test_create_modal_expiry_ux_strings():
    text = COUPONS_PAGE.read_text(encoding="utf-8")
    assert "تاريخ ووقت الانتهاء" in text
    assert "سيصبح الكوبون غير نشط بعد هذا الوقت." in text
    assert "defaultExpiryLocalValue" in text
    assert "formatExpiryLocalAr" in text


def test_create_modal_still_posts_store_coupon():
    text = COUPONS_PAGE.read_text(encoding="utf-8")
    assert "onCreate({" in text or "await onCreate" in text
    assert "expires: expiresIso" in text or "expires:" in text


def test_store_coupon_utils_code_format_contract():
    text = STORE_COUPON_UTILS.read_text(encoding="utf-8")
    assert "export function generateStoreCouponCode" in text
    assert "export function validateCouponCode" in text
    assert "NH" in text
    assert COUPON_CODE_VALIDATION_AR_MSG in text
    assert all(c in text for c in CODE_ALPHABET_NO_AMBIG[:5])  # charset present


def test_generated_code_shape_regex_matches_examples():
    """Document expected NH{tenant?}{rand} shape without executing TS."""
    pattern = re.compile(r"^NH\d*[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4,5}$")
    for sample in ("NH33K7P9", "NH1A8Q2", "NH233M4X7", "NHK7P9"):
        assert pattern.match(sample), sample
