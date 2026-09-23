#!/usr/bin/env python3
"""Read-only reconciliation of one campaign's sends, per message and per recipient.

Why this exists
───────────────
Before the attempt ledger (``campaign_send_attempts``), a recipient row in
``campaign_send_logs`` held ONE ``provider_message_id``. When two dispatchers
ran concurrently, Meta accepted two copies and the second wamid overwrote the
first on that row, so the recipient row alone cannot say how many copies a
customer got or which one was delivered. The copies are still recoverable from
evidence that kept every wamid:

* ``message_events`` — one outbound row per accepted copy
  (``metadata.wa_message_id``) with the webhook receipt flags
  (``_status_delivered`` / ``_status_read`` / ``_status_failed``,
  ``delivery_status``, ``delivery_error``);
* ``campaign_send_attempts`` — every attempt made after this fix;
* runtime logs — ``campaign=<id> sent OK to <phone> wamid=<id>`` for every
  accepted copy and ``status_failed wamid=… recipient_id=…`` for every
  post-accept failure (Railway ``get-logs`` JSON exports).

Nothing here writes. With ``--database-url`` the session is opened
``READ ONLY``; with log files only local files are read.

Categories (per recipient; every message count is reported separately)
──────────────────────────────────────────────────────────────────────
  delivered_once              exactly one copy delivered/read, every other copy failed
  delivered_multiple          two or more copies delivered/read
  accepted_multiple_unproven  two or more accepted copies, fewer than two proven delivered
  all_failed                  every attempt failed (before or after acceptance)
  uncertain                   an attempt whose outcome is unknown (accepted with no
                              receipt yet, or left in ``sending`` / ``uncertain``)
  not_started                 no attempt was ever made
  excluded                    skipped before sending (cap, opt-out, manual exclusion…)

"Read" counts as delivered even without a separate delivered receipt. The
absence of a receipt never counts as "not delivered". Log-only runs cannot see
delivery receipts, so they report ``delivery_evidence: unavailable`` and never
fill the two delivered categories.

Usage
─────
  # Database (read-only) — the authoritative run:
  DATABASE_URL=… python scripts/operators/campaign_send_reconciliation.py \\
      --tenant-id T --campaign-id C --out report.json

  # Logs only (what an operator without DB access can prove):
  python scripts/operators/campaign_send_reconciliation.py \\
      --tenant-id T --campaign-id C --log-json exports/*.json \\
      --failed-tsv extra_failed.tsv --out report.json
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

CATEGORIES = (
    "delivered_once", "delivered_multiple", "accepted_multiple_unproven",
    "all_failed", "uncertain", "not_started", "excluded",
)


@dataclass
class Copy:
    wamid: str
    delivered: bool = False
    read: bool = False
    failed: bool = False
    failed_reason: Optional[str] = None
    accepted_at: Optional[str] = None

    @property
    def has_delivery(self) -> bool:
        return self.delivered or self.read


@dataclass
class Recipient:
    phone: str
    copies: Dict[str, Copy] = field(default_factory=dict)
    log_status: Optional[str] = None
    log_attempts: int = 0
    pre_accept_failures: int = 0
    uncertain_attempts: int = 0

    def copy(self, wamid: str) -> Copy:
        c = self.copies.get(wamid)
        if c is None:
            c = self.copies[wamid] = Copy(wamid=wamid)
        return c


def classify(r: Recipient, *, delivery_evidence: bool) -> str:
    copies = list(r.copies.values())
    status = (r.log_status or "").lower()
    if not copies:
        if status.startswith("skipped_"):
            return "excluded"
        if status in ("sending", "uncertain") or r.uncertain_attempts:
            return "uncertain"
        if status == "failed" or r.pre_accept_failures:
            return "all_failed"
        if status in ("", "queued") and r.log_attempts == 0:
            return "not_started"
        return "uncertain"
    if r.uncertain_attempts or status in ("sending", "uncertain"):
        # An attempt beyond the known copies may exist.
        return "uncertain" if len(copies) < 2 else "accepted_multiple_unproven"
    delivered = [c for c in copies if c.has_delivery]
    if len(delivered) >= 2:
        return "delivered_multiple"
    if all(c.failed and not c.has_delivery for c in copies):
        return "all_failed"
    if len(copies) >= 2:
        if len(delivered) == 1 and all(c.failed for c in copies if not c.has_delivery):
            return "delivered_once"
        return "accepted_multiple_unproven"
    # exactly one copy
    if delivered:
        return "delivered_once"
    if not delivery_evidence:
        return "uncertain"
    return "uncertain"


def mask(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    h = hashlib.sha256(digits.encode()).hexdigest()[:10]
    return f"•••{digits[-4:]}#{h}"


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return f"+{d}" if d else ""


# ── Log evidence ─────────────────────────────────────────────────────────

_SENT_RE = re.compile(r"campaign=(\d+) sent OK to (\+?\d+) wamid=(\S+)")
_META_ERR_RE = re.compile(r"campaign=(\d+) Meta error key=(\S+) .*? phone=(\+?\d+)")
_EXC_RE = re.compile(r"campaign=(\d+) exception sending to (\+?\d+)")
_FAILED_RE = re.compile(
    r"status_failed wamid=(\S+) status=failed recipient_id=(\d+) .*?tenant_id=(\d+)"
    r".*?errors=\[\{.*?'title': ['\"](.*?)['\"],"
)
_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")


def iter_log_messages(paths: Iterable[str]) -> Iterable[str]:
    seen = set()
    for pattern in paths:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for entry in data.get("deploy", []) if isinstance(data, dict) else []:
                msg = entry.get("message") or ""
                key = (entry.get("timestamp"), msg)
                if key in seen:
                    continue
                seen.add(key)
                yield msg


def apply_logs(recipients: Dict[str, Recipient], *, campaign_id: int, tenant_id: int,
               log_paths: List[str], failed_tsv: List[str]) -> Dict[str, Any]:
    meta = {"accepted_lines": 0, "failed_events": 0, "pre_accept_errors": 0,
            "exceptions": 0, "first_accept": None, "last_accept": None,
            "failed_events_unmatched_wamid": 0}
    failed_events: List[tuple] = []
    for msg in iter_log_messages(log_paths):
        ts = (_TS_RE.match(msg) or [None, None])[1]
        m = _SENT_RE.search(msg)
        if m and int(m.group(1)) == campaign_id:
            phone, wamid = norm_phone(m.group(2)), m.group(3)
            r = recipients.setdefault(phone, Recipient(phone))
            if wamid not in r.copies:
                meta["accepted_lines"] += 1
            r.copy(wamid).accepted_at = ts
            if ts:
                meta["first_accept"] = min(filter(None, [meta["first_accept"], ts]))
                meta["last_accept"] = max(filter(None, [meta["last_accept"], ts]))
            continue
        m = _META_ERR_RE.search(msg)
        if m and int(m.group(1)) == campaign_id:
            recipients.setdefault(norm_phone(m.group(3)), Recipient(norm_phone(m.group(3)))) \
                .pre_accept_failures += 1
            meta["pre_accept_errors"] += 1
            continue
        m = _EXC_RE.search(msg)
        if m and int(m.group(1)) == campaign_id:
            recipients.setdefault(norm_phone(m.group(2)), Recipient(norm_phone(m.group(2)))) \
                .uncertain_attempts += 1
            meta["exceptions"] += 1
            continue
        m = _FAILED_RE.search(msg)
        if m and int(m.group(3)) == tenant_id:
            failed_events.append((m.group(1), norm_phone(m.group(2)), m.group(4)))
    for path in failed_tsv:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4:
                    failed_events.append((parts[2], norm_phone(parts[1]), parts[3]))
    seen = set()
    for wamid, phone, reason in failed_events:
        if wamid in seen:
            continue
        seen.add(wamid)
        r = recipients.get(phone)
        if r is None or wamid not in r.copies:
            # A failure for a wamid this campaign's accept lines never
            # showed (another campaign/automation, or outside the export).
            meta["failed_events_unmatched_wamid"] += 1
            continue
        c = r.copies[wamid]
        c.failed = True
        c.failed_reason = reason
        meta["failed_events"] += 1
    return meta


# ── Database evidence (read-only) ────────────────────────────────────────


def apply_database(recipients: Dict[str, Recipient], *, url: str, campaign_id: int,
                   tenant_id: int) -> Dict[str, Any]:
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(url)
    meta: Dict[str, Any] = {}
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        owner = conn.execute(
            text("SELECT tenant_id FROM campaigns WHERE id = :c"), {"c": campaign_id},
        ).scalar()
        if owner is None or int(owner) != tenant_id:
            raise SystemExit(f"campaign {campaign_id} does not belong to tenant {tenant_id}")
        rows = conn.execute(text(
            "SELECT customer_phone_e164, status, attempt_count, provider_message_id "
            "FROM campaign_send_logs WHERE tenant_id = :t AND campaign_id = :c"
        ), {"t": tenant_id, "c": campaign_id}).all()
        meta["send_log_rows"] = len(rows)
        for phone, status, attempts, wamid in rows:
            r = recipients.setdefault(norm_phone(phone), Recipient(norm_phone(phone)))
            r.log_status = status
            r.log_attempts = int(attempts or 0)
            if status == "failed" and not wamid:
                r.pre_accept_failures = max(r.pre_accept_failures, 1)
            if wamid:
                r.copy(wamid)
        events = conn.execute(text(
            "SELECT metadata FROM message_events WHERE tenant_id = :t "
            "AND direction = 'outbound' AND event_type = 'campaign' "
            "AND (metadata->>'campaign_id') = :c"
        ), {"t": tenant_id, "c": str(campaign_id)}).all()
        meta["message_event_rows"] = len(events)
        # Phone lives on the conversation; the wamid encodes it too, but
        # the send-log / attempt rows are the reliable join.
        wamid_to_phone = {w: p for p, r in recipients.items() for w in r.copies}
        try:
            for phone, wamid, state, dl, rd, fl, err in conn.execute(text(
                "SELECT customer_phone_e164, provider_message_id, state, delivered_at, "
                "read_at, failed_at, post_accept_error_code FROM campaign_send_attempts "
                "WHERE tenant_id = :t AND campaign_id = :c"
            ), {"t": tenant_id, "c": campaign_id}):
                r = recipients.setdefault(norm_phone(phone), Recipient(norm_phone(phone)))
                if state == "uncertain" or state in ("claimed", "request_started"):
                    r.uncertain_attempts += 1
                if wamid:
                    c = r.copy(wamid)
                    c.delivered |= dl is not None
                    c.read |= rd is not None
                    c.failed |= fl is not None
                    c.failed_reason = c.failed_reason or err
                    wamid_to_phone[wamid] = norm_phone(phone)
        except Exception:  # noqa: silent-ok — ledger table absent before this deploy
            conn.rollback()
            conn.execute(text("SET TRANSACTION READ ONLY"))
        unmatched = 0
        for (md,) in events:
            md = md or {}
            wamid = md.get("wa_message_id") or (md.get("provider_send") or {}).get("wamid")
            if not wamid:
                continue
            phone = wamid_to_phone.get(wamid) or _phone_from_wamid(wamid)
            if not phone:
                unmatched += 1
                continue
            r = recipients.setdefault(phone, Recipient(phone))
            c = r.copy(wamid)
            c.read |= bool(md.get("_status_read"))
            c.delivered |= bool(md.get("_status_delivered")) or c.read
            if md.get("_status_failed"):
                c.failed = True
                c.failed_reason = c.failed_reason or str(md.get("delivery_error") or "")[:120]
        meta["message_events_unmatched"] = unmatched
    engine.dispose()
    return meta


def _phone_from_wamid(wamid: str) -> Optional[str]:
    """Cloud API wamids embed the recipient: base64 of a small protobuf
    whose first field is the phone digits."""
    import base64  # noqa: PLC0415
    try:
        raw = wamid.split(".", 1)[1]
        blob = base64.b64decode(raw + "=" * (-len(raw) % 4))
        n = blob[2]
        digits = blob[3:3 + n].decode()
        return norm_phone(digits) if digits.isdigit() else None
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


# ── Report ───────────────────────────────────────────────────────────────


def build_report(recipients: Dict[str, Recipient], *, delivery_evidence: bool,
                 sources: Dict[str, Any], emit_recipients: bool) -> Dict[str, Any]:
    counts = {k: 0 for k in CATEGORIES}
    messages = {"accepted": 0, "delivered_or_read": 0, "read": 0,
                "failed_after_accept": 0, "no_receipt": 0}
    per = []
    for phone, r in recipients.items():
        cat = classify(r, delivery_evidence=delivery_evidence)
        counts[cat] += 1
        for c in r.copies.values():
            messages["accepted"] += 1
            if c.has_delivery:
                messages["delivered_or_read"] += 1
            if c.read:
                messages["read"] += 1
            if c.failed and not c.has_delivery:
                messages["failed_after_accept"] += 1
            if not c.has_delivery and not c.failed:
                messages["no_receipt"] += 1
        if emit_recipients:
            per.append({"recipient": mask(phone), "category": cat, "copies": len(r.copies),
                        "failed_reasons": sorted({c.failed_reason for c in r.copies.values()
                                                  if c.failed_reason})})
    accepted_hist: Dict[int, int] = defaultdict(int)
    for r in recipients.values():
        if r.copies:
            accepted_hist[len(r.copies)] += 1
    reasons: Dict[str, int] = defaultdict(int)
    for r in recipients.values():
        for c in r.copies.values():
            if c.failed:
                reasons[c.failed_reason or "unknown"] += 1
    return {
        "delivery_evidence": "available" if delivery_evidence else "unavailable",
        "recipients_total": len(recipients),
        "recipients_by_category": counts,
        "recipients_by_accepted_copies": dict(sorted(accepted_hist.items())),
        "messages": messages,
        "failed_after_accept_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "sources": sources,
        **({"recipients": per} if emit_recipients else {}),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant-id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--no-database", action="store_true")
    ap.add_argument("--log-json", nargs="*", default=[])
    ap.add_argument("--failed-tsv", nargs="*", default=[])
    ap.add_argument("--emit-recipients", action="store_true",
                    help="include a masked per-recipient list")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    recipients: Dict[str, Recipient] = {}
    sources: Dict[str, Any] = {}
    use_db = bool(args.database_url) and not args.no_database
    if use_db:
        sources["database"] = apply_database(
            recipients, url=args.database_url,
            campaign_id=args.campaign_id, tenant_id=args.tenant_id,
        )
    if args.log_json or args.failed_tsv:
        sources["logs"] = apply_logs(
            recipients, campaign_id=args.campaign_id, tenant_id=args.tenant_id,
            log_paths=args.log_json, failed_tsv=args.failed_tsv,
        )
    if not sources:
        ap.error("give --database-url/DATABASE_URL or --log-json files")
    report = build_report(recipients, delivery_evidence=use_db, sources=sources,
                          emit_recipients=args.emit_recipients)
    text_out = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text_out)
    print(text_out if not args.emit_recipients else json.dumps(
        {k: v for k, v in report.items() if k != "recipients"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
