"""Action executor for ACTION_CUSTOMER_COUPON_REQUEST.

Calls the merged customer_request_coupon_service. No second coupon subsystem.
Does not emit customer-facing prose.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from modules.ai.brain.commerce.customer_coupon_request_owner import (
    project_customer_request_coupon_facts,
)
from modules.ai.brain.types import ActionResult, BrainContext, Decision
from services.customer_request_coupon_canary import is_customer_coupon_canary_tenant
from services.customer_request_coupon_service import (
    REASON_IDENTITY_UNAVAILABLE,
    REASON_LIVE_ISSUANCE_DISABLED,
    issue_customer_coupon,
)

logger = logging.getLogger("nahla.brain.execution.customer_coupon_request")


class CustomerCouponRequestHandler:
    async def handle(self, decision: Decision, ctx: BrainContext) -> ActionResult:
        tenant_id = int(getattr(ctx, "tenant_id", 0) or 0)
        if not is_customer_coupon_canary_tenant(tenant_id):
            return ActionResult(
                success=False,
                error="tenant_not_allowlisted",
                data={
                    "type": "customer_coupon_request",
                    "customer_request_coupon_facts": {
                        "requested": True,
                        "issued": False,
                        "reason": REASON_LIVE_ISSUANCE_DISABLED,
                    },
                    "service_called": False,
                },
            )

        db = getattr(ctx, "_db", None) or getattr(ctx, "db", None)
        customer_id = getattr(ctx, "customer_id", None)
        if db is None or customer_id in (None, "", 0, "0"):
            facts = {
                "requested": True,
                "issued": False,
                "reason": REASON_IDENTITY_UNAVAILABLE,
                "reused_assignment": False,
            }
            return ActionResult(
                success=True,
                data={
                    "type": "customer_coupon_request",
                    "customer_request_coupon_facts": facts,
                    "service_called": False,
                },
            )

        result = await issue_customer_coupon(
            db,
            tenant_id,
            int(customer_id),
            for_channel="ai",
            allow_issuance=True,
        )
        facts = project_customer_request_coupon_facts(result)
        logger.info(
            "[CUSTOMER_COUPON_REQUEST] tenant=%s issued=%s reason=%s reused=%s",
            tenant_id,
            facts.get("issued"),
            facts.get("reason"),
            facts.get("reused_assignment"),
        )
        return ActionResult(
            success=True,
            data={
                "type": "customer_coupon_request",
                "customer_request_coupon_facts": facts,
                "service_called": True,
            },
        )


def customer_request_coupon_facts_from_result(result: ActionResult) -> Dict[str, Any]:
    raw = dict(getattr(result, "data", None) or {}).get("customer_request_coupon_facts")
    return dict(raw) if isinstance(raw, dict) else {}


__all__ = [
    "CustomerCouponRequestHandler",
    "customer_request_coupon_facts_from_result",
]
