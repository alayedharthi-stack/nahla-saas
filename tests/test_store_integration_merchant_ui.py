"""
Merchant-facing Salla integration UI guards (no credential fields in /store-integration).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "StoreIntegration.tsx"
UTIL_PAGE = REPO_ROOT / "dashboard" / "src" / "utils" / "sallaMerchantIntegration.ts"
EMBEDDED_PAGE = REPO_ROOT / "dashboard" / "src" / "pages" / "SallaEntryScreen.tsx"

FORBIDDEN_IN_STORE_PAGE = [
    "Webhook Secret",
    "مفتاح API",
    "Account Token",
    "Reveal token once",
    "partners.salla.sa",
    "refresh_token",
    "access token",
    "معرّف المتجر (Store ID)",
    "showApiKey",
    "api_key_hint",
]

REQUIRED_MERCHANT_COPY = [
    "إكمال ربط سلة",
    "إعادة ربط سلة",
    "مطلوب لمزامنة الكوبونات والطلبات والمنتجات مع سلة",
    "انتهت صلاحية ربط سلة",
    "مزامنة الكوبونات مع سلة تتطلب إكمال ربط سلة",
]


def test_store_integration_hides_merchant_credential_fields():
    text = STORE_PAGE.read_text(encoding="utf-8")
    for marker in FORBIDDEN_IN_STORE_PAGE:
        assert marker not in text, f"merchant page must not contain {marker!r}"


def test_store_integration_shows_merchant_cta_copy():
    text = STORE_PAGE.read_text(encoding="utf-8")
    assert "data-merchant-salla-integration" in text
    assert "SALLA_MERCHANT_COPY.completeLinkCta" in text or "إكمال ربط سلة" in text


def test_salla_merchant_util_includes_required_copy():
    text = UTIL_PAGE.read_text(encoding="utf-8")
    for snippet in REQUIRED_MERCHANT_COPY:
        assert snippet in text


def test_salla_entry_embedded_uses_merchant_friendly_cta():
    text = EMBEDDED_PAGE.read_text(encoding="utf-8")
    assert "needsReauth" in text
    assert "t.cta.reconnectStore" in text
    assert "t.status.sallaEmbedded" in text
    assert "t.status.easyMode" not in text


def test_salla_merchant_forbidden_markers_exported():
    text = UTIL_PAGE.read_text(encoding="utf-8")
    assert "SALLA_MERCHANT_FORBIDDEN_UI_MARKERS" in text
