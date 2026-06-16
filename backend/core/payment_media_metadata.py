"""
core/payment_media_metadata.py
──────────────────────────────
Flatten inbound MessageEvent metadata for payment pipelines.

WhatsApp webhook persists classifier output under
``extra_metadata.normalized_inbound`` — older readers that only
looked at the top level missed ``payment_evidence_status`` on the
follow-up "تم التحويل" promotion path.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def flatten_inbound_payment_metadata(raw: Any) -> Dict[str, Any]:
    """Merge ``normalized_inbound`` into a single dict for payment readers."""
    if not isinstance(raw, dict):
        return {}
    out = dict(raw)
    nested = raw.get("normalized_inbound")
    if isinstance(nested, dict):
        for key, val in nested.items():
            if val not in (None, "") and key not in out:
                out[key] = val
            elif key in (
                "payment_evidence_status",
                "payment_evidence_reason",
                "payment_evidence_signals",
                "payment_evidence_hints",
                "payment_resolution_state",
                "bank_receipt_extraction",
                "receipt_data",
                "pdf_kind",
                "image_kind",
                "vision_text",
                "ocr_text",
                "pdf_text_preview",
                "caption",
                "filename",
                "storage_url",
            ) and val not in (None, ""):
                out[key] = val
    return out


def payment_text_blob(metadata: Dict[str, Any]) -> str:
    """Best-effort OCR / vision text for tenant-account matching."""
    parts = [
        metadata.get("vision_text"),
        metadata.get("ocr_text"),
        metadata.get("pdf_text_preview"),
        metadata.get("pdf_text_full"),
        metadata.get("caption"),
        metadata.get("filename"),
    ]
    hints = metadata.get("payment_evidence_hints")
    if isinstance(hints, dict):
        parts.extend(str(hints.get(k) or "") for k in (
            "bank_name", "amount", "sender_name", "reference_number",
        ))
    receipt_data = metadata.get("receipt_data")
    if isinstance(receipt_data, dict):
        parts.extend(str(receipt_data.get(k) or "") for k in (
            "bank_name", "amount", "beneficiary_name", "beneficiary_iban",
            "reference_number",
        ))
    return "\n".join(str(p or "").strip() for p in parts if p).strip()


def build_payment_evidence_linkage(
    *,
    tenant_id: int,
    phone: str,
    conversation: Any = None,
    customer: Any = None,
) -> Dict[str, Any]:
    """Link payment evidence to customer → conversation → pending order."""
    linkage: Dict[str, Any] = {
        "tenant_id": int(tenant_id) if tenant_id else None,
        "customer_phone": str(phone or "").strip() or None,
    }
    conv_id = getattr(conversation, "id", None)
    if conv_id is not None:
        linkage["conversation_id"] = int(conv_id)
    cust = customer
    if cust is None and conversation is not None:
        cust = getattr(conversation, "customer", None)
    cust_id = getattr(cust, "id", None)
    if cust_id is not None:
        linkage["customer_id"] = int(cust_id)
    if tenant_id and conv_id:
        try:
            from services.nahla_order_bridge import nahla_wa_external_id  # noqa: PLC0415

            linkage["pending_order_external_id"] = nahla_wa_external_id(
                int(tenant_id), int(conv_id),
            )
        except Exception:  # noqa: silent-ok — optional order bridge import
            pass
    return {k: v for k, v in linkage.items() if v not in (None, "")}


def enrich_payment_receipt_metadata(
    metadata: Dict[str, Any],
    *,
    tenant_id: int,
    phone: str,
    conversation: Any = None,
    customer: Any = None,
) -> Dict[str, Any]:
    """Return a copy of receipt metadata with linkage + receipt_data."""
    out = dict(metadata or {})
    linkage = build_payment_evidence_linkage(
        tenant_id=tenant_id,
        phone=phone,
        conversation=conversation,
        customer=customer,
    )
    if linkage:
        out["payment_evidence_linkage"] = linkage
    return out
