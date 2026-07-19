"""Unit tests for bilingual merchant store identity helpers."""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from core.store_identity import (  # noqa: E402
    DEFAULT_SAFE_FALLBACK_AR,
    DEFAULT_SAFE_FALLBACK_EN,
    SOURCE_MERCHANT_OVERRIDE,
    detect_store_name_language,
    external_source,
    merge_external_store_name,
    merge_merchant_store_name_updates,
    normalize_store_name,
    persist_external_store_name,
    resolve_store_name,
)
from core.tenant import DEFAULT_STORE  # noqa: E402
from routers.settings import _split_store_name_updates  # noqa: E402


def _base() -> dict:
    return dict(DEFAULT_STORE)


class TestNormalizeAndDetect:
    def test_normalize_collapses_whitespace(self) -> None:
        assert normalize_store_name("  متجر   عام  ") == "متجر عام"

    def test_detect_arabic_letters(self) -> None:
        assert detect_store_name_language("متجر عام") == "ar"

    def test_detect_latin_as_english(self) -> None:
        assert detect_store_name_language("General Store") == "en"

    def test_arabic_punctuation_does_not_make_latin_name_arabic(self) -> None:
        assert detect_store_name_language("General، Store") == "en"

    def test_mixed_text_with_arabic_letter_is_arabic(self) -> None:
        assert detect_store_name_language("General متجر") == "ar"

    def test_no_translation_between_slots(self) -> None:
        merged = merge_external_store_name(_base(), "General Store", "salla")
        assert merged["store_name_en"] == "General Store"
        assert merged["store_name_ar"] == ""


class TestExternalMerge:
    def test_external_fills_empty_language_slot(self) -> None:
        merged = merge_external_store_name(_base(), "متجر تجريبي", "salla")
        assert merged["store_name_ar"] == "متجر تجريبي"
        assert merged["store_name_ar_source"] == external_source("salla")
        assert merged["store_name"] == "متجر تجريبي"

    def test_external_updates_when_source_is_external(self) -> None:
        current = {
            **_base(),
            "store_name_en": "Old Name",
            "store_name_en_source": external_source("zid"),
        }
        merged = merge_external_store_name(current, "Updated Store", "salla")
        assert merged["store_name_en"] == "Updated Store"
        assert merged["store_name_en_source"] == external_source("salla")

    def test_external_ignores_empty_name(self) -> None:
        current = {**_base(), "store_name_ar": "قيمة"}
        merged = merge_external_store_name(current, "   ", "salla")
        assert merged["store_name_ar"] == "قيمة"


class TestMerchantOverrideProtection:
    def test_manual_override_blocks_external(self) -> None:
        current = {
            **_base(),
            "store_name_ar": "اسم يدوي",
            "store_name_ar_source": SOURCE_MERCHANT_OVERRIDE,
        }
        merged = merge_external_store_name(current, "اسم سلة", "salla")
        assert merged["store_name_ar"] == "اسم يدوي"
        assert merged["store_name_ar_source"] == SOURCE_MERCHANT_OVERRIDE

    def test_unknown_source_blocks_external(self) -> None:
        current = {
            **_base(),
            "store_name_en": "Legacy English",
            "store_name_en_source": "",
        }
        merged = merge_external_store_name(current, "Salla English", "salla")
        assert merged["store_name_en"] == "Legacy English"

    def test_blocked_external_does_not_populate_empty_legacy(self) -> None:
        current = {
            **_base(),
            "store_name_ar": "اسم يدوي",
            "store_name_ar_source": SOURCE_MERCHANT_OVERRIDE,
            "store_name": "",
            "store_name_source": "",
        }
        merged = merge_external_store_name(current, "اسم خارجي", "salla")
        assert merged["store_name"] == ""
        assert merged["store_name_source"] == ""


class TestMerchantUpdates:
    def test_explicit_merchant_edit_sets_override(self) -> None:
        merged = merge_merchant_store_name_updates(
            _base(),
            {"store_name_en": "My Store"},
        )
        assert merged["store_name_en"] == "My Store"
        assert merged["store_name_en_source"] == SOURCE_MERCHANT_OVERRIDE
        assert merged["store_name"] == "My Store"

    def test_unchanged_value_preserves_source(self) -> None:
        current = {
            **_base(),
            "store_name_ar": "متجر",
            "store_name_ar_source": external_source("salla"),
        }
        merged = merge_merchant_store_name_updates(current, {"store_name_ar": "متجر"})
        assert merged["store_name_ar_source"] == external_source("salla")

    def test_clearing_value_clears_source(self) -> None:
        current = {
            **_base(),
            "store_name_en": "Store",
            "store_name_en_source": SOURCE_MERCHANT_OVERRIDE,
        }
        merged = merge_merchant_store_name_updates(current, {"store_name_en": ""})
        assert merged["store_name_en"] == ""
        assert merged["store_name_en_source"] == ""

    def test_bilingual_change_recomputes_legacy(self) -> None:
        merged = merge_merchant_store_name_updates(
            _base(),
            {"store_name_ar": "متجر أ", "store_name_en": "Store B"},
        )
        assert merged["store_name"] == "متجر أ"

    def test_arabic_merchant_slot_owns_selected_legacy_source(self) -> None:
        current = {
            **_base(),
            "store_name_en": "External Store",
            "store_name_en_source": external_source("zid"),
        }
        merged = merge_merchant_store_name_updates(
            current,
            {"store_name_ar": "اسم يدوي"},
        )
        assert merged["store_name"] == "اسم يدوي"
        assert merged["store_name_source"] == SOURCE_MERCHANT_OVERRIDE

    def test_external_arabic_slot_keeps_selected_legacy_source(self) -> None:
        current = {
            **_base(),
            "store_name_ar": "اسم خارجي",
            "store_name_ar_source": external_source("salla"),
        }
        merged = merge_merchant_store_name_updates(
            current,
            {"store_name_en": "Manual Store"},
        )
        assert merged["store_name"] == "اسم خارجي"
        assert merged["store_name_source"] == external_source("salla")


class TestSettingsStoreNameUpdates:
    def test_bilingual_update_drops_stale_legacy_mirror(self) -> None:
        incoming, name_updates = _split_store_name_updates({
            "store_name": "Stale Legacy",
            "store_name_source": "external:salla",
            "store_name_ar": "اسم يدوي",
            "store_url": "https://shop.example",
        })
        assert name_updates == {"store_name_ar": "اسم يدوي"}
        assert "store_name" not in incoming
        assert "store_name_source" not in incoming
        assert incoming["store_url"] == "https://shop.example"

    def test_legacy_only_update_is_preserved(self) -> None:
        incoming, name_updates = _split_store_name_updates({
            "store_name": "Legacy Client Name",
        })
        assert name_updates == {}
        assert incoming["store_name"] == "Legacy Client Name"


class TestResolver:
    def test_priority_requested_language_merchant(self) -> None:
        current = {
            **_base(),
            "store_name_ar": "عربي",
            "store_name_ar_source": SOURCE_MERCHANT_OVERRIDE,
            "store_name_en": "English",
            "store_name_en_source": external_source("salla"),
        }
        assert resolve_store_name(current, "ar") == "عربي"

    def test_priority_external_when_no_merchant_override(self) -> None:
        current = {
            **_base(),
            "store_name_en": "Imported",
            "store_name_en_source": external_source("zid"),
        }
        assert resolve_store_name(current, "en") == "Imported"

    def test_fallback_other_language(self) -> None:
        current = {
            **_base(),
            "store_name_ar": "عربي فقط",
            "store_name_ar_source": external_source("salla"),
        }
        assert resolve_store_name(current, "en") == "عربي فقط"

    def test_legacy_store_name_fallback(self) -> None:
        current = {**_base(), "store_name": "Legacy Name"}
        assert resolve_store_name(current, "en") == "Legacy Name"

    def test_tenant_name_before_safe_fallback(self) -> None:
        assert resolve_store_name(_base(), "ar", tenant_name="Tenant Label") == "Tenant Label"

    def test_safe_fallback_last(self) -> None:
        assert resolve_store_name(_base(), "ar") == DEFAULT_SAFE_FALLBACK_AR
        assert resolve_store_name(_base(), "en") == DEFAULT_SAFE_FALLBACK_EN

    def test_never_uses_owner_email(self) -> None:
        current = {
            **_base(),
            "store_name": "legacy-owner@example.com",
            "owner_email": "owner@example.com",
        }
        assert resolve_store_name(current, "en", tenant_name="") == DEFAULT_SAFE_FALLBACK_EN
        resolved = resolve_store_name(
            current,
            "en",
            tenant_name="tenant-owner@example.com",
        )
        assert resolved == DEFAULT_SAFE_FALLBACK_EN
        assert "@" not in resolved


class TestPersistExternal:
    def test_persist_no_op_when_unchanged(self) -> None:
        db = MagicMock()
        settings = MagicMock()
        settings.store_settings = {
            **_base(),
            "store_name_ar": "\u0645\u062a\u062c\u0631",
            "store_name_ar_source": external_source("salla"),
            "store_name": "\u0645\u062a\u062c\u0631",
            "store_name_source": external_source("salla"),
        }
        with patch("core.store_identity.get_or_create_settings", return_value=settings):
            changed = persist_external_store_name(db, 1, "\u0645\u062a\u062c\u0631", "salla")
        assert changed is False

    def test_persist_flags_modified_on_change(self) -> None:
        db = MagicMock()
        settings = MagicMock()
        settings.store_settings = dict(_base())
        with patch("core.store_identity.get_or_create_settings", return_value=settings):
            with patch("core.store_identity.flag_modified") as flag_modified:
                changed = persist_external_store_name(db, 7, "General Store", "zid")
        assert changed is True
        assert settings.store_settings["store_name_en"] == "General Store"
        flag_modified.assert_called_once_with(settings, "store_settings")
