"""Trusted tenant-scoped, read-only promotion tool.

The commerce runtime's model may tell a customer about a coupon only when the
merchant's own records say it is currently valid and shareable on this
channel. ``promotion_truth.resolve_shareable_promotions`` is the platform's
one resolver for that: it never invents a code, it leaves out campaign-only,
expired, disabled and exhausted codes, and it reports what it could not read.
This module projects its facts into evidence the loop verifies a reply
against. Whether the customer in the conversation qualifies is not evaluated
here, and every projection says so.

There is no Agents-SDK wrapper: the legacy path keeps its own promotion
policy. The commerce runtime's loop passes the trusted context directly.
"""
from __future__ import annotations

from typing import Any

from modules.ai.brain.commerce.promotion_truth import (
    NO_VALID_PROMOTIONS,
    PROMOTION_QUERY_FAILED,
    resolve_shareable_promotions,
)
from modules.ai.commerce_agent_v2.context import CommerceAgentContext
from modules.ai.commerce_agent_v2.output import (
    EvidenceRecord,
    PromotionListResult,
    PromotionSnapshot,
)

MAX_PROMOTIONS = 8
_MAX_TEXT = 300
_RECORD_KINDS = ("coupon", "offer")


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def _project(fact: dict[str, Any]) -> tuple[PromotionSnapshot, EvidenceRecord] | None:
    """One resolver fact as a snapshot plus its evidence record, or ``None``
    when the fact cannot be cited: no id, an unknown kind, or a coupon
    without a code. An offer never carries a code."""
    kind = str(fact.get("record_kind") or "")
    try:
        promotion_id = int(fact.get("id"))
    except (TypeError, ValueError):
        return None
    if promotion_id <= 0 or kind not in _RECORD_KINDS:
        return None
    code = _text(fact.get("code"), 64) if kind == "coupon" else ""
    if kind == "coupon" and not code:
        return None
    conditions = fact.get("conditions")
    if not isinstance(conditions, dict):
        conditions = {}
    ref = f"promotion:{kind}:{promotion_id}"
    fields: dict[str, Any] = {
        "promotion_id": promotion_id,
        "record_kind": kind,
        "code": code,
        "name": _text(fact.get("name"), 160),
        "description": _text(fact.get("description")),
        "discount_type": _text(fact.get("discount_type") or fact.get("promotion_type"), 64),
        "discount_value": _text(fact.get("discount_value"), 64),
        "expires_at": _text(fact.get("expires_at") or fact.get("ends_at"), 64),
        "conditions": dict(conditions),
        "eligibility_determined": False,
        "eligibility_note": _text(fact.get("eligibility_note"), 120),
    }
    evidence = EvidenceRecord(
        ref=ref,
        source="promotion_coupon" if kind == "coupon" else "promotion_offer",
        source_id=str(promotion_id),
        facts=[],
        fields=dict(fields),
        provenance={
            "service": "modules.ai.brain.commerce.promotion_truth.resolve_shareable_promotions",
            "record": "coupons" if kind == "coupon" else "promotions",
            "freshness": "query_time",
        },
    )
    return PromotionSnapshot(**fields, evidence_ref=ref), evidence


async def list_shareable_promotions_impl(
    context: CommerceAgentContext,
    *,
    limit: int = MAX_PROMOTIONS,
) -> PromotionListResult:
    """SDK-free implementation of the ``list_shareable_promotions`` read tool.

    Reads the tenant's currently valid, shareable coupons and offers through
    the platform's promotion-truth resolver and registers each as evidence.
    ``not_found`` is an honest empty answer; ``error`` says the records could
    not be read, which is never reported as "no promotions".
    """
    context.assert_scope()
    bounded = max(1, min(int(limit or MAX_PROMOTIONS), MAX_PROMOTIONS))
    truth = resolve_shareable_promotions(context.db, int(context.tenant_id), limit=bounded)
    snapshots: list[PromotionSnapshot] = []
    evidence: list[EvidenceRecord] = []
    for fact in [*list(getattr(truth, "shareable", None) or ()),
                 *list(getattr(truth, "offers", None) or ())]:
        projected = _project(fact) if isinstance(fact, dict) else None
        if projected is None:
            continue
        snapshot, record = projected
        snapshots.append(snapshot)
        evidence.append(record)
        if len(snapshots) >= bounded:
            break
    outcome = str(getattr(truth, "query_outcome", "") or "")
    if not snapshots:
        if bool(getattr(truth, "query_failed", False)):
            return PromotionListResult(
                status="error",
                query_outcome=outcome or PROMOTION_QUERY_FAILED,
                failure_reason="promotion_query_failed",
            )
        return PromotionListResult(
            status="not_found",
            query_outcome=outcome or NO_VALID_PROMOTIONS,
            failure_reason="no_valid_shareable_promotions",
        )
    context.register_evidence(evidence)
    return PromotionListResult(status="ok", promotions=snapshots, evidence=evidence,
                               query_outcome=outcome)


__all__ = ["MAX_PROMOTIONS", "list_shareable_promotions_impl"]
