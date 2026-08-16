"""Feature gating for FactBoundPersonaComposer enforcement."""
from __future__ import annotations

import os
from typing import Any, Optional

from core.tenant import STORE_AI_MODE_TEST, merge_ai_defaults, resolve_store_ai_mode


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def persona_composer_allowlist_result(
    *,
    tenant_id: int,
    customer_phone: str,
    ai_settings: Optional[dict[str, Any]] = None,
) -> str:
    """Diagnostic token for persona compose gate observability.

    ``persona_composer_allowlist_tenants`` is obsolete leftover JSON. It is
    not read. A stale ``[33]`` copied into another tenant's ai_settings must
    not grant or deny compose overlay. Canonical gate:

    persona_composer_enabled + store_ai_mode=test + HA phone + enforce flag.

    This overlay only rephrases after Brain; it does not select Brain vs
    OrderFlow ownership.
    """
    _ = tenant_id  # signature kept; tenant-id lists are no longer a gate
    ai = merge_ai_defaults(dict(ai_settings or {}))
    if not ai.get("persona_composer_enabled", False):
        if os.environ.get("NAHLA_PERSONA_COMPOSER_ENFORCE_TEST_MODE", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            return "composer_disabled"
    if resolve_store_ai_mode(ai) != STORE_AI_MODE_TEST:
        return "not_test_mode"
    phone = _normalize_phone(customer_phone)
    allowlist = {
        _normalize_phone(p) for p in (ai.get("ai_test_allowed_numbers") or []) if str(p).strip()
    }
    if not phone or phone not in allowlist:
        return "phone_not_allowlisted"
    if ai.get("persona_composer_enforce_test_mode") is False:
        return "enforce_flag_off"
    return "allowed"


def is_persona_composer_enforce_enabled(
    *,
    tenant_id: int,
    customer_phone: str,
    ai_settings: Optional[dict[str, Any]] = None,
) -> bool:
    """True when Phase 2 social composer may replace outbound phrasing."""
    return (
        persona_composer_allowlist_result(
            tenant_id=tenant_id,
            customer_phone=customer_phone,
            ai_settings=ai_settings,
        )
        == "allowed"
    )
