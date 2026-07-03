"""
core/order_creation_evidence.py
ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤ظ¤
Order creation evidence ظ¤ P1-C-1.

Operational claims about order creation (creating / created / prepared)
require persisted evidence. Prevents ┬س╪ش╪د╪▒┘è ╪ح┘╪┤╪د╪ة┬╗ / ┬س╪ز┘à ╪ح┘╪┤╪د╪ة┬╗ /
┬س┘┘à ╪ث╪ش╪» ╪╖┘╪ذ╪د╪ز┬╗ contradictions.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nahla.order_creation_evidence")

NO_ORDER_NUMBER_YET_AR = (
    "باقي ما صدر رقم طلب؛ نحتاج نكمل البيانات ثم ننشئ الطلب."
)
ORDER_REFERENCE_CREATE_FAILED_AR = (
    "تعذر إنشاء رقم الطلب الآن، وسيتم مراجعته من فريق المتجر."
)

_CREATING_OUTBOUND_MARKERS = (
    "جارٍ إنشاء طلب",
    "جاري إنشاء طلب",
    "جارٍ إنشاء",
    "جاري إنشاء",
)

_CREATED_CLAIM_RE = re.compile(
    r"(?:تم\s*إنشاء\s*الطلب|تم\s*إنشاء\s*طلب|تم\s*تجهيز\s*طلب|"
    r"تم\s*إنشاء\s*طلبك|تم\s*تجهيز\s*طلبك)",
    re.UNICODE | re.IGNORECASE,
)

_CREATING_CLAIM_RE = re.compile(
    r"(?:جار[يٍ]\s*إنشاء|جاري\s*إنشاء)",
    re.UNICODE | re.IGNORECASE,
)


class OrderCreationStatus(str, Enum):
    NONE = "none"
    CREATING = "creating"
    CREATED = "created"
    FAILED = "failed"


@dataclass(frozen=True)
class OrderCreationEvidence:
    status: OrderCreationStatus
    draft_order_id: str = ""
    salla_order_id: str = ""
    product_title: str = ""
    reference: str = ""

    @property
    def has_store_reference(self) -> bool:
        return bool(
            str(self.draft_order_id or "").strip()
            or str(self.salla_order_id or "").strip()
            or str(self.reference or "").strip()
        )

    def can_claim_created(self) -> bool:
        return (
            self.status == OrderCreationStatus.CREATED
            and self.has_store_reference
        )

    def can_claim_creating(self) -> bool:
        return self.status == OrderCreationStatus.CREATING

    def can_claim_failed(self) -> bool:
        return self.status == OrderCreationStatus.FAILED


def _status_from_raw(raw: str) -> OrderCreationStatus:
    val = str(raw or "").strip().lower()
    for member in OrderCreationStatus:
        if member.value == val:
            return member
    return OrderCreationStatus.NONE


def resolve_order_creation_evidence(
    *,
    state: Any = None,
    order_prep: Any = None,
    handler_data: Optional[Dict[str, Any]] = None,
) -> OrderCreationEvidence:
    """Resolve evidence from brain state + optional handler payload."""
    data = dict(handler_data or {})
    op = order_prep
    if op is None and state is not None:
        op = getattr(state, "order_prep", None)

    draft_id = ""
    salla_id = ""
    product_title = ""
    status = OrderCreationStatus.NONE
    reference = ""

    if state is not None:
        draft_id = str(getattr(state, "draft_order_id", "") or "").strip()
        focus = getattr(state, "current_product_focus", None) or {}
        if isinstance(focus, dict):
            product_title = str(focus.get("title") or focus.get("name") or "")

    if op is not None:
        status = _status_from_raw(getattr(op, "order_creation_status", "") or "")
        salla_id = str(getattr(op, "salla_order_id", "") or "").strip()
        if not product_title:
            pid = str(getattr(op, "product_id", "") or "")
            if pid:
                product_title = pid

    ref_from_data = str(
        data.get("reference") or data.get("order_id") or ""
    ).strip()
    if ref_from_data:
        reference = ref_from_data
        salla_id = salla_id or ref_from_data

    if data.get("salla_retry"):
        status = OrderCreationStatus.CREATING
    elif data.get("salla_escalate") or data.get("intent_only"):
        status = OrderCreationStatus.FAILED
    elif ref_from_data or draft_id or salla_id:
        if status not in (
            OrderCreationStatus.CREATING,
            OrderCreationStatus.FAILED,
        ):
            status = OrderCreationStatus.CREATED

    if status == OrderCreationStatus.NONE and op is not None:
        failure_count = int(getattr(op, "salla_failure_count", 0) or 0)
        last_failed = bool(getattr(op, "last_order_failed", False))
        if failure_count >= 2 and last_failed:
            status = OrderCreationStatus.FAILED
        elif last_failed and failure_count == 1:
            status = OrderCreationStatus.CREATING

    if draft_id and status == OrderCreationStatus.NONE:
        status = OrderCreationStatus.CREATED

    return OrderCreationEvidence(
        status=status,
        draft_order_id=draft_id,
        salla_order_id=salla_id,
        product_title=product_title,
        reference=reference or draft_id or salla_id,
    )


def stamp_order_prep_creation(
    prep: Any,
    *,
    status: OrderCreationStatus,
    salla_order_id: str = "",
) -> None:
    try:
        prep.order_creation_status = status.value
        if salla_order_id:
            prep.salla_order_id = str(salla_order_id)
    except Exception:  # noqa: BLE001  # noqa: silent-ok — prep stamp is best-effort on handler paths
        pass


def recent_outbound_claims_order_creating(
    history: Optional[List[Any]],
    *,
    lookback: int = 6,
) -> bool:
    if not history:
        return False
    seen = 0
    try:
        for turn in reversed(history):
            direction = str((turn or {}).get("direction") or "").lower()
            if direction not in ("out", "outbound"):
                continue
            seen += 1
            body = str((turn or {}).get("body") or "")
            if any(marker in body for marker in _CREATING_OUTBOUND_MARKERS):
                return True
            if seen >= lookback:
                break
    except Exception:  # noqa: BLE001  # noqa: silent-ok — history scan is best-effort for track fallback
        return False
    return False


def resolve_track_order_fallback(
    *,
    state: Any = None,
    history: Optional[List[Any]] = None,
    handler_data: Optional[Dict[str, Any]] = None,
    db: Any = None,
    tenant_id: Optional[int] = None,
    conversation_id: Optional[int] = None,
    phone: Optional[str] = None,
) -> Optional[str]:
    """Honest reply when store lookup finds no order but local evidence exists."""
    from modules.ai.brain.compose import templates as T  # noqa: PLC0415

    resolved_phone = str(phone or "").strip()
    if not resolved_phone and state is not None:
        resolved_phone = str(getattr(state, "customer_phone", "") or "").strip()

    if db is not None and tenant_id:
        try:
            from core.local_order_resolver import (  # noqa: PLC0415
                resolve_customer_order_context,
            )

            local_ctx = resolve_customer_order_context(
                db,
                tenant_id=int(tenant_id),
                conversation_id=int(conversation_id) if conversation_id else None,
                phone=resolved_phone or None,
                intent="track_order",
            )
            if local_ctx.selected_order is not None:
                ref = local_ctx.selected_order.display_reference
                if ref:
                    status = local_ctx.selected_order.status or "draft"
                    label = "قيد الإكمال" if local_ctx.selected_order.is_open else status
                    return T.order_status(
                        reference=ref,
                        status=status,
                        status_label_ar=label,
                    )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — local resolver must not break track fallback
            pass

    if db is not None and tenant_id and conversation_id:
        try:
            from modules.ai.checkout_authority import (  # noqa: PLC0415
                load_local_draft_evidence,
                draft_display_reference,
            )

            draft = load_local_draft_evidence(
                db,
                tenant_id=int(tenant_id),
                conversation_id=int(conversation_id),
            )
            ref = draft_display_reference(draft)
            if ref:
                return T.order_status(
                    reference=ref,
                    status="draft",
                    status_label_ar="قيد الإكمال",
                )
        except Exception:  # noqa: BLE001  # noqa: silent-ok — local draft lookup must not break track fallback
            pass

    evidence = resolve_order_creation_evidence(
        state=state,
        handler_data=handler_data,
    )

    if evidence.can_claim_created() and evidence.reference:
        return T.order_status(
            reference=evidence.reference,
            status="creating",
            status_label_ar="قيد الإنشاء",
        )

    if evidence.can_claim_creating() or recent_outbound_claims_order_creating(history):
        product = {}
        if state is not None:
            focus = getattr(state, "current_product_focus", None)
            if isinstance(focus, dict):
                product = focus
        return T.order_creation_in_progress(
            product=product,
            reference=evidence.reference,
        )

    if evidence.can_claim_failed():
        product = {}
        if state is not None:
            focus = getattr(state, "current_product_focus", None)
            if isinstance(focus, dict):
                product = focus
        return T.order_creation_failed(product=product)

    draft_id = str(getattr(state, "draft_order_id", "") or "").strip() if state else ""
    if draft_id:
        return T.order_creation_in_progress(
            product=getattr(state, "current_product_focus", None) or {},
            reference=draft_id,
        )

    prep_d: Dict[str, Any] = {}
    op = None
    if state is not None:
        op = getattr(state, "order_prep", None)
    if op is not None:
        if hasattr(op, "to_dict"):
            try:
                prep_d = dict(op.to_dict())
            except Exception:  # noqa: BLE001
                prep_d = {}
        elif isinstance(op, dict):
            prep_d = dict(op)
    if prep_d.get("line_items") or prep_d.get("order_flow_v2_trusted_price"):
        items = prep_d.get("line_items") or []
        if items or prep_d.get("catalog_line_items_authoritative"):
            return NO_ORDER_NUMBER_YET_AR

    if prep_d.get("local_draft_authoritative") or prep_d.get("draft_order_reference"):
        ref = str(prep_d.get("draft_order_reference") or "").strip()
        if ref:
            return T.order_status(
                reference=ref,
                status="draft",
                status_label_ar="قيد الإكمال",
            )
        return NO_ORDER_NUMBER_YET_AR

    return None


def outbound_contains_unsupported_creation_claim(
    text: str,
    evidence: OrderCreationEvidence,
) -> bool:
    """True when outbound text claims creation without evidence."""
    body = text or ""
    if not body.strip():
        return False
    if _CREATED_CLAIM_RE.search(body) and not evidence.can_claim_created():
        return True
    if _CREATING_CLAIM_RE.search(body) and not (
        evidence.can_claim_creating() or evidence.can_claim_created()
    ):
        return True
    return False


def log_order_creation_evidence(
    *,
    tenant_id: Any = None,
    evidence: OrderCreationEvidence,
    route: str = "",
) -> None:
    try:
        logger.info(
            "[ORDER_CREATION_EVIDENCE] tenant=%s route=%s status=%s "
            "draft_order_id=%s salla_order_id=%s reference=%s",
            tenant_id,
            route or "-",
            evidence.status.value,
            evidence.draft_order_id or "-",
            evidence.salla_order_id or "-",
            evidence.reference or "-",
        )
    except Exception:  # noqa: BLE001  # noqa: silent-ok — order evidence telemetry must never block handler
        pass


__all__ = [
    "NO_ORDER_NUMBER_YET_AR",
    "ORDER_REFERENCE_CREATE_FAILED_AR",
    "OrderCreationEvidence",
    "OrderCreationStatus",
    "log_order_creation_evidence",
    "outbound_contains_unsupported_creation_claim",
    "recent_outbound_claims_order_creating",
    "resolve_order_creation_evidence",
    "resolve_track_order_fallback",
    "stamp_order_prep_creation",
]
