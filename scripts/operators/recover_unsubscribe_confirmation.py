#!/usr/bin/env python3
"""Recover one explicitly authorized, still-pending consent prompt.

Dry-run by default. Apply requires the observed pending timestamp. A durable
claim is committed BEFORE wire I/O: a crash/uncertain outcome never auto-retries.
No campaign dispatch, settings edits, model calls or inbound replay.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in reversed([ROOT, ROOT / "backend", ROOT / "database"]):
    sys.path.insert(0, str(path))

CLAIM_KEY = "unsubscribe_confirmation_recovery"


def eligibility(customer, *, tenant_id, customer_id, phone, expected_pending_at=None):
    from services.unsubscribe import is_customer_pending_unsubscribe, _parse_dt
    from services.customer_intelligence import normalize_phone

    if (customer is None or customer.id != customer_id or customer.tenant_id != tenant_id
            or normalize_phone(customer.phone) != normalize_phone(phone)):
        raise ValueError("recipient_mismatch")
    meta = customer.extra_metadata or {}
    pending = meta.get("pending_unsubscribe_at")
    if not is_customer_pending_unsubscribe(customer) or meta.get("is_unsubscribed"):
        raise ValueError("not_pending")
    if not _parse_dt(pending):
        raise ValueError("missing_pending_timestamp")
    if expected_pending_at is not None and pending != expected_pending_at:
        raise ValueError("pending_state_changed")
    sent = _parse_dt(meta.get("pending_unsubscribe_prompt_sent_at"))
    if sent and sent >= _parse_dt(pending):
        raise ValueError("prompt_already_sent")
    claim = meta.get(CLAIM_KEY) or {}
    if claim.get("pending_at") == pending:
        raise ValueError("recovery_already_claimed_no_automatic_retry")
    return pending


async def recover(db, *, tenant_id, customer_id, phone, expected_pending_at=None, apply=False):
    from models import Customer, WhatsAppConnection, Conversation
    from services.unsubscribe import build_confirmation_payload, CONFIRMATION_BODY_AR
    from services.customer_intelligence import normalize_phone
    from core.automation_send_guard import evaluate_unsubscribe_notice_send

    customer = (db.query(Customer).filter_by(id=customer_id, tenant_id=tenant_id)
                .with_for_update().one_or_none())
    pending = eligibility(customer, tenant_id=tenant_id, customer_id=customer_id,
                          phone=phone, expected_pending_at=expected_pending_at)
    phone = normalize_phone(phone)
    conn = db.query(WhatsAppConnection).filter_by(tenant_id=tenant_id).one_or_none()
    if conn is None or not conn.phone_number_id or conn.status != "connected":
        raise ValueError("connection_not_ready")
    payload = build_confirmation_payload(phone)
    gate = evaluate_unsubscribe_notice_send(
        db, tenant_id=tenant_id, customer_phone=phone, payload=payload,
        blocked_path="unsubscribe_recovery",
    )
    if gate.block:
        raise ValueError("recipient_safety_block:" + gate.reason)
    report = {"action": "would_send", "pending_at": pending, "recipient_verified": True}
    if not apply:
        db.rollback()
        return report
    if not expected_pending_at:
        raise ValueError("expected_pending_timestamp_required")
    claim = {"pending_at": pending, "status": "request_started",
             "started_at": datetime.now(timezone.utc).isoformat()}
    customer.extra_metadata = {**(customer.extra_metadata or {}), CLAIM_KEY: claim}
    phone_id = conn.phone_number_id
    db.commit()

    from routers.whatsapp_webhook import _post_wa
    result = {}
    ok = await _post_wa(phone_id=phone_id, payload=payload, _tenant_id=tenant_id,
                        _db=db, _unsubscribe_notice=True, _result_sink=result)
    # A false/ambiguous outcome retains the claim for manual reconciliation;
    # never issue a second payload just because the first may have timed out.
    customer = (db.query(Customer).filter_by(id=customer_id, tenant_id=tenant_id)
                .populate_existing().with_for_update().one())
    meta = dict(customer.extra_metadata or {})
    claim = {**claim, "status": "accepted" if ok else "not_confirmed",
             "wamid": result.get("wamid"), "classification": result.get("classification")}
    meta[CLAIM_KEY] = claim
    if ok and meta.get("pending_unsubscribe_at") == pending and meta.get("pending_unsubscribe"):
        meta["pending_unsubscribe_prompt_sent_at"] = datetime.now(timezone.utc).isoformat()
    customer.extra_metadata = meta
    db.commit()
    if ok:
        from core.conversation_engine import StateManager
        from core.outbound_send_status import build_provider_send_block
        convo = (db.query(Conversation).filter_by(tenant_id=tenant_id, customer_id=customer_id)
                 .order_by(Conversation.id.desc()).first())
        StateManager.save_message(
            db, phone, CONFIRMATION_BODY_AR, "outbound", tenant_id=tenant_id,
            conversation_id=getattr(convo, "id", None),
            extra_metadata={"compose_source": "security_exact_text",
                            "chosen_path": "unsubscribe_confirmation_recovery",
                            "provider_send": build_provider_send_block(
                                classification="ok", response_body=result.get("response_body"),
                                wamid=result.get("wamid"), operation="unsubscribe_recovery")},
        )
    return {"action": claim["status"], "wamid": result.get("wamid"),
            "classification": result.get("classification"), "recipient_verified": True}


def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    engine = create_engine(url, connect_args={"connect_timeout": 10,
                           "options": "-c statement_timeout=15000"})
    with Session(engine) as db:
        result = asyncio.run(recover(
            db, tenant_id=int(os.environ["RECOVERY_TENANT_ID"]),
            customer_id=int(os.environ["RECOVERY_CUSTOMER_ID"]),
            phone=os.environ["RECOVERY_PHONE"],
            expected_pending_at=os.environ.get("RECOVERY_EXPECTED_PENDING_AT"),
            apply=os.environ.get("RECOVERY_APPLY") == "1",
        ))
    print("UNSUBSCRIBE_RECOVERY=" + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
