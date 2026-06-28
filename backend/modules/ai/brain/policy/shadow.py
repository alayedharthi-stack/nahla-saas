"""Shadow hook for merchant operational policy resolver (PR-B1)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..types import BrainContext
from .contracts import MerchantOperationalPolicyHint, hint_to_log_dict
from .merchant_operational_policy_resolver import resolve_merchant_operational_policy_hint

logger = logging.getLogger("nahla.brain.merchant_operational_policy")


def attach_merchant_operational_policy_hint(
    ctx: BrainContext,
    hint: MerchantOperationalPolicyHint,
) -> None:
    ctx.merchant_operational_policy_hint = hint  # type: ignore[attr-defined]


def log_merchant_operational_policy_shadow(
    *,
    tenant_id: int,
    hint: MerchantOperationalPolicyHint,
) -> None:
    payload = hint_to_log_dict(hint)
    logger.info(
        "[MERCHANT_OP_POLICY] tenant=%s response_purpose=%s allowed_actions=%s "
        "forbidden_actions=%s required_action=%s confidence=%s conflict=%s "
        "source_sections=%s missing_config_reason=%s evidence=%s",
        tenant_id,
        payload.get("response_purpose"),
        payload.get("allowed_actions"),
        payload.get("forbidden_actions"),
        payload.get("required_action"),
        payload.get("confidence"),
        payload.get("conflict"),
        payload.get("source_sections"),
        payload.get("missing_config_reason"),
        payload.get("evidence"),
    )


def prepare_merchant_operational_policy_shadow(
    ctx: BrainContext,
    *,
    db: Any = None,
) -> Optional[MerchantOperationalPolicyHint]:
    """
    Resolve merchant operational policy hint and attach to context.

    Shadow-only: never mutates decision, compose, or outbound reply.
    """
    tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
    message = str(getattr(ctx, "raw_message", "") or getattr(ctx, "message", "") or "")

    hint = resolve_merchant_operational_policy_hint(db, tenant_id, message)
    attach_merchant_operational_policy_hint(ctx, hint)
    log_merchant_operational_policy_shadow(tenant_id=tenant_id, hint=hint)
    return hint
