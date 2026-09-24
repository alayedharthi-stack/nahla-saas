#!/usr/bin/env python3
"""Hold every ``queued`` send-log row the reconciliation does not approve
for a new send, so the dispatcher cannot claim it.

Why this exists
───────────────
Before a stopped campaign is resumed, every row the dispatcher can claim
(``status='queued'``) must be a recipient the reconciliation classifies as
``not_started_new_send_decision``: no attempt, no accepted copy, no
unresolved evidence. A queued row that the reconciliation places anywhere
else (a possible earlier attempt, an accepted copy, a delivery, a pending
receipt …) must not be sent again. This tool moves exactly those rows to
the existing terminal hold ``status='uncertain'`` /
``error_code='send_outcome_unknown'`` — the state the ledger already uses
for "the outcome is unknown, never re-send automatically". Nothing moves
rows out of that state automatically.

It never touches a row that is not ``queued``, and never touches a queued
row the reconciliation approves.

Safety
──────
* Default is a dry run: one ``REPEATABLE READ, READ ONLY`` reconciliation
  snapshot (the same code as ``campaign_send_reconciliation.py``), then the
  list of rows it would hold (recipients masked). Nothing is written.
* ``--apply`` additionally requires ``--expect N`` (the count the dry run
  reported and a person reviewed) and the environment variable
  ``NAHLA_QUARANTINE_CONFIRM=campaign-<id>``. The write runs in ONE
  transaction that locks the campaign's lease row and the target rows,
  re-checks that the lease is not live, that no attempt is in flight and
  that the locked queued rows are exactly the reviewed set, updates them,
  and commits — or changes nothing.
* Pre-write gate (from the read-only snapshot): the reconciliation is
  ``decision_eligible``, the schema preflight passed, the campaign is not
  ``active``, no live lease, no attempt in flight.
* After the write the reconciliation runs again and the final gate is
  printed: ``decision_eligible``, ``queued == not_started_new_send_decision``,
  lease, ``attempts_in_flight`` and ``retry_review_candidate``.

The exit code is 0 only when the final gate passes (dry run: when the
pre-write gate passes). Run it where ``DATABASE_URL`` is a reference, never
printed; do not set ``PGOPTIONS=-c default_transaction_read_only=on`` for
``--apply`` (the write transaction needs it off; every read is still
read-only by construction).

Usage
─────
  DATABASE_URL=… python scripts/operators/campaign_quarantine_unapproved_queued.py \\
      --tenant-id T --campaign-id C                       # dry run
  NAHLA_QUARANTINE_CONFIRM=campaign-C DATABASE_URL=… python … \\
      --tenant-id T --campaign-id C --apply --expect N    # hold exactly N rows
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REC_PATH = Path(__file__).with_name("campaign_send_reconciliation.py")
_spec = importlib.util.spec_from_file_location("campaign_send_reconciliation", _REC_PATH)
rec = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, rec)
_spec.loader.exec_module(rec)

APPROVED = "not_started_new_send_decision"
HOLD_STATUS = "uncertain"
HOLD_CODE = "send_outcome_unknown"
HOLD_MESSAGE = ("[reconciliation_hold] queued row not approved for a new send by the "
                "campaign reconciliation ({category} / {proposal}); held, never re-sent "
                "automatically")


def reconcile(url: str, *, tenant_id: int, campaign_id: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """One read-only reconciliation snapshot. Returns (summary, per_phone)
    where per_phone maps the (unmasked, in-memory only) phone to its
    category, proposal and history."""
    recipients: Dict[str, Any] = {}
    meta = rec.apply_database(recipients, url=url, campaign_id=campaign_id,
                              tenant_id=tenant_id, check_schema=True)
    meta.pop("_wamids_read", None)
    reasons: List[str] = []
    statuses = {n: v.get("status") for n, v in meta["sources"].items()}
    if not all(s == "complete" for s in statuses.values()):
        reasons.append(f"sources not complete: {statuses}")
    if not meta["attribution"]["complete"]:
        reasons.append(f"attribution incomplete: {meta['attribution']['blocking']}")
    pre = meta.get("preflight") or {}
    if not pre.get("schema_ok"):
        reasons.append("schema preflight failed")
    report = rec.build_report(recipients, delivery_evidence=not reasons, sources={},
                              emit_recipients=False, decision_eligible=not reasons,
                              ineligible_reasons=reasons)
    per_phone: Dict[str, Any] = {}
    for phone, r in recipients.items():
        cat = rec.classify(r, delivery_evidence=True)
        hist = rec.attempt_history(r)
        per_phone[phone] = {
            "log_status": r.log_status, "category": cat,
            "proposal": rec.resend_proposal(r, cat),
            "log_attempts": r.log_attempts, "copies": len(r.copies),
            "delivered_copies": r.delivered_copies(),
            "has_proven_delivery": r.has_proven_delivery(),
            "unknown_attempts": hist.unknown_attempts,
            "history_reasons": hist.reasons,
            "ledger_states": sorted(a.state for a in r.ledger.values()),
        }
    summary = {
        "decision_eligible": report["decision_eligible"],
        "ineligible_reasons": report["ineligible_reasons"],
        "recipients_total": report["recipients_total"],
        "recipients_by_category": report["recipients_by_category"],
        "resend_proposal": report["resend_proposal"],
        "recipients_with_proven_delivery": report["recipients_with_proven_delivery"],
        "unknown_attempts_total": report["unknown_attempts_total"],
        "messages": report["messages"],
        "attribution": meta["attribution"],
        "preflight": pre,
    }
    return summary, per_phone


def unapproved_queued(per_phone: Dict[str, Any]) -> Dict[str, Any]:
    return {p: v for p, v in per_phone.items()
            if (v["log_status"] or "").lower() == "queued" and v["proposal"] != APPROVED}


def gate(summary: Dict[str, Any], *, final: bool) -> Tuple[bool, List[str]]:
    pre = summary["preflight"]
    counts = pre.get("send_log_status_counts") or {}
    lease = pre.get("lease")
    fails: List[str] = []
    if not summary["decision_eligible"]:
        fails.append("decision_eligible is false")
    if lease not in ("none", None) and (lease.get("live") or lease.get("has_owner")):
        fails.append(f"lease is held: {lease}")
    if (pre.get("attempts_in_flight") or 0) != 0:
        fails.append(f"attempts_in_flight={pre.get('attempts_in_flight')}")
    if ((pre.get("campaign") or {}).get("status") or "").lower() == "active":
        fails.append("campaign status is active (stop it before any repair)")
    if final:
        queued = int(counts.get("queued", 0))
        approved = int(summary["resend_proposal"].get(APPROVED, 0))
        if queued != approved:
            fails.append(f"queued ({queued}) != {APPROVED} ({approved})")
        if summary["resend_proposal"].get("retry_review_candidate", 0):
            fails.append("retry_review_candidate > 0 (needs a separate decision)")
    return (not fails), fails


def _public(summary: Dict[str, Any]) -> Dict[str, Any]:
    pre = summary["preflight"]
    return {**{k: v for k, v in summary.items() if k != "preflight"},
            "campaign_status": (pre.get("campaign") or {}).get("status"),
            "send_log_status_counts": pre.get("send_log_status_counts"),
            "lease": pre.get("lease"), "attempts_in_flight": pre.get("attempts_in_flight"),
            "schema_ok": pre.get("schema_ok")}


def _masked_rows(targets: Dict[str, Any]) -> List[Dict[str, Any]]:
    return sorted(({"recipient": rec.mask(p), **v} for p, v in targets.items()),
                  key=lambda x: (x["category"], x["recipient"]))


def apply_hold(url: str, *, tenant_id: int, campaign_id: int,
               targets: Dict[str, Any]) -> int:
    """Hold exactly ``targets`` in one transaction, or change nothing."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            lease = conn.execute(text(
                "SELECT owner IS NOT NULL, expires_at > (now() AT TIME ZONE 'utc') "
                "FROM campaign_dispatch_leases WHERE campaign_id = :c FOR UPDATE"),
                {"c": campaign_id}).first()
            if lease is not None and (lease[0] or lease[1]):
                raise RuntimeError("campaign lease is held; nothing changed")
            in_flight = conn.execute(text(
                "SELECT count(*) FROM campaign_send_attempts WHERE campaign_id = :c "
                "AND state IN ('claimed', 'request_started')"), {"c": campaign_id}).scalar()
            if in_flight:
                raise RuntimeError(f"{in_flight} attempts in flight; nothing changed")
            status = conn.execute(text(
                "SELECT status FROM campaigns WHERE id = :c AND tenant_id = :t FOR UPDATE"),
                {"c": campaign_id, "t": tenant_id}).scalar()
            if (status or "").lower() == "active":
                raise RuntimeError("campaign is active; nothing changed")
            rows = conn.execute(text(
                "SELECT id, customer_phone_e164 FROM campaign_send_logs "
                "WHERE tenant_id = :t AND campaign_id = :c AND status = 'queued' FOR UPDATE"),
                {"t": tenant_id, "c": campaign_id}).all()
            ids = [int(i) for i, ph in rows if rec.norm_phone(ph) in targets]
            matched = {rec.norm_phone(ph) for i, ph in rows if rec.norm_phone(ph) in targets}
            if len(ids) != len(targets) or matched != set(targets):
                raise RuntimeError(
                    f"locked queued rows ({len(ids)}) do not match the reviewed set "
                    f"({len(targets)}); nothing changed")
            for i, ph in rows:
                p = rec.norm_phone(ph)
                if p not in targets:
                    continue
                v = targets[p]
                conn.execute(text(
                    "UPDATE campaign_send_logs SET status = :s, error_code = :e, "
                    "error_message = :m, updated_at = (now() AT TIME ZONE 'utc') "
                    "WHERE id = :i AND status = 'queued'"),
                    {"s": HOLD_STATUS, "e": HOLD_CODE, "i": int(i),
                     "m": HOLD_MESSAGE.format(category=v["category"], proposal=v["proposal"])})
            return len(ids)
    finally:
        engine.dispose()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant-id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--expect", type=int)
    args = ap.parse_args(argv)
    if not args.database_url:
        ap.error("DATABASE_URL is required")

    out: Dict[str, Any] = {"mode": "apply" if args.apply else "dry_run"}
    try:
        before, per_phone = reconcile(args.database_url, tenant_id=args.tenant_id,
                                      campaign_id=args.campaign_id)
    except rec.SourceError as exc:
        print(json.dumps({"error": "reconciliation incomplete", "detail": str(exc)[:2000]}))
        return 2
    targets = unapproved_queued(per_phone)
    ok, fails = gate(before, final=False)
    out["before"] = _public(before)
    out["unapproved_queued_count"] = len(targets)
    out["unapproved_queued"] = _masked_rows(targets)
    out["pre_write_gate"] = {"passed": ok, "failures": fails}

    if not args.apply:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if ok else 3
    if not ok:
        out["result"] = "refused: pre-write gate failed; nothing changed"
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 3
    if args.expect is None or args.expect != len(targets):
        out["result"] = (f"refused: --expect {args.expect} does not match the "
                         f"{len(targets)} rows found; nothing changed")
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 3
    if os.environ.get("NAHLA_QUARANTINE_CONFIRM") != f"campaign-{args.campaign_id}":
        out["result"] = "refused: NAHLA_QUARANTINE_CONFIRM not set for this campaign"
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 3
    try:
        out["held"] = apply_hold(args.database_url, tenant_id=args.tenant_id,
                                 campaign_id=args.campaign_id, targets=targets) if targets else 0
    except RuntimeError as exc:
        out["result"] = f"refused: {exc}"
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 3

    after, per_phone_after = reconcile(args.database_url, tenant_id=args.tenant_id,
                                       campaign_id=args.campaign_id)
    ok, fails = gate(after, final=True)
    out["after"] = _public(after)
    out["remaining_unapproved_queued"] = len(unapproved_queued(per_phone_after))
    out["final_gate"] = {"passed": ok, "failures": fails}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
