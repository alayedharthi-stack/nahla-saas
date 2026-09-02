"""Read-only Salla provider-state facts for customer-request coupon issuance.

Used only to keep empty-path reasons honest:

- no Salla integration row → pool empty (native miss is a true empty pool)
- Salla is configured / expected but unusable → salla_unavailable
- adapter can be built → existing on-demand create contract

This helper must never call ``pick_active_salla_integration`` (that path
auto-housekeeps, disables loser rows, and commits) or ``get_adapter``
(which opens a separate session and uses the mutating picker).
"""
from __future__ import annotations

from typing import Any

SALLA_NOT_CONFIGURED = "salla_not_configured"
SALLA_CONFIGURED_BUT_UNAVAILABLE = "salla_configured_but_unavailable"
SALLA_ADAPTER_AVAILABLE = "salla_adapter_available"


def classify_salla_coupon_provider_state(db: Any, tenant_id: int) -> str:
    """Classify Salla coupon-provider usability from the caller's session.

    Read-only: SELECT + in-memory scoring. No commits, no housekeeping.
    """
    from store_integration.registry import (
        _ADAPTER_REGISTRY,
        _list_salla_integrations_for_tenant,
        _needs_reauth,
        _score_integration,
    )

    try:
        import store_adapters.salla_adapter  # noqa: F401
    except ImportError:
        pass

    rows = _list_salla_integrations_for_tenant(db, int(tenant_id))
    if not rows:
        return SALLA_NOT_CONFIGURED

    winner = sorted(rows, key=_score_integration, reverse=True)[0]
    if not winner.enabled:
        return SALLA_CONFIGURED_BUT_UNAVAILABLE
    if _needs_reauth(winner):
        return SALLA_CONFIGURED_BUT_UNAVAILABLE
    adapter_cls = _ADAPTER_REGISTRY.get(winner.provider)
    if adapter_cls is None:
        return SALLA_CONFIGURED_BUT_UNAVAILABLE
    return SALLA_ADAPTER_AVAILABLE


__all__ = [
    "SALLA_ADAPTER_AVAILABLE",
    "SALLA_CONFIGURED_BUT_UNAVAILABLE",
    "SALLA_NOT_CONFIGURED",
    "classify_salla_coupon_provider_state",
]
