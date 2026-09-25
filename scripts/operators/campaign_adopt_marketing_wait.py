#!/usr/bin/env python3
"""Explicitly adopt one owner-approved legacy marketing pause; no direct sends.

Dry-run by default. Apply changes only the campaign's typed wait, under the
lease lock, preserving its paused state and all recipients/attempts. The normal
scheduler owns subsequent eligibility, capacity, stop and duplicate checks.
No broad migration; require the exact tenant, campaign and observed pause time.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in reversed([ROOT, ROOT / "backend", ROOT / "database"]):
    sys.path.insert(0, str(path))


def adopt(db, *, tenant_id: int, campaign_id: int, expected_paused_at: datetime,
          apply: bool = False):
    from models import Campaign, CampaignDispatchLease, CampaignSendAttempt
    from services import campaign_send_ledger as ledger

    lease = (db.query(CampaignDispatchLease)
             .filter_by(campaign_id=campaign_id, tenant_id=tenant_id)
             .with_for_update().one_or_none())
    c = db.query(Campaign).filter_by(id=campaign_id, tenant_id=tenant_id).with_for_update().one_or_none()
    if c is None or lease is None:
        raise ValueError("campaign_or_lease_not_found")
    existing = ledger.authorized_capacity_wait(c.template_variables, ledger.PAUSE_PROVIDER_THROTTLING)
    if existing is not None:
        return {"action": "already_adopted", "campaign_id": campaign_id, "wait": existing}
    if (c.status != "paused" or lease.pause_reason != ledger.PAUSE_PROVIDER_THROTTLING
            or lease.stop_requested_at is not None or ledger.lease_is_live(lease)
            or ledger._naive(lease.paused_at) != ledger._naive(expected_paused_at)):
        raise ValueError("campaign_state_changed_or_not_eligible")
    detail = lease.pause_detail or ""
    if not detail.startswith("post_accept marketing_blocked "):
        raise ValueError("not_a_typed_marketing_pause")
    if db.query(CampaignSendAttempt.id).filter(
        CampaignSendAttempt.campaign_id == campaign_id,
        CampaignSendAttempt.state.in_((ledger.ATTEMPT_CLAIMED, ledger.ATTEMPT_REQUEST_STARTED)),
    ).first():
        raise ValueError("attempt_in_flight")
    # Retain the real pause age, not an invented immediate eligibility time.
    wait = ledger.record_marketing_wait(c, now=ledger._naive(lease.paused_at))
    report = {"action": "adopted" if apply else "would_adopt", "tenant_id": tenant_id,
              "campaign_id": campaign_id, "wait": wait}
    if apply:
        db.commit()
    else:
        db.rollback()
    return report


def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--tenant-id", required=True, type=int)
    args.add_argument("--campaign-id", required=True, type=int)
    args.add_argument("--expected-paused-at", required=True)
    args.add_argument("--apply", action="store_true")
    a = args.parse_args()
    url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    engine = create_engine(url, connect_args={"connect_timeout": 10, "options": "-c statement_timeout=15000"})
    with Session(engine) as db:
        result = adopt(db, tenant_id=a.tenant_id, campaign_id=a.campaign_id,
                       expected_paused_at=datetime.fromisoformat(a.expected_paused_at), apply=a.apply)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
