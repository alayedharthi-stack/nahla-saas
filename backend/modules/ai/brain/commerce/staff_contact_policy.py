"""
Staff contact policy — pre-brain deterministic guard (Phase A).

Short-circuits explicit staff / CS contact requests when evidence
exists or returns an honest not-configured reply when it does not.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("nahla.brain.staff_contact_policy")

_FLAG_FALSY = frozenset({"0", "false", "no", "off"})


def staff_contact_policy_enabled() -> bool:
    raw = os.getenv("STAFF_CONTACT_POLICY_ENABLED", "1").strip().lower()
    return raw not in _FLAG_FALSY


@dataclass(frozen=True)
class StaffContactPolicyDecision:
    reply_text: str
    call_target: Any = None
    deliver_contact: bool = False
    reason: str = ""
    request_kind: str = ""
    evidence_source: str = ""
    skip_brain: bool = True


def _build_call_target(record: Any) -> Optional[Any]:
    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        build_staff_call_target_from_record,
    )

    return build_staff_call_target_from_record(record)


def evaluate_staff_contact_policy(
    db: Any,
    *,
    tenant_id: int,
    message: str,
    store_contact_phone: str = "",
) -> Optional[StaffContactPolicyDecision]:
    """Return a short-circuit decision for explicit contact requests."""
    if not staff_contact_policy_enabled():
        return None

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        _CONTACT_ASK_RE,
        _norm as _evidence_norm,
        build_deliver_reply_text,
        build_not_configured_reply,
        classify_staff_contact_request,
        load_staff_contact_registry,
        resolve_staff_contact,
    )
    from modules.ai.brain.commerce.contact_route_policy import (  # noqa: PLC0415
        should_defer_contact_policies_for_commerce,
        staff_policy_applies_to_named_request,
    )

    if should_defer_contact_policies_for_commerce(message or ""):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s defer=true reason=commerce_flow",
            tenant_id,
        )
        return None

    request = classify_staff_contact_request(message or "")
    if request.kind in {"none", "arrival", "not_responding"}:
        return None

    if request.kind == "general_channel":
        from modules.ai.brain.commerce.entity_extraction_guard import (  # noqa: PLC0415
            general_contact_reply_for_message,
        )

        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=general_channel deliver=false",
            tenant_id,
        )
        return StaffContactPolicyDecision(
            reply_text=general_contact_reply_for_message(message or ""),
            deliver_contact=False,
            reason="generic_store_contact",
            request_kind="general_channel",
        )

    registry = load_staff_contact_registry(
        db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
    )
    registry_match = registry.match_record_in_message(message or "") is not None
    explicit_ask = bool(_CONTACT_ASK_RE.search(_evidence_norm(message or "")))

    if request.kind == "named" and not staff_policy_applies_to_named_request(
        message or "",
        registry_match=registry_match,
        explicit_contact_ask=explicit_ask,
    ):
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=named defer=true reason=not_staff_ask",
            tenant_id,
        )
        return None

    resolution = resolve_staff_contact(registry, request, message=message or "")

    if not resolution.found and resolution.reason == "no_named_intent":
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=%s defer=true reason=no_named_intent",
            tenant_id, request.kind,
        )
        return None

    if resolution.found and resolution.record is not None:
        target = _build_call_target(resolution.record)
        if target is None:
            logger.info(
                "[STAFF_CONTACT_POLICY] tenant=%s kind=%s reason=phone_normalize_failed",
                tenant_id, request.kind,
            )
            return StaffContactPolicyDecision(
                reply_text=build_not_configured_reply(resolution),
                deliver_contact=False,
                reason="phone_normalize_failed",
                request_kind=request.kind,
                evidence_source=resolution.record.source,
            )
        logger.info(
            "[STAFF_CONTACT_POLICY] tenant=%s kind=%s deliver=true "
            "reason=%s source=%s name_len=%d",
            tenant_id,
            request.kind,
            resolution.reason,
            resolution.record.source,
            len(resolution.record.lookup_name or ""),
        )
        return StaffContactPolicyDecision(
            reply_text=build_deliver_reply_text(resolution.record),
            call_target=target,
            deliver_contact=True,
            reason=resolution.reason,
            request_kind=request.kind,
            evidence_source=resolution.record.source,
        )

    logger.info(
        "[STAFF_CONTACT_POLICY] tenant=%s kind=%s deliver=false reason=%s",
        tenant_id, request.kind, resolution.reason,
    )
    if resolution.reason in {"no_named_intent", "name_not_configured"} and not resolution.unknown_name:
        return None
    if should_defer_contact_policies_for_commerce(message or ""):
        return None
    return StaffContactPolicyDecision(
        reply_text=build_not_configured_reply(resolution),
        deliver_contact=False,
        reason=resolution.reason,
        request_kind=request.kind,
    )


def evaluate_generic_handoff_contact_policy(
    db: Any,
    *,
    tenant_id: int,
    message: str = "",
    store_contact_phone: str = "",
) -> Optional[StaffContactPolicyDecision]:
    """Contact delivery or honest not-configured for generic handoff asks."""
    if not staff_contact_policy_enabled():
        return None

    from modules.ai.brain.commerce.staff_contact_evidence import (  # noqa: PLC0415
        StaffContactRequest,
        build_deliver_reply_text,
        build_not_configured_reply,
        load_staff_contact_registry,
        resolve_staff_contact,
    )

    registry = load_staff_contact_registry(
        db, int(tenant_id or 0), store_contact_phone=store_contact_phone,
    )
    resolution = resolve_staff_contact(
        registry,
        StaffContactRequest(kind="generic_staff"),
        message=message or "",
    )
    if resolution.found and resolution.record is not None:
        target = _build_call_target(resolution.record)
        if target is None:
            return StaffContactPolicyDecision(
                reply_text=build_not_configured_reply(resolution),
                reason="phone_normalize_failed",
                request_kind="generic_staff",
            )
        return StaffContactPolicyDecision(
            reply_text=build_deliver_reply_text(resolution.record),
            call_target=target,
            deliver_contact=True,
            reason=resolution.reason,
            request_kind="generic_staff",
            evidence_source=resolution.record.source,
        )
    return StaffContactPolicyDecision(
        reply_text=build_not_configured_reply(resolution),
        deliver_contact=False,
        reason=resolution.reason,
        request_kind="generic_staff",
    )


__all__ = [
    "StaffContactPolicyDecision",
    "evaluate_generic_handoff_contact_policy",
    "evaluate_staff_contact_policy",
    "staff_contact_policy_enabled",
]
