"""
Incident investigation helper.

Read-only diagnostic script for conversation 9063 and tenant 33.

This script is NOT production logic.
This script is NOT used by runtime code paths.
This script exists only for historical debugging and audit purposes.

Do not use as architectural reference.
"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import create_engine, text
from core.receipt_extraction import compute_receipt_fields
from core.receipt_text_quality import (
    compute_ocr_escalation_shadow,
    compute_text_quality,
)

eng = create_engine(os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1))
out = []
with eng.connect() as c:
    rows = c.execute(text(
        "SELECT id, metadata FROM message_events "
        "WHERE tenant_id=33 AND conversation_id=9063 AND direction='inbound' "
        "ORDER BY id DESC LIMIT 15"
    )).mappings().all()
    for r in rows:
        md = dict(r["metadata"] or {})
        ni = md.get("normalized_inbound") or {}
        nim = ni.get("metadata") if isinstance(ni, dict) else {}
        if not isinstance(nim, dict):
            nim = {}
        blob = nim or md
        pdf_full = str(blob.get("pdf_text_full") or "")
        pdf_preview = str(blob.get("pdf_text_preview") or "")
        pdf_status = str(blob.get("pdf_text_status") or "")
        pdf_kind = str(blob.get("pdf_kind") or "")
        evidence_text = pdf_full or pdf_preview
        fields = compute_receipt_fields(metadata=blob)
        quality = compute_text_quality(evidence_text)
        shadow = compute_ocr_escalation_shadow(
            text=evidence_text,
            pdf_kind=pdf_kind,
            pdf_text_status=str(blob.get("pdf_pypdf_text_status") or pdf_status),
            metadata=blob,
        )
        out.append({
            "event_id": r["id"],
            "wa_message_id": blob.get("wa_message_id"),
            "pdf_kind": pdf_kind,
            "pdf_text_status": pdf_status,
            "pdf_text_length": blob.get("pdf_text_length"),
            "pdf_text_preview_snip": pdf_preview[:400],
            "vision_len": len(str(blob.get("vision_text") or "")),
            "quality_score": quality.quality_score,
            "is_garbled": quality.is_garbled,
            "shadow_would_escalate": shadow.would_escalate,
            "shadow_reason": shadow.shadow_reason,
            "amounts": [getattr(a, "value", None) for a in (fields.amounts or ())],
            "amount_confidence": fields.amount_confidence.value,
            "iban_confidence": fields.iban_confidence.value,
            "beneficiary_confidence": fields.beneficiary_confidence.value,
        })
    op = c.execute(text(
        "SELECT metadata->'brain_state'->'order_prep' FROM conversations WHERE id=9063"
    )).scalar() or {}
    out.append({"order_prep": {k: op.get(k) for k in (
        "total_price", "price", "customer_first_name", "customer_last_name"
    )}})
Path("trace_9063.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok")
