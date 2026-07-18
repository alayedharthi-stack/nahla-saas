"""Closed JSON/value scrubber for merchant-plane tenant clone."""
from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Sequence

from scripts.operators.tenant_merchant_clone_contract import (
    FORBIDDEN_JSON_KEY_MARKERS,
    PHONE_SCRUB_PLACEHOLDER,
    SCRUBBED_JSON_KEY_REPLACEMENTS,
    TARGET_AI_MODE,
    TARGET_AI_TEST_ALLOWLIST,
)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"\+?\d{10,15}")


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def _is_forbidden_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(marker in normalized for marker in FORBIDDEN_JSON_KEY_MARKERS)


def _is_integration_email_field_key(key: str) -> bool:
    normalized = _normalize_key(key)
    if normalized == "email":
        return True
    if normalized.endswith("_email"):
        return True
    return key.endswith("Email")


def scrub_json_value(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    """Recursively scrub known secret/PII keys. Returns (scrubbed, transformations)."""
    transformations: list[str] = []

    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if _is_forbidden_key(key):
                replacement = SCRUBBED_JSON_KEY_REPLACEMENTS.get(_normalize_key(key), None)
                if replacement is not None:
                    scrubbed[key] = replacement
                    transformations.append(f"scrub_key:{child_path}")
                else:
                    return value, [f"unhandled_forbidden_key:{child_path}"]
                continue
            cleaned, child_transforms = scrub_json_value(child, path=child_path)
            if any(t.startswith("unhandled_forbidden_key:") for t in child_transforms):
                return value, child_transforms
            scrubbed[key] = cleaned
            transformations.extend(child_transforms)
        return scrubbed, transformations

    if isinstance(value, list):
        cleaned_list: list[Any] = []
        for idx, item in enumerate(value):
            cleaned, child_transforms = scrub_json_value(item, path=f"{path}[{idx}]")
            if any(t.startswith("unhandled_forbidden_key:") for t in child_transforms):
                return value, child_transforms
            cleaned_list.append(cleaned)
            transformations.extend(child_transforms)
        return cleaned_list, transformations

    if isinstance(value, str):
        if _EMAIL_RE.search(value):
            return "[scrubbed]", [f"scrub_email_literal:{path or 'root'}"]
        if _PHONE_RE.fullmatch(value.strip()):
            return PHONE_SCRUB_PLACEHOLDER, [f"scrub_phone_literal:{path or 'root'}"]
    return value, transformations


def scrub_ai_settings(ai_settings: Mapping[str, Any] | None) -> dict[str, Any]:
    base = dict(copy.deepcopy(ai_settings or {}))
    base["store_ai_mode"] = TARGET_AI_MODE
    base["ai_test_allowed_numbers"] = list(TARGET_AI_TEST_ALLOWLIST)
    base["store_ai_enabled"] = True
    scrubbed, _ = scrub_json_value(base)
    return scrubbed if isinstance(scrubbed, dict) else base


def scrub_whatsapp_settings(whatsapp_settings: Mapping[str, Any] | None) -> dict[str, Any]:
    base = dict(copy.deepcopy(whatsapp_settings or {}))
    for key in (
        "access_token",
        "verify_token",
        "phone_number",
        "phone_number_id",
        "owner_whatsapp_number",
    ):
        if key in base:
            base[key] = ""
    scrubbed, transforms = scrub_json_value(base)
    if any(t.startswith("unhandled_forbidden_key:") for t in transforms):
        raise ValueError("whatsapp_settings_unhandled_forbidden_key")
    return scrubbed if isinstance(scrubbed, dict) else base


def _scrub_integration_value(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    """Recursively scrub integration config — email-suffix keys and known secrets."""
    transformations: list[str] = []

    if isinstance(value, Mapping):
        scrubbed: dict[str, Any] = {}
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if _is_integration_email_field_key(key):
                scrubbed[key] = ""
                transformations.append(f"scrub_integration_email_key:{child_path}")
                continue
            normalized = _normalize_key(key)
            if any(
                marker in normalized
                for marker in ("token", "secret", "password", "oauth", "api_key")
            ):
                scrubbed[key] = ""
                transformations.append(f"scrub_integration_secret_key:{child_path}")
                continue
            if _is_forbidden_key(key):
                replacement = SCRUBBED_JSON_KEY_REPLACEMENTS.get(normalized)
                if replacement is not None:
                    scrubbed[key] = replacement
                    transformations.append(f"scrub_integration_key:{child_path}")
                    continue
                return value, [f"unhandled_forbidden_key:{child_path}"]
            cleaned, child_transforms = _scrub_integration_value(child, path=child_path)
            if any(t.startswith("unhandled_forbidden_key:") for t in child_transforms):
                return value, child_transforms
            scrubbed[key] = cleaned
            transformations.extend(child_transforms)
        return scrubbed, transformations

    if isinstance(value, list):
        cleaned_list: list[Any] = []
        for idx, item in enumerate(value):
            cleaned, child_transforms = _scrub_integration_value(
                item,
                path=f"{path}[{idx}]",
            )
            if any(t.startswith("unhandled_forbidden_key:") for t in child_transforms):
                return value, child_transforms
            cleaned_list.append(cleaned)
            transformations.extend(child_transforms)
        return cleaned_list, transformations

    if isinstance(value, str):
        if _EMAIL_RE.search(value):
            return "", [f"scrub_integration_email_literal:{path or 'root'}"]
        if _PHONE_RE.fullmatch(value.strip()):
            return PHONE_SCRUB_PLACEHOLDER, [
                f"scrub_integration_phone_literal:{path or 'root'}"
            ]
    return value, transformations


def scrub_integration_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    base = dict(copy.deepcopy(config or {}))
    scrubbed, transforms = _scrub_integration_value(base)
    if any(t.startswith("unhandled_forbidden_key:") for t in transforms):
        raise ValueError("integration_config_unhandled_forbidden_key")
    return scrubbed if isinstance(scrubbed, dict) else base


def scan_for_unhandled_forbidden_keys(value: Any, *, path: str = "") -> list[str]:
    """Fail-closed scan — returns paths of forbidden keys without scrub mapping."""
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if _is_forbidden_key(key):
                if _normalize_key(key) not in SCRUBBED_JSON_KEY_REPLACEMENTS:
                    violations.append(child_path)
            violations.extend(scan_for_unhandled_forbidden_keys(child, path=child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            violations.extend(scan_for_unhandled_forbidden_keys(item, path=f"{path}[{idx}]"))
    return violations


def scrub_row_json_columns(
    row: Mapping[str, Any],
    json_columns: Sequence[str],
    *,
    table: str,
) -> tuple[dict[str, Any], list[str]]:
    """Apply table-specific JSON scrubbers."""
    out = dict(row)
    transformations: list[str] = []

    for column in json_columns:
        if table == "tenant_settings" and column == "ai_settings":
            out[column] = scrub_ai_settings(out.get(column))
            transformations.append("transform:tenant_settings.ai_settings_safe_test_mode")
            continue
        if table == "tenant_settings" and column == "whatsapp_settings":
            out[column] = scrub_whatsapp_settings(out.get(column))
            transformations.append("transform:tenant_settings.whatsapp_settings_stripped")
            continue
        if column not in out or out[column] is None:
            continue
        if table == "integrations" and column == "config":
            out[column] = scrub_integration_config(out[column])
            transformations.append("transform:integrations.config_stripped")
            continue
        scrubbed, col_transforms = scrub_json_value(out[column])
        if any(t.startswith("unhandled_forbidden_key:") for t in col_transforms):
            raise ValueError(f"unhandled_forbidden_json:{table}.{column}")
        out[column] = scrubbed
        transformations.extend(f"{table}.{column}:{t}" for t in col_transforms)
    return out, transformations
