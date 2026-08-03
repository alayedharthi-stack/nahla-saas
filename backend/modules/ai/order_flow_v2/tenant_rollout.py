"""Per-tenant OrderFlowV2 rollout via env allowlists (no schema migration).

Production rollout: add tenant IDs to ``ORDER_FLOW_V2_ENFORCE_TENANTS`` without
flipping ``ORDER_FLOW_V2_ENABLED`` for every merchant.

Rollback: remove the tenant ID from the enforce list or add it to
``ORDER_FLOW_V2_DISABLED_TENANTS`` (no migration required).
"""
from __future__ import annotations

import os

_ENV_ENFORCE = "ORDER_FLOW_V2_ENFORCE_TENANTS"
_ENV_DISABLED = "ORDER_FLOW_V2_DISABLED_TENANTS"


def parse_order_flow_v2_tenant_ids(raw: str | None) -> frozenset[int]:
    """Parse comma-separated positive tenant IDs; ignore invalid tokens."""
    if raw is None:
        return frozenset()
    text = str(raw).strip()
    if not text:
        return frozenset()
    allowed: set[int] = set()
    for part in text.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            tenant_id = int(piece)
        except ValueError:
            continue
        if tenant_id > 0:
            allowed.add(tenant_id)
    return frozenset(allowed)


def is_order_flow_v2_enforce_allowlist_configured() -> bool:
    """True when ``ORDER_FLOW_V2_ENFORCE_TENANTS`` is present in the environment."""
    return _ENV_ENFORCE in os.environ


def order_flow_v2_enforce_tenant_ids() -> frozenset[int]:
    """Tenant IDs permitted for live OrderFlowV2 when enforce allowlist is configured."""
    return parse_order_flow_v2_tenant_ids(os.environ.get(_ENV_ENFORCE))


def order_flow_v2_disabled_tenant_ids() -> frozenset[int]:
    """Tenant IDs always excluded from live and shadow OrderFlowV2."""
    return parse_order_flow_v2_tenant_ids(os.environ.get(_ENV_DISABLED))


__all__ = [
    "is_order_flow_v2_enforce_allowlist_configured",
    "order_flow_v2_disabled_tenant_ids",
    "order_flow_v2_enforce_tenant_ids",
    "parse_order_flow_v2_tenant_ids",
]
