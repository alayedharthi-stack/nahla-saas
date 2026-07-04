"""Feature gating for FactBoundPersonaComposer enforcement."""
from __future__ import annotations

import os
from typing import Any, Optional, Set

from core.tenant import STORE_AI_MODE_TEST, merge_ai_defaults, resolve_store_ai_mode

_DEFAULT_ALLOWLIST_TENANTS: frozenset[int] = frozenset({33})


def _normalize_phone(phone: str) -> str:
    return "".join(ch for ch in str(phone or "") if ch.isdigit())


def _allowed_tenants(ai_settings: dict[str, Any]) -> Set[int]:
    raw = ai_settings.get("persona_composer_allowlist_tenants")
    if isinstance(raw, list) and raw:
        out: set[int] = set()
        for item in raw:
            try:
                out.add(int(item))
            except (TypeError, ValueError):
                continue
        if out:
            return out
    return set(_DEFAULT_ALLOWLIST_TENANTS)


def is_persona_composer_enforce_enabled(
    *,
    tenant_id: int,
    customer_phone: str,
    ai_settings: Optional[dict[str, Any]] = None,
) -> bool:
    """True when Phase 2 social composer may replace outbound phrasing."""
    ai = merge_ai_defaults(dict(ai_settings or {}))
    if not ai.get("persona_composer_enabled", False):
        if os.environ.get("NAHLA_PERSONA_COMPOSER_ENFORCE_TEST_MODE", "").strip().lower() not in (
            "1",
            "true",
            "yes",
        ):
            return False
    if resolve_store_ai_mode(ai) != STORE_AI_MODE_TEST:
        return False
    if int(tenant_id) not in _allowed_tenants(ai):
        return False
    phone = _normalize_phone(customer_phone)
    allowlist = {
        _normalize_phone(p) for p in (ai.get("ai_test_allowed_numbers") or []) if str(p).strip()
    }
    if not phone or phone not in allowlist:
        return False
    if ai.get("persona_composer_enforce_test_mode") is False:
        return False
    return True
