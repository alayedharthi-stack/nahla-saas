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

A failure is only "all_failed" when it is *proven*: an explicit rejection
code, and a known attempt history that covers every attempt the row
counted. The pre-ledger dispatcher overwrote a row's error on each retry
and wrote ambiguous codes (``watchdog_timeout``, ``exception``,
``no_message_id``) for requests that may have been accepted — those stay
``uncertain``.

Every recipient also carries ``has_proven_delivery`` / ``delivered_copies``,
independent of its category, and a ``resend_proposal``. A recipient with any
delivered/read copy is excluded from a resend whatever its other copies say.
The proposal is input to a human decision, never an authorisation.

Sources and completeness (database mode)
────────────────────────────────────────
All reads happen in one ``REPEATABLE READ, READ ONLY`` transaction (one
consistent snapshot). Each source is reported as ``complete``, ``absent``
or ``error``. A missing ledger table is only tolerated with
``--allow-missing-ledger`` (a database from before the ledger deploy); any
other read failure — permissions, schema drift, a dropped connection —
aborts the run with exit code 2. ``decision_eligible`` is true only when
every source is complete and the schema preflight passed; log-only runs are
never decision-eligible.

Usage
─────
  # Database (read-only) — the authoritative run:
  DATABASE_URL=… python scripts/operators/campaign_send_reconciliation.py \\
      --tenant-id T --campaign-id C --out report.json

  # Plus the post-deploy preflight (ledger tables/indexes, campaign status,
  # lease, in-flight attempts) in the same read-only snapshot:
  DATABASE_URL=… python scripts/operators/campaign_send_reconciliation.py \\
      --tenant-id T --campaign-id C --check-schema --emit-recipients --out report.json

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
    # Count of pre-accept failure observations and the codes they carried
    # (explicit Meta rejections or the legacy dispatcher's stored code).
    pre_accept_failures: int = 0
    pre_accept_codes: List[str] = field(default_factory=list)
    uncertain_attempts: int = 0
    # Attempt-ledger rows that prove no message left (rejected / not_sent /
    # abandoned). They resolve legacy ambiguity for the attempts they cover.
    ledger_unsent_attempts: int = 0

    def copy(self, wamid: str) -> Copy:
        c = self.copies.get(wamid)
        if c is None:
            c = self.copies[wamid] = Copy(wamid=wamid)
        return c


# Codes the pre-ledger dispatcher stored when the request may have been
# accepted (or when nothing identifies what happened). Never proof of a
# failure on their own.
AMBIGUOUS_FAILURE_CODES = frozenset({
    "", "unknown", "exception", "watchdog_timeout", "watchdog_revive",
    "no_message_id", "send_outcome_unknown", "internal_error",
})


def _is_ambiguous_code(code: Optional[str]) -> bool:
    return (code or "").strip().lower() in AMBIGUOUS_FAILURE_CODES


def proven_unsent(r: Recipient) -> bool:
    """Every attempt this recipient counted is proven to have produced no
    message: covered by ledger ``not sent`` rows, or by explicit (non-
    ambiguous) rejection codes whose number covers the attempt counter."""
    attempts = max(int(r.log_attempts or 0), 1)
    if r.ledger_unsent_attempts >= attempts:
        return True
    codes = [c for c in r.pre_accept_codes]
    if not codes or any(_is_ambiguous_code(c) for c in codes):
        return False
    # The legacy row keeps only its last error: earlier attempts' outcomes
    # were overwritten, so a counter above what we saw is incomplete history.
    return len(codes) + r.ledger_unsent_attempts >= attempts


def classify(r: Recipient, *, delivery_evidence: bool) -> str:
    copies = list(r.copies.values())
    status = (r.log_status or "").lower()
    if not copies:
        if status.startswith("skipped_"):
            return "excluded"
        if status in ("sending", "uncertain") or r.uncertain_attempts:
            return "uncertain"
        if status == "failed" or r.pre_accept_failures or r.pre_accept_codes:
            return "all_failed" if proven_unsent(r) else "uncertain"
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
    return "uncertain"


def _load_meta_errors():
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        for sub in ("backend", "database"):
            path = os.path.join(root, sub)
            if path not in sys.path:
                sys.path.insert(0, path)
        from services import meta_errors  # noqa: PLC0415
        return meta_errors
    except ImportError as exc:
        # Without the classifier nothing is proposed for retry (conservative).
        print(f"warning: meta_errors unavailable ({exc}); no retry candidates",
              file=sys.stderr)
        return None


def _retryable_rejection(code: str) -> bool:
    """A pre-accept rejection Meta invites us to retry (rate limit,
    temporary unavailability, a transport error before connecting)."""
    me = _load_meta_errors()
    key = (code or "").strip().lower()
    if me is None or not key:
        return False
    if key not in me.ERRORS:
        key = me.classify_meta_error(code=code, message=code).key
    entry = me.ERRORS.get(key)
    return bool(entry and entry.retryable) and not _is_ambiguous_code(key)


def resend_proposal(r: Recipient, category: str) -> str:
    """Proposed treatment in a resend decision — input for a person, never
    an authorisation. Proven delivery wins over every other signal."""
    if any(c.has_delivery for c in r.copies.values()):
        return "excluded_proven_delivery"
    if category == "excluded":
        return "excluded_before_send"
    if category == "not_started":
        return "not_started_new_send_decision"
    if category != "all_failed":
        return "excluded_unresolved"
    if any(c.failed for c in r.copies.values()):
        # Meta accepted and then declined delivery (spam limit, ecosystem
        # engagement, experiment, undeliverable): a restriction, not a retry.
        return "excluded_meta_restriction"
    codes = r.pre_accept_codes or []
    if codes and all(_retryable_rejection(c) for c in codes):
        return "retry_review_candidate"
    return "excluded_meta_restriction"


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
    r".*?campaign_send_log=(\w+).*?errors=\[\{.*?'title': ['\"](.*?)['\"],"
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
               log_paths: List[str], failed_tsv: List[str],
               infer_from_failures: bool = False) -> Dict[str, Any]:
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
            r = recipients.setdefault(norm_phone(m.group(3)), Recipient(norm_phone(m.group(3))))
            r.pre_accept_failures += 1
            r.pre_accept_codes.append(m.group(2))
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
            failed_events.append((m.group(1), norm_phone(m.group(2)), m.group(5), m.group(4)))
    for path in failed_tsv:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4:
                    failed_events.append((parts[2], norm_phone(parts[1]), parts[3],
                                          parts[4] if len(parts) > 4 else ""))
    seen = set()
    # A failure webhook proves Meta accepted that wamid. When the accept
    # line itself is missing from the export, the copy is still counted if
    # the event ties it to this campaign's recipients: the webhook matched a
    # campaign send-log row, or the wamid's sibling event for the same
    # recipient did. Such copies are reported separately as "inferred".
    matched_phones = {phone for _, phone, _, flag in failed_events if flag == "matched"}
    meta["accepted_inferred_from_failure"] = 0
    meta["recipients_inferred_from_failure"] = 0
    for wamid, phone, reason, flag in failed_events:
        if wamid in seen:
            continue
        seen.add(wamid)
        r = recipients.get(phone)
        if r is None or wamid not in r.copies:
            belongs = (r is not None) or (infer_from_failures and phone in matched_phones)
            if not (infer_from_failures and belongs):
                # A failure for a wamid this campaign's accept lines never
                # showed (another send, or outside the export).
                meta["failed_events_unmatched_wamid"] += 1
                continue
            if r is None:
                r = recipients[phone] = Recipient(phone)
                meta["recipients_inferred_from_failure"] += 1
            r.copy(wamid)
            meta["accepted_inferred_from_failure"] += 1
        c = r.copies[wamid]
        c.failed = True
        c.failed_reason = reason
        meta["failed_events"] += 1
    return meta


# ── Database evidence (read-only) ────────────────────────────────────────


class SourceError(RuntimeError):
    """A required evidence source could not be read completely."""


# (name, table, required) — ledger tables become optional only with
# ``--allow-missing-ledger``.
DB_SOURCES = (
    ("campaign_send_logs", "campaign_send_logs", True),
    ("message_events", "message_events", True),
    ("message_delivery_events", "message_delivery_events", True),
    ("campaign_send_attempts", "campaign_send_attempts", "ledger"),
    ("campaign_status_event_inbox", "campaign_status_event_inbox", "ledger"),
)

LEDGER_TABLES = (
    "campaign_dispatch_leases", "campaign_send_attempts",
    "campaign_status_event_inbox", "campaign_messaging_scopes",
)
LEDGER_INDEXES = {
    "campaign_dispatch_leases": ("campaign_dispatch_leases_pkey",),
    "campaign_send_attempts": (
        "campaign_send_attempts_pkey", "uq_campaign_send_attempt_log_no",
        "uq_campaign_send_attempt_wamid", "ix_campaign_send_attempt_campaign_state",
        "ix_campaign_send_attempt_scope_started",
    ),
    "campaign_status_event_inbox": (
        "campaign_status_event_inbox_pkey", "uq_campaign_status_event_wamid_status",
        "ix_campaign_status_event_pending",
    ),
    "campaign_messaging_scopes": ("campaign_messaging_scopes_pkey",),
}


def _chunks(items: List[str], n: int = 1000) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _read(conn: Any, sql: str, params: Dict[str, Any]) -> List[Any]:
    """Fetch a result completely; any failure propagates (never a partial
    list mistaken for a complete one)."""
    from sqlalchemy import text  # noqa: PLC0415
    return list(conn.execute(text(sql), params).all())


def _table_exists(conn: Any, table: str) -> bool:
    from sqlalchemy import text  # noqa: PLC0415
    return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar()


def schema_preflight(conn: Any, *, campaign_id: int, tenant_id: int) -> Dict[str, Any]:
    """Post-deploy checks in the same snapshot: the four ledger tables and
    their required indexes, the campaign's status, its lease and any
    attempt still in flight. Read-only."""
    from sqlalchemy import text  # noqa: PLC0415
    tables = {t: bool(_table_exists(conn, t)) for t in LEDGER_TABLES}
    present = {r[0] for r in conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE tablename = ANY(:t)"
    ), {"t": list(LEDGER_TABLES)}).all()}
    missing_idx = sorted(i for t, idxs in LEDGER_INDEXES.items() for i in idxs if i not in present)
    camp = conn.execute(text(
        "SELECT status, audience_count, sent_count, launched_at, updated_at "
        "FROM campaigns WHERE id = :c AND tenant_id = :t"
    ), {"c": campaign_id, "t": tenant_id}).mappings().first()
    status_counts = {r[0]: int(r[1]) for r in conn.execute(text(
        "SELECT status, count(*) FROM campaign_send_logs "
        "WHERE tenant_id = :t AND campaign_id = :c GROUP BY status"
    ), {"t": tenant_id, "c": campaign_id}).all()}
    lease = None
    in_flight = None
    if tables["campaign_dispatch_leases"]:
        row = conn.execute(text(
            "SELECT owner IS NOT NULL AS has_owner, expires_at, (expires_at > now() AT TIME ZONE 'utc') "
            "AS live, stop_requested_at, pause_reason, heartbeat_at FROM campaign_dispatch_leases "
            "WHERE campaign_id = :c"
        ), {"c": campaign_id}).mappings().first()
        lease = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()} if row else "none"
    if tables["campaign_send_attempts"]:
        in_flight = int(conn.execute(text(
            "SELECT count(*) FROM campaign_send_attempts WHERE campaign_id = :c "
            "AND state IN ('claimed', 'request_started')"
        ), {"c": campaign_id}).scalar() or 0)
    ok = all(tables.values()) and not missing_idx
    return {
        "ledger_tables": tables,
        "missing_indexes": missing_idx,
        "schema_ok": ok,
        "campaign": ({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in camp.items()}
                     if camp else None),
        "send_log_status_counts": status_counts,
        "lease": lease,
        "attempts_in_flight": in_flight,
    }


def apply_database(recipients: Dict[str, Recipient], *, url: str, campaign_id: int,
                   tenant_id: int, allow_missing_ledger: bool = False,
                   check_schema: bool = False) -> Dict[str, Any]:
    """Read every evidence source in one read-only snapshot.

    Returns ``{"sources": {name: {...status...}}, ...}``. Raises
    ``SourceError`` (with the partial source table attached) when a
    required source is missing or any read fails, so a caller can never
    mistake an incomplete read for a complete report.
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(url)
    meta: Dict[str, Any] = {"sources": {}}
    src = meta["sources"]
    current = None
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            owner = conn.execute(
                text("SELECT tenant_id FROM campaigns WHERE id = :c"), {"c": campaign_id},
            ).scalar()
            if owner is None or int(owner) != tenant_id:
                raise SystemExit(f"campaign {campaign_id} does not belong to tenant {tenant_id}")

            for name, table, required in DB_SOURCES:
                if _table_exists(conn, table):
                    src[name] = {"status": "pending"}
                elif required == "ledger" and allow_missing_ledger:
                    src[name] = {"status": "absent", "allowed": True}
                else:
                    src[name] = {"status": "absent", "allowed": False}
                    raise SourceError(f"required source {name} is missing")

            # 1. Recipient rows. The row's delivery columns describe the
            #    wamid it holds (the last one written, pre-ledger).
            current = "campaign_send_logs"
            rows = _read(conn,
                "SELECT customer_phone_e164, status, attempt_count, provider_message_id, "
                "error_code, delivered_at, read_at, failed_at "
                "FROM campaign_send_logs WHERE tenant_id = :t AND campaign_id = :c",
                {"t": tenant_id, "c": campaign_id})
            for phone, status, attempts, wamid, err, dl, rd, fl in rows:
                r = recipients.setdefault(norm_phone(phone), Recipient(norm_phone(phone)))
                r.log_status = status
                r.log_attempts = int(attempts or 0)
                if status == "failed" and not wamid:
                    r.pre_accept_failures = max(r.pre_accept_failures, 1)
                    r.pre_accept_codes.append((err or "").strip())
                if wamid:
                    c = r.copy(wamid)
                    c.read |= rd is not None
                    c.delivered |= dl is not None or rd is not None
                    if fl is not None and dl is None and rd is None:
                        c.failed = True
                        c.failed_reason = c.failed_reason or (err or "")[:120]
            src[current] = {"status": "complete", "rows": len(rows)}
            wamid_to_phone = {w: p for p, r in recipients.items() for w in r.copies}

            # 2. Attempt ledger (every request since the ledger deploy).
            if src["campaign_send_attempts"]["status"] == "pending":
                current = "campaign_send_attempts"
                atts = _read(conn,
                    "SELECT customer_phone_e164, provider_message_id, state, delivered_at, "
                    "read_at, failed_at, post_accept_error_code, error_code "
                    "FROM campaign_send_attempts WHERE tenant_id = :t AND campaign_id = :c",
                    {"t": tenant_id, "c": campaign_id})
                for phone, wamid, state, dl, rd, fl, perr, err in atts:
                    r = recipients.setdefault(norm_phone(phone), Recipient(norm_phone(phone)))
                    if state in ("uncertain", "claimed", "request_started"):
                        r.uncertain_attempts += 1
                    elif state in ("rejected", "not_sent", "abandoned"):
                        r.ledger_unsent_attempts += 1
                    if wamid:
                        c = r.copy(wamid)
                        c.read |= rd is not None
                        c.delivered |= dl is not None or rd is not None
                        if fl is not None:
                            c.failed = True
                            c.failed_reason = c.failed_reason or perr
                        wamid_to_phone[wamid] = norm_phone(phone)
                src[current] = {"status": "complete", "rows": len(atts)}

            # 3. One outbound message_events row per accepted copy, with
            #    the webhook's receipt flags for that copy's wamid.
            current = "message_events"
            events = _read(conn,
                "SELECT metadata FROM message_events WHERE tenant_id = :t "
                "AND direction = 'outbound' AND event_type = 'campaign' "
                "AND (metadata->>'campaign_id') = :c",
                {"t": tenant_id, "c": str(campaign_id)})
            unmatched = 0
            for (md,) in events:
                md = md or {}
                wamid = md.get("wa_message_id") or (md.get("provider_send") or {}).get("wamid")
                if not wamid:
                    unmatched += 1
                    continue
                phone = wamid_to_phone.get(wamid) or _phone_from_wamid(wamid)
                if not phone:
                    unmatched += 1
                    continue
                r = recipients.setdefault(phone, Recipient(phone))
                c = r.copy(wamid)
                wamid_to_phone[wamid] = phone
                c.read |= bool(md.get("_status_read"))
                c.delivered |= bool(md.get("_status_delivered")) or c.read
                if md.get("_status_failed"):
                    c.failed = True
                    c.failed_reason = c.failed_reason or str(md.get("delivery_error") or "")[:120]
            src[current] = {"status": "complete", "rows": len(events),
                            "rows_without_wamid_or_phone": unmatched}

            # 4. Append-only per-status events (raw Meta code preserved).
            #    Looked up by the wamids now known for this campaign.
            known = sorted(wamid_to_phone)
            current = "message_delivery_events"
            mde = 0
            for chunk in _chunks(known):
                for wamid, status, raw_code, err in _read(conn,
                        "SELECT wamid, status, raw_code, error_code FROM message_delivery_events "
                        "WHERE tenant_id = :t AND wamid = ANY(:w)", {"t": tenant_id, "w": chunk}):
                    mde += 1
                    c = recipients[wamid_to_phone[wamid]].copy(wamid)
                    st = (status or "").lower()
                    if st == "read":
                        c.read = c.delivered = True
                    elif st == "delivered":
                        c.delivered = True
                    elif st == "failed":
                        c.failed = True
                        c.failed_reason = c.failed_reason or (raw_code or err or "")
            src[current] = {"status": "complete", "rows": mde}

            # 5. Ledger webhook inbox (events for known wamids; pending ones
            #    are receipts not yet attached to an attempt).
            if src["campaign_status_event_inbox"]["status"] == "pending":
                current = "campaign_status_event_inbox"
                inbox = pending = 0
                for chunk in _chunks(known):
                    for wamid, status, applied in _read(conn,
                            "SELECT provider_message_id, status, applied_at IS NOT NULL "
                            "FROM campaign_status_event_inbox WHERE provider_message_id = ANY(:w)",
                            {"w": chunk}):
                        inbox += 1
                        pending += 0 if applied else 1
                        c = recipients[wamid_to_phone[wamid]].copy(wamid)
                        st = (status or "").lower()
                        if st == "read":
                            c.read = c.delivered = True
                        elif st == "delivered":
                            c.delivered = True
                        elif st == "failed":
                            c.failed = True
                src[current] = {"status": "complete", "rows": inbox, "pending_unapplied": pending}

            current = None
            if check_schema:
                meta["preflight"] = schema_preflight(conn, campaign_id=campaign_id, tenant_id=tenant_id)
    except SourceError as exc:
        meta["error"] = str(exc)
        raise SourceError(json.dumps(meta, ensure_ascii=False, default=str)) from exc
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — every other failure is fatal, never partial
        if current:
            src[current] = {"status": "error",
                            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
        meta["error"] = f"read failed in {current or 'setup'}"
        raise SourceError(json.dumps(meta, ensure_ascii=False, default=str)) from exc
    finally:
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


RESEND_PROPOSALS = (
    "excluded_proven_delivery", "excluded_unresolved", "excluded_meta_restriction",
    "excluded_before_send", "retry_review_candidate", "not_started_new_send_decision",
)


def build_report(recipients: Dict[str, Recipient], *, delivery_evidence: bool,
                 sources: Dict[str, Any], emit_recipients: bool,
                 decision_eligible: bool = False,
                 ineligible_reasons: Optional[List[str]] = None) -> Dict[str, Any]:
    counts = {k: 0 for k in CATEGORIES}
    proposals = {k: 0 for k in RESEND_PROPOSALS}
    messages = {"accepted": 0, "delivered_or_read": 0, "read": 0,
                "failed_after_accept": 0, "no_receipt": 0}
    proven_delivery = 0
    per = []
    for phone, r in recipients.items():
        cat = classify(r, delivery_evidence=delivery_evidence)
        counts[cat] += 1
        delivered_copies = sum(1 for c in r.copies.values() if c.has_delivery)
        if delivered_copies:
            proven_delivery += 1
        proposal = resend_proposal(r, cat)
        proposals[proposal] += 1
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
            per.append({
                "recipient": mask(phone), "category": cat,
                "has_proven_delivery": bool(delivered_copies),
                "delivered_copies": delivered_copies,
                "copies": len(r.copies),
                "resend_proposal": proposal,
                "failed_reasons": sorted({c.failed_reason for c in r.copies.values()
                                          if c.failed_reason}),
                "pre_accept_codes": sorted({c for c in r.pre_accept_codes if c}),
            })
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
        "decision_eligible": bool(decision_eligible),
        "ineligible_reasons": list(ineligible_reasons or []),
        "delivery_evidence": "available" if delivery_evidence else "unavailable",
        "recipients_total": len(recipients),
        "recipients_by_category": counts,
        "recipients_with_proven_delivery": proven_delivery,
        "resend_proposal": proposals,
        "recipients_by_accepted_copies": dict(sorted(accepted_hist.items())),
        "messages": messages,
        "failed_after_accept_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "sources": sources,
        **({"recipients": per} if emit_recipients else {}),
    }


def _write(report: Dict[str, Any], out: Optional[str], emit_recipients: bool) -> None:
    text_out = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text_out)
    print(text_out if not emit_recipients else json.dumps(
        {k: v for k, v in report.items() if k != "recipients"},
        ensure_ascii=False, indent=2, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant-id", type=int, required=True)
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--no-database", action="store_true")
    ap.add_argument("--allow-missing-ledger", action="store_true",
                    help="tolerate absent ledger tables (database from before the ledger "
                         "deploy); the report is then not decision-eligible")
    ap.add_argument("--check-schema", action="store_true",
                    help="also run the post-deploy preflight (ledger tables/indexes, "
                         "campaign status, lease, in-flight attempts)")
    ap.add_argument("--log-json", nargs="*", default=[])
    ap.add_argument("--failed-tsv", nargs="*", default=[])
    ap.add_argument("--infer-accepted-from-failures", action="store_true",
                    help="count copies whose accept line is missing but whose failure "
                         "webhook ties them to this campaign (reported separately)")
    ap.add_argument("--emit-recipients", action="store_true",
                    help="include a masked per-recipient list")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    recipients: Dict[str, Recipient] = {}
    sources: Dict[str, Any] = {}
    reasons: List[str] = []
    use_db = bool(args.database_url) and not args.no_database
    db_complete = False
    if use_db:
        try:
            db_meta = apply_database(
                recipients, url=args.database_url,
                campaign_id=args.campaign_id, tenant_id=args.tenant_id,
                allow_missing_ledger=args.allow_missing_ledger,
                check_schema=args.check_schema,
            )
        except SourceError as exc:
            try:
                failed = json.loads(str(exc))
            except ValueError:
                failed = {"error": str(exc)}
            report = {
                "decision_eligible": False,
                "ineligible_reasons": ["database source unreadable: " + str(failed.get("error"))],
                "sources": {"database": failed},
            }
            _write(report, args.out, False)
            print("FATAL: evidence source incomplete — no reconciliation produced", file=sys.stderr)
            return 2
        sources["database"] = db_meta
        statuses = {n: v.get("status") for n, v in db_meta["sources"].items()}
        db_complete = all(st == "complete" for st in statuses.values())
        if not db_complete:
            reasons.append("database sources not all complete: " + json.dumps(statuses))
        pre = db_meta.get("preflight")
        if args.check_schema and pre and not pre.get("schema_ok"):
            reasons.append("ledger schema incomplete")
        if not args.check_schema:
            reasons.append("schema preflight not run (--check-schema)")
    else:
        reasons.append("no database evidence (log-only run)")
    if args.log_json or args.failed_tsv:
        sources["logs"] = apply_logs(
            recipients, campaign_id=args.campaign_id, tenant_id=args.tenant_id,
            log_paths=args.log_json, failed_tsv=args.failed_tsv,
            infer_from_failures=args.infer_accepted_from_failures,
        )
    if not sources:
        ap.error("give --database-url/DATABASE_URL or --log-json files")
    report = build_report(
        recipients, delivery_evidence=use_db and db_complete, sources=sources,
        emit_recipients=args.emit_recipients,
        decision_eligible=use_db and not reasons, ineligible_reasons=reasons,
    )
    _write(report, args.out, args.emit_recipients)
    return 0


if __name__ == "__main__":
    sys.exit(main())
