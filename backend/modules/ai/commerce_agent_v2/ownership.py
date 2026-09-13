"""Fail-closed ownership gates for the tenant-scoped V2 WhatsApp canary."""
from __future__ import annotations

from core.config import (
    COMMERCE_AGENT_V2_ENABLED,
    COMMERCE_AGENT_V2_KILL_SWITCH,
    COMMERCE_AGENT_V2_OUTBOUND_TENANT_IDS,
    COMMERCE_AGENT_V2_SHADOW_ONLY,
    COMMERCE_AGENT_V2_TENANT_IDS,
)


def outbound_enabled_for_tenant(tenant_id: int) -> bool:
    """Return true only when every explicit outbound ownership gate passes."""
    resolved = int(tenant_id)
    return bool(
        COMMERCE_AGENT_V2_ENABLED
        and not COMMERCE_AGENT_V2_SHADOW_ONLY
        and not COMMERCE_AGENT_V2_KILL_SWITCH
        and resolved in COMMERCE_AGENT_V2_TENANT_IDS
        and resolved in COMMERCE_AGENT_V2_OUTBOUND_TENANT_IDS
    )


__all__ = ["outbound_enabled_for_tenant"]
