"""
modules/ai/brain/postprocess/shipment_evidence.py
───────────────────────────────────────────────────
Structured shipment evidence only — never infer from customer imperatives,
payment receipts, under_review status, prior bot text, or untrusted echoes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.active_order_context import load_commerce_bundle

# Matches store_sync shipped transition set (+ delivered).
_SHIPPING_EVIDENCE_STATUSES = frozenset({
    "shipped",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "delivering",
})

_TRUSTED_AUTOMATION_KEYS = (
    "automation_trigger",
    "smart_trigger",
    "automation_event_type",
    "store_sync_event",
    "automation_event",
)


@dataclass(frozen=True)
class ShipmentEvidenceResult:
    evidence_ok: bool
    evidence_source: str
    order_status: str
    tracking_present: bool
    reason: str


def _structured_ctx(commerce_bundle: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    bundle = commerce_bundle or {}
    ctx = bundle.get("active_order_context")
    return dict(ctx) if isinstance(ctx, dict) else None


def _tracking_from_ctx(ctx: Optional[Dict[str, Any]]) -> tuple[bool, str]:
    if not ctx:
        return False, ""
    url = str(ctx.get("tracking_url") or "").strip()
    if url:
        return True, "tracking_url"
    number = str(ctx.get("tracking_number") or "").strip()
    if number:
        return True, "tracking_number"
    return False, ""


def _status_in_evidence_set(raw: Optional[str]) -> bool:
    status = str(raw or "").strip().lower()
    if not status:
        return False
    if status in _SHIPPING_EVIDENCE_STATUSES:
        return True
    # Normalise common aliases without trusting free-text history.
    aliases = {
        "out-for-delivery": "out_for_delivery",
        "in-transit": "in_transit",
    }
    return aliases.get(status, status) in _SHIPPING_EVIDENCE_STATUSES


def _trusted_automation_metadata(metadata: Optional[Dict[str, Any]]) -> tuple[bool, str]:
    md = metadata or {}
    for key in _TRUSTED_AUTOMATION_KEYS:
        val = str(md.get(key) or "").strip().lower()
        if val == "order_shipped":
            return True, f"metadata.{key}=order_shipped"
    event = md.get("automation_payload")
    if isinstance(event, dict):
        trigger = str(event.get("trigger") or event.get("event_type") or "").strip().lower()
        if trigger == "order_shipped":
            return True, "metadata.automation_payload.order_shipped"
    # smb echoes are untrusted unless they carry store/shipping automation proof.
    if str(md.get("event_type") or "").strip().lower() == "smb_message_echo":
        return False, "smb_message_echo_without_trusted_automation"
    return False, ""


def evaluate_shipment_evidence(
    *,
    commerce_bundle: Optional[Dict[str, Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
    inbound_metadata: Optional[Dict[str, Any]] = None,
    payment_receipt_received: bool = False,
) -> ShipmentEvidenceResult:
    """Return whether structured shipment evidence exists for this turn."""
    bundle = commerce_bundle
    if bundle is None and extra_metadata is not None:
        bundle = load_commerce_bundle(extra_metadata)

    ctx = _structured_ctx(bundle)
    order_status = str((ctx or {}).get("order_status") or "").strip()
    tracking_present, tracking_source = _tracking_from_ctx(ctx)

    if tracking_present:
        return ShipmentEvidenceResult(
            evidence_ok=True,
            evidence_source=tracking_source,
            order_status=order_status,
            tracking_present=True,
            reason="structured_tracking",
        )

    if ctx:
        shipping_status = str(ctx.get("shipping_status") or "").strip().lower()
        if _status_in_evidence_set(shipping_status):
            return ShipmentEvidenceResult(
                evidence_ok=True,
                evidence_source="structured_shipping_status",
                order_status=order_status,
                tracking_present=False,
                reason=f"shipping_status={shipping_status}",
            )
        if _status_in_evidence_set(order_status):
            return ShipmentEvidenceResult(
                evidence_ok=True,
                evidence_source="structured_order_status",
                order_status=order_status,
                tracking_present=False,
                reason=f"order_status={order_status}",
            )

    auto_ok, auto_reason = _trusted_automation_metadata(inbound_metadata)
    if auto_ok:
        return ShipmentEvidenceResult(
            evidence_ok=True,
            evidence_source="automation_order_shipped",
            order_status=order_status,
            tracking_present=tracking_present,
            reason=auto_reason,
        )

    if payment_receipt_received:
        return ShipmentEvidenceResult(
            evidence_ok=False,
            evidence_source="none",
            order_status=order_status or "under_review",
            tracking_present=False,
            reason="payment_receipt_alone_not_shipment_evidence",
        )

    if order_status in {"pending_review", "under_review", "confirmed", "preparing"}:
        return ShipmentEvidenceResult(
            evidence_ok=False,
            evidence_source="none",
            order_status=order_status,
            tracking_present=False,
            reason="pre_shipment_order_status",
        )

    return ShipmentEvidenceResult(
        evidence_ok=False,
        evidence_source="none",
        order_status=order_status,
        tracking_present=False,
        reason="no_structured_shipment_evidence",
    )
