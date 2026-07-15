"""
customer_conditional_coupon_subject.py
──────────────────────────────────────
AI-owned safe subject-handle boundary for conditional-coupon facts.

No trusted upstream A1 handle contract exists in the current runtime. This
module therefore resolves nothing and fails closed until a separately owned
Platform/AI bridge publishes one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from .customer_conditional_coupon_contract import (
    CUSTOMER_SCOPE_EXTERNAL,
    CUSTOMER_SCOPE_INTERNAL,
    CUSTOMER_SCOPE_UNRESOLVED,
    IDENTITY_STATUS_AMBIGUOUS,
    IDENTITY_STATUS_RESOLVED,
    IDENTITY_STATUS_UNRESOLVED,
)


@dataclass(frozen=True)
class ConditionalCouponSubjectHandle:
    """Safe per-subject handle passed from upstream state — no PII."""

    subject_kind: str
    tenant_id: int
    identity_namespace: str
    handle_source: str
    customer_id: Optional[int] = None
    external_customer_profile_id: Optional[UUID] = None


@dataclass(frozen=True)
class SubjectResolutionResult:
    status: str
    handle: Optional[ConditionalCouponSubjectHandle] = None
    reason_code: Optional[str] = None


def resolve_conditional_coupon_subject_handle(
    *,
    tenant_id: int,
    conversation: Any = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
) -> SubjectResolutionResult:
    """
    Return unresolved until an authoritative upstream handle is published.

    ``Conversation.customer_id`` is a general customer relation, not a
    provenance-bearing A1 scope contract. Inbound metadata has no established
    trusted writer. Neither may authorize an A1 history query here. Phone,
    message text, and ``build_order_context`` are also intentionally excluded.
    """
    if not tenant_id:
        return SubjectResolutionResult(
            status=IDENTITY_STATUS_UNRESOLVED,
            reason_code="missing_tenant",
        )

    _ = conversation, inbound_metadata
    return SubjectResolutionResult(
        status=IDENTITY_STATUS_UNRESOLVED,
        reason_code="authoritative_subject_handle_unavailable",
    )


def customer_scope_for_handle(handle: ConditionalCouponSubjectHandle) -> str:
    if handle.subject_kind == "external_customer_profile":
        return CUSTOMER_SCOPE_EXTERNAL
    if handle.subject_kind == "nahla_internal_customer":
        return CUSTOMER_SCOPE_INTERNAL
    return CUSTOMER_SCOPE_UNRESOLVED


__all__ = [
    "ConditionalCouponSubjectHandle",
    "SubjectResolutionResult",
    "customer_scope_for_handle",
    "resolve_conditional_coupon_subject_handle",
]
