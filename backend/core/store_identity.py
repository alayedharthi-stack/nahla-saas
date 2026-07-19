"""
core/store_identity.py
──────────────────────
Pure helpers for bilingual merchant store identity in TenantSettings.store_settings.

Ownership boundary: platform settings + external sync populate these fields;
merchant dashboard edits set merchant_override sources. AI / Trusted Context
consumers should read the same JSON contract — see docs/contracts/merchant-store-identity.md.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, Literal, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from core.tenant import DEFAULT_STORE, get_or_create_settings, merge_defaults

StoreLanguage = Literal["ar", "en"]

SOURCE_MERCHANT_OVERRIDE = "merchant_override"
SOURCE_EXTERNAL_PREFIX = "external:"

_ARABIC_RANGES = (
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
)

_BILINGUAL_VALUE_FIELDS = ("store_name_ar", "store_name_en")
_BILINGUAL_SOURCE_FIELDS = ("store_name_ar_source", "store_name_en_source")

DEFAULT_SAFE_FALLBACK_AR = "متجر"
DEFAULT_SAFE_FALLBACK_EN = "Store"


def normalize_store_name(name: Optional[str]) -> str:
    """Trim and collapse internal whitespace; empty input becomes ``\"\"``."""
    if not name:
        return ""
    collapsed = " ".join(str(name).split())
    return collapsed.strip()


def detect_store_name_language(name: str) -> StoreLanguage:
    """Any Arabic Unicode letter => ``ar``; otherwise ``en``. Never translates."""
    for char in name or "":
        codepoint = ord(char)
        if (
            any(start <= codepoint <= end for start, end in _ARABIC_RANGES)
            and unicodedata.category(char).startswith("L")
        ):
            return "ar"
    return "en"


def is_external_source(source: Optional[str]) -> bool:
    return bool(source) and str(source).startswith(SOURCE_EXTERNAL_PREFIX)


def external_source(provider: str) -> str:
    return f"{SOURCE_EXTERNAL_PREFIX}{provider}"


def _lang_field(lang: StoreLanguage) -> str:
    return f"store_name_{lang}"


def _lang_source_field(lang: StoreLanguage) -> str:
    return f"store_name_{lang}_source"


def _approved_name(current: Dict[str, Any], lang: StoreLanguage) -> str:
    return normalize_store_name(current.get(_lang_field(lang), ""))


def _approved_source(current: Dict[str, Any], lang: StoreLanguage) -> str:
    return str(current.get(_lang_source_field(lang)) or "").strip()


def _can_external_update_field(current: Dict[str, Any], lang: StoreLanguage) -> bool:
    value = _approved_name(current, lang)
    source = _approved_source(current, lang)
    if not value:
        return True
    if source == SOURCE_MERCHANT_OVERRIDE:
        return False
    if is_external_source(source):
        return True
    # Non-empty value with unknown / legacy / empty source — protect from overwrite.
    return False


def _select_legacy_store_name(current: Dict[str, Any]) -> tuple[str, str]:
    """Return the Arabic-first legacy mirror and its matching source."""
    for lang in ("ar", "en"):
        value = _approved_name(current, lang)
        if value:
            return value, _approved_source(current, lang)
    return "", ""


def merge_external_store_name(
    current: Dict[str, Any],
    name: Optional[str],
    provider: str,
) -> Dict[str, Any]:
    """
    Fill or refresh the language slot implied by ``name`` from an external provider.

    Updates only when the target slot is empty or currently owned by an external source.
    Never overwrites merchant_override or unknown-source values.
    Legacy ``store_name`` is updated only when empty or its source is external.
    """
    normalized = normalize_store_name(name)
    if not normalized:
        return dict(current)

    result = dict(current)
    lang = detect_store_name_language(normalized)
    ext_source = external_source(provider)

    slot_updated = _can_external_update_field(result, lang)
    if slot_updated:
        result[_lang_field(lang)] = normalized
        result[_lang_source_field(lang)] = ext_source

    legacy = normalize_store_name(result.get("store_name", ""))
    legacy_source = str(result.get("store_name_source") or "").strip()
    if slot_updated and (not legacy or is_external_source(legacy_source)):
        selected_value, selected_source = _select_legacy_store_name(result)
        result["store_name"] = selected_value
        result["store_name_source"] = selected_source

    return result


def merge_merchant_store_name_updates(
    current: Dict[str, Any],
    updates: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Apply explicit merchant dashboard edits for bilingual store names.

    Only keys present in ``updates`` are considered. Unchanged values keep their
    source. Changed non-empty values become merchant_override. Clearing a value
    clears its source. Legacy ``store_name`` is recomputed when a bilingual field
    actually changes.
    """
    result = dict(current)
    bilingual_changed = False

    for field in _BILINGUAL_VALUE_FIELDS:
        if field not in updates:
            continue
        new_value = normalize_store_name(updates.get(field))
        old_value = normalize_store_name(result.get(field, ""))
        source_field = f"{field}_source"

        if new_value == old_value:
            continue

        bilingual_changed = True
        result[field] = new_value
        if new_value:
            result[source_field] = SOURCE_MERCHANT_OVERRIDE
        else:
            result[source_field] = ""

    if bilingual_changed:
        selected_value, selected_source = _select_legacy_store_name(result)
        result["store_name"] = selected_value
        result["store_name_source"] = selected_source

    return result


def _resolver_value_for_lang(
    current: Dict[str, Any],
    lang: StoreLanguage,
    *,
    merchant_only: bool,
) -> str:
    value = _approved_name(current, lang)
    if not value:
        return ""
    source = _approved_source(current, lang)
    if merchant_only:
        return value if source == SOURCE_MERCHANT_OVERRIDE else ""
    if source == SOURCE_MERCHANT_OVERRIDE:
        return ""
    # external or legacy/unset source — treat as available for display
    return value


def _is_disallowed_identity_fallback(value: str) -> bool:
    """Reject owner/account email shapes from display fallbacks."""
    text = normalize_store_name(value)
    return bool(text) and "@" in text


def resolve_store_name(
    current: Dict[str, Any],
    language: StoreLanguage,
    *,
    tenant_name: str = "",
    safe_fallback: Optional[str] = None,
) -> str:
    """
    Resolve a display store name for ``language`` without using owner/email.

    Priority:
      1. Requested-language merchant_override value
      2. Requested-language external / available value
      3. Other approved bilingual language
      4. Legacy store_name
      5. tenant_name
      6. safe_fallback (language-aware default when omitted)
    """
    if safe_fallback is None:
        safe_fallback = DEFAULT_SAFE_FALLBACK_AR if language == "ar" else DEFAULT_SAFE_FALLBACK_EN

    other: StoreLanguage = "en" if language == "ar" else "ar"
    tenant_label = normalize_store_name(tenant_name)
    if _is_disallowed_identity_fallback(tenant_label):
        tenant_label = ""

    for candidate in (
        _resolver_value_for_lang(current, language, merchant_only=True),
        _resolver_value_for_lang(current, language, merchant_only=False),
        _approved_name(current, other),
        normalize_store_name(current.get("store_name", "")),
        tenant_label,
        normalize_store_name(safe_fallback),
    ):
        if candidate and not _is_disallowed_identity_fallback(candidate):
            return candidate
    return normalize_store_name(safe_fallback) or (
        DEFAULT_SAFE_FALLBACK_AR if language == "ar" else DEFAULT_SAFE_FALLBACK_EN
    )


def _store_identity_snapshot(current: Dict[str, Any]) -> Dict[str, str]:
    keys = (
        "store_name",
        "store_name_source",
        *_BILINGUAL_VALUE_FIELDS,
        *_BILINGUAL_SOURCE_FIELDS,
    )
    return {k: str(current.get(k) or "") for k in keys}


def persist_external_store_name(
    db: Session,
    tenant_id: int,
    name: Optional[str],
    provider: str,
) -> bool:
    """
    Merge an external provider store name into TenantSettings.store_settings.

    Returns True when JSON was modified.
    """
    if not tenant_id:
        return False

    normalized = normalize_store_name(name)
    if not normalized:
        return False

    settings = get_or_create_settings(db, tenant_id)
    current = merge_defaults(settings.store_settings, DEFAULT_STORE)
    merged = merge_external_store_name(current, normalized, provider)

    before = _store_identity_snapshot(current)
    after = _store_identity_snapshot(merged)
    if before == after:
        return False

    settings.store_settings = merged
    flag_modified(settings, "store_settings")
    return True
