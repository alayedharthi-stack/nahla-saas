#!/usr/bin/env python3
"""
P0 Bank Receipt Extraction Reliability — read-only measurement report.

Queries inbound document metadata from MessageEvent rows and computes:
  * text quality scores (post-hoc, same pure functions as prod telemetry)
  * garbled-text detection rate
  * shadow OCR escalation rate
  * field-confidence gaps (status=ok but core fields absent)

Requires DATABASE_URL. Does NOT mutate state or invoke Vision.

Enable prod telemetry (7–14 day window) with::

    RECEIPT_TEXT_QUALITY_TELEMETRY_ENABLED=1
    RECEIPT_FIELD_EXTRACTION_TELEMETRY_ENABLED=1
    RECEIPT_VERDICT_TELEMETRY_ENABLED=1

Grep log targets: [RECEIPT_TEXT_QUALITY], [RECEIPT_OCR_ESCALATION_SHADOW],
[PAYMENT_RECEIPT_EXTRACTED], [PAYMENT_VERIFICATION_DECISION]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, text

from core.receipt_extraction import compute_receipt_fields
from core.receipt_text_quality import (
    compute_ocr_escalation_shadow,
    compute_text_quality,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--days", type=int, default=14,
        help="Lookback window (default: 14)",
    )
    p.add_argument(
        "--tenant-id", type=int, default=None,
        help="Optional single-tenant filter (default: all tenants)",
    )
    p.add_argument(
        "--limit", type=int, default=5000,
        help="Max inbound document events to scan",
    )
    p.add_argument(
        "--output", type=str, default="",
        help="Optional JSON output path",
    )
    return p.parse_args()


def _metadata_blob(row: dict) -> dict:
    md = dict(row.get("metadata") or {})
    ni = md.get("normalized_inbound") or {}
    if isinstance(ni, dict):
        nim = ni.get("metadata")
        if isinstance(nim, dict):
            return nim
        return ni
    return md


def main() -> None:
    args = _parse_args()
    url = os.environ.get("DATABASE_URL", "").replace(
        "postgres://", "postgresql://", 1,
    )
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    eng = create_engine(url)

    sql = """
        SELECT id, tenant_id, conversation_id, created_at, metadata
        FROM message_events
        WHERE direction = 'inbound'
          AND created_at >= :since
          AND (
            metadata->'normalized_inbound'->'metadata'->>'source_type' = 'document'
            OR metadata->'normalized_inbound'->>'normalized_type' = 'document'
          )
    """
    params: dict = {"since": since, "limit": args.limit}
    if args.tenant_id is not None:
        sql += " AND tenant_id = :tenant_id"
        params["tenant_id"] = args.tenant_id
    sql += " ORDER BY id DESC LIMIT :limit"

    rows: list = []
    with eng.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    counters: Counter = Counter()
    samples: list = []

    for row in rows:
        nim = _metadata_blob(dict(row))
        pdf_status = str(nim.get("pdf_text_status") or "")
        pdf_kind = str(nim.get("pdf_kind") or "unknown")
        full_text = str(
            nim.get("pdf_text_full")
            or nim.get("pdf_text_preview")
            or ""
        )
        text_len = int(nim.get("pdf_text_length") or len(full_text))

        counters["documents_total"] += 1
        if pdf_status == "ok" and text_len > 0:
            counters["pypdf_ok_nonempty"] += 1
        if pdf_kind in {
            "payment_receipt",
            "payment_pre_review",
            "payment_pending_evidence",
        }:
            counters["payment_candidates"] += 1

        quality = compute_text_quality(full_text)
        if quality.is_garbled:
            counters["garbled_detected"] += 1

        pypdf_text = full_text if pdf_status in ("ok", "") else ""
        pypdf_status = str(nim.get("pdf_pypdf_text_status") or pdf_status)
        shadow = compute_ocr_escalation_shadow(
            text=pypdf_text or full_text,
            pdf_kind=pdf_kind,
            pdf_text_status=pypdf_status,
            metadata=nim,
        )
        if shadow.would_escalate:
            counters["shadow_would_escalate"] += 1

        fields = compute_receipt_fields(metadata=nim)
        if (
            pdf_status == "ok"
            and text_len > 0
            and pdf_kind in {
                "payment_receipt",
                "payment_pre_review",
                "payment_pending_evidence",
            }
            and fields.amount_confidence.value == "absent"
            and fields.iban_confidence.value == "absent"
        ):
            counters["phase1_gap_ok_but_core_absent"] += 1
            if len(samples) < 20:
                samples.append({
                    "event_id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "pdf_kind": pdf_kind,
                    "pdf_text_status": pdf_status,
                    "text_len": text_len,
                    "quality_score": quality.quality_score,
                    "is_garbled": quality.is_garbled,
                    "shadow_reason": shadow.shadow_reason,
                    "amount_confidence": fields.amount_confidence.value,
                    "iban_confidence": fields.iban_confidence.value,
                    "beneficiary_confidence": fields.beneficiary_confidence.value,
                })

    total = max(counters["documents_total"], 1)
    payment_n = max(counters["payment_candidates"], 1)
    pypdf_n = max(counters["pypdf_ok_nonempty"], 1)

    report = {
        "window_days": args.days,
        "since_utc": since.isoformat(),
        "tenant_filter": args.tenant_id,
        "documents_scanned": counters["documents_total"],
        "rates": {
            "garbled_of_all_documents": round(
                counters["garbled_detected"] / total, 4,
            ),
            "shadow_escalate_of_payment_candidates": round(
                counters["shadow_would_escalate"] / payment_n, 4,
            ),
            "phase1_gap_of_pypdf_ok_payment": round(
                counters["phase1_gap_ok_but_core_absent"] / pypdf_n, 4,
            ),
        },
        "counts": dict(counters),
        "samples_phase1_gap": samples,
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output:
        Path(args.output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
