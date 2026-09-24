#!/usr/bin/env python3
"""Read-only RCA of the sends a campaign made in one time window — built for
the 2026-09-24 incident where campaign 35 was auto-resumed at 13:47:43Z and
~99 messages were accepted before ``provider_throttling`` paused it.

For every ledger attempt claimed in ``[--since, --until]`` it reports:

* the new sends: attempts, distinct send-log rows and recipients, their
  ledger states, accepted / wamid / delivered / read, and post-accept
  failures by error key (e.g. ``marketing_blocked`` = Meta 131049);
* the intersection with ``--unapproved-send-log-ids`` — the rows the
  reconciliation did not approve, as listed (``planned_changes``) by the
  quarantine dry run that ran BEFORE the window; matched by send-log id and
  by recipient, since a recipient can have more than one row;
* for each new recipient, the strongest evidence that existed BEFORE
  ``--since``: an earlier ledger attempt (any campaign of the tenant) that
  was accepted / delivered / read / uncertain / in flight, or another row
  of this campaign that was sent / delivered / read / uncertain or carries
  a wamid. Recipients with none are ``clean``.

Evidence scanned: ``campaign_send_attempts`` and ``campaign_send_logs``.
``message_events`` (the overwritten first copies the reconciliation also
uses) is NOT scanned here; the unapproved-ids intersection covers those.

Read-only: one ``REPEATABLE READ, READ ONLY`` transaction; nothing is added,
updated or deleted. No phone number is printed — only internal ids and
counts. ``DATABASE_URL`` is never echoed.

    python scripts/operators/campaign_auto_resume_incident_rca.py \\
        --tenant-id 33 --campaign-id 35 \\
        --since 2026-09-24T13:47:43Z --until 2026-09-24T13:53:05Z \\
        --unapproved-send-log-ids 101,102,...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

EVIDENCE_ORDER = ("read", "delivered", "accepted", "uncertain", "in_flight", "sent_row", "wamid_row")
_NORM = "regexp_replace(customer_phone_e164, '[^0-9]', '', 'g')"


def _ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _ids(raw: Optional[str]) -> List[int]:
    return sorted({int(x) for x in (raw or "").replace(" ", "").split(",") if x})


def _strongest(kinds: Sequence[str]) -> str:
    for k in EVIDENCE_ORDER:
        if k in kinds:
            return k
    return "clean"


def analyse(conn: Any, *, tenant_id: int, campaign_id: int, since: datetime, until: datetime,
            unapproved_ids: Sequence[int]) -> Dict[str, Any]:
    from sqlalchemy import text  # noqa: PLC0415
    q = lambda sql, **p: conn.execute(text(sql), p).all()  # noqa: E731
    p = {"t": tenant_id, "c": campaign_id, "s": since, "u": until}

    attempts = q(
        f"SELECT id, send_log_id, {_NORM}, state, provider_message_id IS NOT NULL, "
        "accepted_at IS NOT NULL, delivered_at IS NOT NULL, read_at IS NOT NULL, "
        "failed_at IS NOT NULL, post_accept_error_code, error_code, messaging_scope_key "
        "FROM campaign_send_attempts WHERE tenant_id = :t AND campaign_id = :c "
        "AND claimed_at >= :s AND claimed_at <= :u ORDER BY id", **p)
    log_ids = sorted({int(a[1]) for a in attempts if a[1] is not None})
    phones = sorted({a[2] for a in attempts if a[2]})
    by_phone_logs: Dict[str, set] = {}
    for a in attempts:
        if a[2] and a[1] is not None:
            by_phone_logs.setdefault(a[2], set()).add(int(a[1]))

    new = {
        "attempts": len(attempts),
        "send_log_rows": len(log_ids),
        "recipients": len(phones),
        "by_state": dict(Counter(a[3] for a in attempts)),
        "accepted": sum(1 for a in attempts if a[5]),
        "with_wamid": sum(1 for a in attempts if a[4]),
        "delivered": sum(1 for a in attempts if a[6]),
        "read": sum(1 for a in attempts if a[7]),
        "failed_after_accept": sum(1 for a in attempts if a[5] and a[8]),
        "post_accept_error_keys": dict(Counter(a[9] for a in attempts if a[9])),
        "pre_accept_error_keys": dict(Counter(a[10] for a in attempts if a[10] and not a[5])),
        "scope_keys": dict(Counter((a[11] or "").split(":", 1)[0] + ":…" for a in attempts)),
        "recipients_with_more_than_one_attempt": sum(
            1 for n in Counter(a[2] for a in attempts).values() if n > 1),
        "send_log_ids": log_ids,
    }

    # ── the unapproved rows (as listed before the window) ───────────────
    unapproved = sorted(set(unapproved_ids))
    un_phones: Dict[int, str] = {}
    if unapproved:
        for i, ph in q(f"SELECT id, {_NORM} FROM campaign_send_logs WHERE tenant_id = :t "
                       "AND campaign_id = :c AND id = ANY(:ids)", ids=unapproved, **p):
            un_phones[int(i)] = ph
    un_phone_set = set(un_phones.values())
    hit_by_id = sorted(set(log_ids) & set(unapproved))
    hit_by_phone = sorted(i for i, ph in un_phones.items() if ph in set(phones))
    intersection = {
        "unapproved_listed": len(unapproved),
        "unapproved_found_in_campaign": len(un_phones),
        "sent_rows_that_are_unapproved": len(hit_by_id),
        "unapproved_rows_whose_recipient_was_sent": len(hit_by_phone),
        "unapproved_ids_hit_by_row": hit_by_id,
        "unapproved_ids_hit_by_recipient": hit_by_phone,
        "new_send_log_ids_for_those_recipients": sorted(
            {i for ph in un_phone_set if ph in by_phone_logs for i in by_phone_logs[ph]}),
    }

    # ── evidence that existed before the window, per new recipient ──────
    evidence: Dict[str, set] = {ph: set() for ph in phones}
    if phones:
        for ph, st, dl, rd, acc in q(
                f"SELECT {_NORM}, state, delivered_at IS NOT NULL, read_at IS NOT NULL, "
                "accepted_at IS NOT NULL FROM campaign_send_attempts WHERE tenant_id = :t "
                f"AND claimed_at < :s AND {_NORM} = ANY(:ph)", ph=phones, **p):
            ks = evidence[ph]
            if rd:
                ks.add("read")
            if dl:
                ks.add("delivered")
            if acc or st == "accepted":
                ks.add("accepted")
            if st == "uncertain":
                ks.add("uncertain")
            if st in ("claimed", "request_started"):
                ks.add("in_flight")
        for i, ph, st, wamid, dl, rd, sent_before in q(
                f"SELECT id, {_NORM}, status, provider_message_id IS NOT NULL, "
                "delivered_at IS NOT NULL AND delivered_at < :s, read_at IS NOT NULL AND read_at < :s, "
                "sent_at IS NOT NULL AND sent_at < :s FROM campaign_send_logs "
                f"WHERE tenant_id = :t AND campaign_id = :c AND {_NORM} = ANY(:ph)", ph=phones, **p):
            ks = evidence[ph]
            if int(i) in by_phone_logs.get(ph, set()):
                # The row sent in the window: only what predates the window counts.
                if rd:
                    ks.add("read")
                if dl:
                    ks.add("delivered")
                if sent_before:
                    ks.add("sent_row")
                continue
            if rd or (st or "").lower() == "read":
                ks.add("read")
            if dl or (st or "").lower() == "delivered":
                ks.add("delivered")
            if (st or "").lower() == "uncertain":
                ks.add("uncertain")
            if sent_before or (st or "").lower() == "sent":
                ks.add("sent_row")
            if wamid:
                ks.add("wamid_row")
    strongest = {ph: _strongest(sorted(ks)) for ph, ks in evidence.items()}
    history = {
        "by_strongest_evidence": dict(Counter(strongest.values())),
        "send_log_ids_by_evidence": {
            k: sorted(i for ph, s in strongest.items() if s == k for i in by_phone_logs.get(ph, ()))
            for k in EVIDENCE_ORDER if any(s == k for s in strongest.values())
        },
        "scanned": ["campaign_send_attempts (tenant, before --since)",
                    "campaign_send_logs (this campaign)"],
        "not_scanned": ["message_events"],
    }

    counts = dict(q("SELECT status, count(*) FROM campaign_send_logs WHERE tenant_id = :t "
                    "AND campaign_id = :c GROUP BY status", **p))
    lease = q("SELECT pause_reason, stop_requested_at IS NOT NULL, owner IS NOT NULL, paused_at "
              "FROM campaign_dispatch_leases WHERE campaign_id = :c", **p)
    return {
        "window": {"since": since.isoformat() + "Z", "until": until.isoformat() + "Z"},
        "new_sends": new,
        "unapproved_intersection": intersection,
        "history_before_window": history,
        "campaign_now": {
            "send_log_status_counts": {k: int(v) for k, v in counts.items()},
            "lease": ({"pause_reason": lease[0][0], "stop_requested": bool(lease[0][1]),
                       "has_owner": bool(lease[0][2]),
                       "paused_at": lease[0][3].isoformat() if lease[0][3] else None}
                      if lease else None),
        },
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant-id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--unapproved-send-log-ids", default="",
                    help="comma-separated send_log_id list from the pre-window dry run")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = ap.parse_args(argv)
    if not args.database_url:
        print(json.dumps({"error": "DATABASE_URL not set"}))
        return 2
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    engine = create_engine(args.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            out = analyse(conn, tenant_id=args.tenant_id, campaign_id=args.campaign_id,
                          since=_ts(args.since), until=_ts(args.until),
                          unapproved_ids=_ids(args.unapproved_send_log_ids))
            conn.rollback()
    finally:
        engine.dispose()
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
