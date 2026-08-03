"""Construct tenant-scoped CommerceToolRuntime from BrainContext."""
from __future__ import annotations

from typing import Any

from modules.ai.brain.commerce.permission_gate import permissions_for_context
from modules.ai.brain.types import BrainContext
from modules.ai.commerce.runtime import CommerceToolRuntime


def build_commerce_runtime(ctx: BrainContext, **kwargs: Any) -> CommerceToolRuntime:
    db = getattr(ctx, "_db", None)
    perms = permissions_for_context(ctx)
    return CommerceToolRuntime(
        db,
        tenant_id=ctx.tenant_id,
        customer_phone=ctx.customer_phone,
        customer_id=ctx.customer_id,
        tenant_context=ctx.tenant_context,
        permissions=perms,
        permission_source=str(getattr(ctx, "permission_source", "") or ""),
        **kwargs,
    )
