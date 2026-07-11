"""
order_creation_claim_guard.py
─────────────────────────────
Block outbound order-creation / order-number claims without persisted evidence.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.order_creation_evidence import (
    NO_ORDER_NUMBER_YET_AR,
    OrderCreationEvidence,
    OrderCreationStatus,
    outbound_contains_unsupported_creation_claim,
    resolve_order_creation_evidence,
)

logger = logging.getLogger("nahla.brain.postprocess.order_creation_claim_guard")

_ORDER_NUMBER_LINE_RE = re.compile(
    r"(?:^|\n)\s*رقم\s*الطلب\s*[:：]?\s*[^\n]+",
    re.UNICODE | re.IGNORECASE,
)
_ORDER_CONFIRMED_RE = re.compile(
    r"تم\s*تأكيد\s*الطلب",
    re.UNICODE | re.IGNORECASE,
)
_COMPENSATION_PROMISE_RE = re.compile(
    r"(?:"
    r"\d+\s*%|"
    r"خصم\s*\d+|"
    r"كوبون|كود\s*خصم|"
    r"تعويض|compensation|discount\s*(?:of|for)?\s*\d+"
    r")",
    re.UNICODE | re.IGNORECASE,
)


@dataclass(frozen=True)
class OrderCreationClaimGuardResult:
    reply: str
    replaced: bool = False
    reason: str = ""


def _line_items_in_prep(
    order_prep: Optional[Dict[str, Any]],
    brain_state: Optional[Dict[str, Any]],
) -> bool:
    try:
        from modules.ai.order_flow_v2.state import line_items_from_state  # noqa: PLC0415

        prep = dict(order_prep or {})
        bs = dict(brain_state or {})
        return bool(line_items_from_state(prep, bs))
    except Exception:  # noqa: BLE001
        return bool((order_prep or {}).get("line_items"))


def _resolve_evidence_with_persisted_draft(
    *,
    db: Any,
    tenant_id: Optional[int],
    conversation_id: Optional[int],
    order_prep: Optional[Dict[str, Any]],
    brain_state: Optional[Dict[str, Any]],
    state: Any = None,
) -> OrderCreationEvidence:
    evidence = resolve_order_creation_evidence(
        state=state,
        order_prep=order_prep,
    )
    if evidence.can_claim_created():
        return evidence

    prep = dict(order_prep or {})
    ref = str(prep.get("draft_order_reference") or "").strip()
    if str(prep.get("order_creation_status") or "").strip().lower() == "created" and ref:
        return OrderCreationEvidence(
            status=OrderCreationStatus.CREATED,
            reference=ref,
        )

    if db is None or not tenant_id or not conversation_id:
        return evidence

    try:
        from core.order_context_builder import _load_active_draft  # noqa: PLC0415
        from modules.ai.order_flow_v2.order_reference import order_display_reference  # noqa: PLC0415

        draft = _load_active_draft(
            db,
            tenant_id=int(tenant_id),
            conversation_id=int(conversation_id),
        )
        if draft is None or not draft.order_id:
            return evidence
        from models import Order  # noqa: PLC0415

        row = db.query(Order).filter_by(id=int(draft.order_id)).first()
        reference = order_display_reference(row, db=db)
        if reference:
            return OrderCreationEvidence(
                status=OrderCreationStatus.CREATED,
                reference=reference,
                draft_order_id=str(draft.external_id or reference),
            )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — persisted draft evidence lookup is best-effort
        pass
    return evidence


def _honest_no_reference_reply(
    *,
    order_prep: Optional[Dict[str, Any]],
    brain_state: Optional[Dict[str, Any]],
) -> str:
    prep = dict(order_prep or {})
    if _line_items_in_prep(order_prep, brain_state) or prep.get("order_flow_v2_active"):
        return NO_ORDER_NUMBER_YET_AR
    return (
        "لا أقدر أؤكد إنشاء الطلب من هذه المحادثة إلا بعد تسجيله في النظام. "
        "إذا تحتاج مساعدة، تواصل مع المتجر."
    )


def _strip_unsupported_order_claims(text: str) -> str:
    cleaned = _ORDER_NUMBER_LINE_RE.sub("", text)
    cleaned = _ORDER_CONFIRMED_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _trusted_compensation_evidence(
    *,
    brain_state: Optional[Dict[str, Any]] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> bool:
    for source in (brain_state, order_prep, commerce_bundle):
        if not isinstance(source, dict):
            continue
        if str(source.get("coupon_id") or source.get("trusted_coupon_code") or "").strip():
            return True
        if source.get("discount_applied") is True:
            return True
        if source.get("approved_compensation_policy") is True:
            return True
        exec_log = source.get("last_execution") or source.get("action_execution")
        if isinstance(exec_log, dict):
            action = str(exec_log.get("action") or "").strip().lower()
            if action in {"apply_coupon", "apply_discount", "grant_compensation"}:
                return bool(exec_log.get("success") is True)
    return False


def _strip_unsupported_compensation_claims(text: str) -> str:
    kept: list[str] = []
    for chunk in re.split(r"(?<=[.!?؟،])\s+|\n+", text or ""):
        part = chunk.strip()
        if part and not _COMPENSATION_PROMISE_RE.search(part):
            kept.append(part)
    return " ".join(kept).strip()


def apply_order_creation_claim_guard(
    reply: str,
    *,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    order_prep: Optional[Dict[str, Any]] = None,
    brain_state: Optional[Dict[str, Any]] = None,
    state: Any = None,
    commerce_bundle: Optional[Dict[str, Any]] = None,
) -> OrderCreationClaimGuardResult:
    original = str(reply or "")
    if not original.strip():
        return OrderCreationClaimGuardResult(reply=original, replaced=False)

    if (
        _COMPENSATION_PROMISE_RE.search(original)
        and not _trusted_compensation_evidence(
            brain_state=brain_state,
            order_prep=order_prep,
            commerce_bundle=commerce_bundle,
        )
    ):
        stripped = _strip_unsupported_compensation_claims(original)
        if stripped != original:
            logger.info(
                "[ORDER_CREATION_CLAIM_GUARD] compensation_scrub tenant=%s conversation=%s",
                tenant_id,
                conversation_id,
            )
            return OrderCreationClaimGuardResult(
                reply=stripped,
                replaced=True,
                reason="untrusted_compensation_claim",
            )

    evidence = _resolve_evidence_with_persisted_draft(
        db=db,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        order_prep=order_prep,
        brain_state=brain_state,
        state=state,
    )

    unsupported_creation = outbound_contains_unsupported_creation_claim(original, evidence)
    unsupported_number = (
        not evidence.can_claim_created()
        and bool(_ORDER_NUMBER_LINE_RE.search(original))
    )
    unsupported_confirmed = (
        not evidence.can_claim_created()
        and bool(_ORDER_CONFIRMED_RE.search(original))
    )

    if not (unsupported_creation or unsupported_number or unsupported_confirmed):
        return OrderCreationClaimGuardResult(reply=original, replaced=False)

    replacement = _honest_no_reference_reply(
        order_prep=order_prep,
        brain_state=brain_state,
    )
    if unsupported_number and not unsupported_creation and not unsupported_confirmed:
        stripped = _strip_unsupported_order_claims(original)
        if stripped and "رقم الطلب" not in stripped and "تم إنشاء" not in stripped:
            replacement = stripped

    logger.info(
        "[ORDER_CREATION_CLAIM_GUARD] replaced tenant=%s conversation=%s "
        "creation=%s number=%s confirmed=%s",
        tenant_id,
        conversation_id,
        unsupported_creation,
        unsupported_number,
        unsupported_confirmed,
    )
    return OrderCreationClaimGuardResult(
        reply=replacement,
        replaced=True,
        reason="unsupported_order_creation_claim",
    )


__all__ = [
    "OrderCreationClaimGuardResult",
    "apply_order_creation_claim_guard",
]
