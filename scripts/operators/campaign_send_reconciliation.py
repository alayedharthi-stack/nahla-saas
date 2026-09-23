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

Per-copy vs recipient-level evidence. A copy's receipts come only from
sources tied to its own wamid or attempt: ``campaign_send_attempts``,
``message_events``, ``message_delivery_events`` and the status inbox. The
summary row's ``delivered_at`` / ``read_at`` / ``failed_at`` are recipient-
level: with the ledger they are aggregates over every accepted attempt, and
before it the webhook set them for whichever wamid the row held at the time
(later overwritten by a second copy). They prove "at least one copy reached
this recipient" — enough to exclude the recipient from a resend — but they
never add a delivered copy, a read or a failure to the anchor wamid, except
for a legacy-only row whose complete history has that anchor as its only
copy (``row_evidence_is_per_copy``).

"Read" counts as delivered even without a separate delivered receipt. The
absence of a receipt never counts as "not delivered". Log-only runs cannot see
delivery receipts, so they report ``delivery_evidence: unavailable`` and never
fill the two delivered categories.

Attempt history. Every attempt the recipient's row counted
(``attempt_count``) must be accounted for exactly once before a category
may say "every attempt failed" or "delivered once". Identities: a ledger
row is ``(send_log_id, attempt_no)``; an accepted copy is its wamid; the
summary row stands for the LAST attempt only — and when the ledger has an
attempt, the summary is that ledger attempt again (the ledger copies its
code onto the row), never a second one. An attempt is proven unsent only by
a ledger ``rejected`` / ``not_sent`` state or, for a legacy row, by a
positive rejection code from ``PROVEN_REJECTION_CODES`` — a code merely
absent from a list of ambiguous ones proves nothing. Earlier legacy
attempts whose outcome was overwritten stay unknown, with or without
copies. Exported log lines carry no attempt identity and never add proof.

Every recipient also carries ``has_proven_delivery`` / ``delivered_copies``,
independent of its category, and a ``resend_proposal``. A recipient with any
delivered/read copy is excluded from a resend whatever its other copies say.
The proposal is input to a human decision, never an authorisation.

Sources, attribution and completeness (database mode)
──────────────────────────────────────────────────────
All reads happen in one ``REPEATABLE READ, READ ONLY`` transaction. Each
source is ``complete``, ``absent`` or ``error``: the table exists, the role
can SELECT it (checked up front, so an empty lookup set cannot hide a
missing grant) and every query ran to completion. A missing ledger table is
tolerated only with ``--allow-missing-ledger``; any other read failure
aborts with exit code 2.

Query completion is not attribution. Copies are discovered from every
campaign association — ledger attempts, the summary row, campaign
``message_events`` (placed through their conversation's customer),
``message_delivery_events`` linked to the campaign's rows, and accept lines
from ``--log-json`` — before receipts are read for the whole wamid set.
Campaign-scoped evidence that cannot be tied to exactly one recipient is
counted under ``attribution`` and makes the report ineligible; an
unapplied inbox receipt that may belong to a recipient keeps that
recipient unresolved. ``decision_eligible`` needs complete sources,
complete attribution and a passed ``--check-schema``; log-only runs are
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
class LedgerAttempt:
    """One ``campaign_send_attempts`` row. ``(send_log_id, attempt_no)`` is
    its identity; the recipient keeps them keyed by ``attempt_no``."""
    attempt_no: int
    state: str
    wamid: Optional[str] = None
    error_code: Optional[str] = None


@dataclass
class Recipient:
    phone: str
    copies: Dict[str, Copy] = field(default_factory=dict)
    # The ``campaign_send_logs`` summary row. It describes the recipient's
    # LAST attempt only; ``log_attempts`` counts every attempt that reached
    # the request phase (an abandoned claim is subtracted again).
    has_log_row: bool = False
    log_status: Optional[str] = None
    log_attempts: int = 0
    log_error_code: Optional[str] = None
    log_failed_at: bool = False
    # The row's anchor wamid and its delivery columns. These columns are
    # RECIPIENT-LEVEL evidence: with the ledger they are aggregates over
    # every accepted attempt; before it, the webhook set them for whatever
    # wamid the row held when the receipt arrived (later overwritten by a
    # second copy). They are tied to the anchor copy only when that copy is
    # provably the recipient's only one (``row_evidence_is_per_copy``).
    log_wamid: Optional[str] = None
    agg_delivered: bool = False
    agg_read: bool = False
    ledger: Dict[int, LedgerAttempt] = field(default_factory=dict)
    # Observations without an attempt identity (exported log lines). They
    # are shown, never counted as proof: they may describe an attempt the
    # summary row or the ledger already describes.
    observed_codes: List[str] = field(default_factory=list)
    uncertain_observations: int = 0
    # Evidence that may belong to this recipient but could not be placed
    # (e.g. an unapplied inbox receipt for an unknown wamid).
    unresolved_evidence: List[str] = field(default_factory=list)

    def copy(self, wamid: str) -> Copy:
        c = self.copies.get(wamid)
        if c is None:
            c = self.copies[wamid] = Copy(wamid=wamid)
        return c

    def ledger_counted(self) -> bool:
        return any(a.state not in LEDGER_NOT_COUNTED_STATES for a in self.ledger.values())

    def delivered_copies(self) -> int:
        return sum(1 for c in self.copies.values() if c.has_delivery)

    def has_proven_delivery(self) -> bool:
        """At least one copy reached this recipient — per-copy evidence, or
        the row's recipient-level delivered/read columns."""
        return self.delivered_copies() > 0 or self.agg_delivered or self.agg_read


# Positive rejection evidence: canonical ``meta_errors`` keys that the
# dispatcher stores only when Meta returned an explicit error body for the
# request (or a local guard refused it before any request). A code that is
# not listed here — ``watchdog_timeout``, ``exception``, ``no_message_id``,
# ``unknown``, ``service_unavailable`` (5xx), ``retry_exhausted`` (the real
# error was overwritten), a new or misspelt string — proves nothing.
PROVEN_REJECTION_CODES = frozenset({
    "automation_blocked", "not_on_whatsapp", "invalid_phone", "invalid_payload",
    "out_of_24h_window", "user_not_opted_in", "marketing_blocked",
    "client_payment_blocked", "rate_limit", "spam_rate_limit",
    "template_param_mismatch", "template_not_found", "template_paused",
    "template_disabled", "policy_violation", "account_locked", "media_error",
    "auth_error", "recipient_quality_low", "blocked_by_user",
    "country_restricted", "transport_not_sent",
})

# Ledger states that prove the attempt produced no message. ``abandoned`` is
# not an attempt at all: the claim was released before the request phase
# and the row's counter was decremented again.
LEDGER_UNSENT_STATES = frozenset({"rejected", "not_sent"})
LEDGER_NOT_COUNTED_STATES = frozenset({"abandoned"})


def is_proven_rejection_code(code: Optional[str]) -> bool:
    return (code or "").strip().lower() in PROVEN_REJECTION_CODES


@dataclass
class History:
    """Every attempt the summary row counted, each accounted for once."""
    complete: bool
    unknown_attempts: int = 0
    unsent_codes: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


def attempt_history(r: Recipient) -> History:
    """Account for each counted attempt exactly once.

    Attempt identities: a ledger row is ``(send_log_id, attempt_no)``; an
    accepted copy is its wamid; the summary row stands for the recipient's
    last attempt. Ledger attempts always follow the legacy ones, so the
    summary describes a legacy attempt only when there is no counted ledger
    attempt — otherwise it is the ledger's last attempt again and adds
    nothing. Exported log lines carry no identity and never add coverage.
    """
    if not r.has_log_row:
        return History(False, reasons=["attempt count unknown (no send-log row)"])
    reasons: List[str] = []
    unsent: List[str] = []
    unknown = 0
    counted = [a for _, a in sorted(r.ledger.items())
               if a.state not in LEDGER_NOT_COUNTED_STATES]
    for a in counted:
        if a.state in LEDGER_UNSENT_STATES:
            unsent.append(a.error_code or a.state)
        elif not (a.state == "accepted" and a.wamid and a.wamid in r.copies):
            unknown += 1                      # claimed / request_started / uncertain
    legacy_n = int(r.log_attempts or 0) - len(counted)
    ledger_wamids = {a.wamid for a in counted if a.wamid}
    legacy_copies = [w for w in r.copies if w not in ledger_wamids]
    legacy_rejection = (
        not counted and legacy_n >= 1
        and (r.log_status or "").lower() == "failed"
        and not r.log_failed_at               # not a post-accept failure
        and is_proven_rejection_code(r.log_error_code)
    )
    covered = len(legacy_copies) + (1 if legacy_rejection else 0)
    if legacy_n < 0:
        reasons.append("ledger attempts exceed the row's attempt counter")
    elif covered > legacy_n:
        reasons.append("more outcomes than counted attempts (counter unreliable)")
    else:
        unknown += legacy_n - covered         # earlier attempts left no evidence
    if legacy_rejection:
        unsent.append((r.log_error_code or "").strip().lower())
    if r.uncertain_observations:
        reasons.append("ambiguous attempt observed in logs")
    reasons.extend(r.unresolved_evidence)
    return History(complete=not reasons and unknown == 0, unknown_attempts=unknown,
                   unsent_codes=unsent, reasons=reasons)


def classify(r: Recipient, *, delivery_evidence: bool) -> str:
    copies = list(r.copies.values())
    delivered = [c for c in copies if c.has_delivery]
    if len(delivered) >= 2:
        return "delivered_multiple"
    h = attempt_history(r)
    status = (r.log_status or "").lower()
    if h.complete and not copies and not h.unsent_codes and not r.ledger_counted():
        # The row never counted an attempt.
        if status.startswith("skipped_"):
            return "excluded"
        if status in ("", "queued"):
            return "not_started"
        return "uncertain"
    if not h.complete:
        # An attempt without evidence may be another accepted copy.
        return "accepted_multiple_unproven" if len(copies) >= 2 else "uncertain"
    if any(not c.has_delivery and not c.failed for c in copies):
        return "accepted_multiple_unproven" if len(copies) >= 2 else "uncertain"
    if delivered:
        return "delivered_once"               # every other attempt proven failed
    return "all_failed"


def row_evidence_is_per_copy(r: Recipient) -> bool:
    """May the summary row's delivered/read/failed columns be read as the
    receipts of its anchor wamid?

    Only for a legacy-only row (no ledger attempt — the ledger rewrites the
    columns as aggregates) whose attempt history is complete and whose only
    accepted copy is the anchor: then no other wamid was ever on the row, so
    every receipt that set the columns was the anchor's own."""
    if r.ledger or not r.log_wamid:
        return False
    if set(r.copies) != {r.log_wamid}:
        return False
    return attempt_history(r).complete


def _apply_row_columns(r: Recipient) -> None:
    c = r.copies[r.log_wamid]
    c.read |= r.agg_read
    c.delivered |= r.agg_delivered or r.agg_read
    if r.log_failed_at and not (r.agg_delivered or r.agg_read):
        c.failed = True
        c.failed_reason = c.failed_reason or (r.log_error_code or "")[:120] or None


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
    """A proven rejection Meta invites us to retry (rate limit, a transport
    failure before any connection). Exact canonical keys only."""
    key = (code or "").strip().lower()
    if key not in PROVEN_REJECTION_CODES:
        return False
    me = _load_meta_errors()
    entry = me.ERRORS.get(key) if me is not None else None
    return bool(entry and entry.retryable)


def resend_proposal(r: Recipient, category: str) -> str:
    """Proposed treatment in a resend decision — input for a person, never
    an authorisation. Proven delivery wins over every other signal."""
    if r.has_proven_delivery():
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
    codes = attempt_history(r).unsent_codes
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


def collect_log_wamids(paths: List[str], *, campaign_id: int) -> Dict[str, str]:
    """wamid → phone for every campaign-scoped accept line, so the database
    pass reads their receipts in the same snapshot (and attributes them)."""
    out: Dict[str, str] = {}
    for msg in iter_log_messages(paths):
        m = _SENT_RE.search(msg)
        if m and int(m.group(1)) == campaign_id:
            out[m.group(3)] = norm_phone(m.group(2))
    return out


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
            # No attempt identity: shown, never counted as proof.
            r.observed_codes.append(m.group(2))
            meta["pre_accept_errors"] += 1
            continue
        m = _EXC_RE.search(msg)
        if m and int(m.group(1)) == campaign_id:
            recipients.setdefault(norm_phone(m.group(2)), Recipient(norm_phone(m.group(2)))) \
                .uncertain_observations += 1
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
# ``--allow-missing-ledger``. ``conversations`` / ``customers`` carry the
# association of a ``message_events`` row to its recipient.
DB_SOURCES = (
    ("campaign_send_logs", "campaign_send_logs", True),
    ("message_events", "message_events", True),
    ("conversations", "conversations", True),
    ("customers", "customers", True),
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


def _chunks(items: List[Any], n: int = 1000) -> Iterable[List[Any]]:
    """Batches of ``items``; an empty list still yields one (empty) batch so
    the query runs — and proves read access — even with nothing to look up."""
    if not items:
        yield []
        return
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


def _can_select(conn: Any, table: str) -> bool:
    from sqlalchemy import text  # noqa: PLC0415
    return bool(conn.execute(text("SELECT has_table_privilege(:t, 'SELECT')"),
                             {"t": table}).scalar())


def _apply_status(c: Copy, status: Optional[str], reason: Optional[str] = None) -> None:
    st = (status or "").lower()
    if st == "read":
        c.read = c.delivered = True
    elif st == "delivered":
        c.delivered = True
    elif st == "failed":
        c.failed = True
        c.failed_reason = c.failed_reason or (reason or "")[:120] or None


def _apply_event_flags(c: Copy, md: Dict[str, Any]) -> None:
    c.read |= bool(md.get("_status_read"))
    c.delivered |= bool(md.get("_status_delivered")) or c.read
    if md.get("_status_failed"):
        c.failed = True
        c.failed_reason = c.failed_reason or str(md.get("delivery_error") or "")[:120] or None


def _event_wamid(md: Dict[str, Any]) -> Optional[str]:
    return md.get("wa_message_id") or (md.get("provider_send") or {}).get("wamid") or None


def schema_preflight(conn: Any, *, campaign_id: int, tenant_id: int) -> Dict[str, Any]:
    """Post-deploy checks in the same snapshot: the four ledger tables and
    their required indexes, the campaign's status, its lease and any
    attempt still in flight. Read-only."""
    from sqlalchemy import text  # noqa: PLC0415
    tables = {t: bool(_table_exists(conn, t)) for t in LEDGER_TABLES}
    present = {r[0] for r in conn.execute(text(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = ANY(:t)"
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
                   check_schema: bool = False,
                   seed_wamids: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Read every evidence source in one read-only snapshot.

    Query completion and attribution are reported separately:

    * ``sources[name].status`` — the table was present, readable and every
      query against it ran to completion (``complete``), or not;
    * ``attribution`` — campaign-scoped evidence that could not be tied to
      exactly one of this campaign's recipients. Nothing campaign-scoped is
      dropped silently: it is counted here and makes the report ineligible.

    Copies are discovered from every campaign association (ledger attempts,
    the summary row, campaign ``message_events``, ``message_delivery_events``
    linked to the campaign's send-log rows, ``seed_wamids`` from exported
    logs), then receipts are read for the complete wamid set, repeating
    until no new wamid appears.

    Raises ``SourceError`` (with the partial source table attached) when a
    required source is missing, unreadable or any read fails.
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(url)
    meta: Dict[str, Any] = {"sources": {}}
    src = meta["sources"]
    attribution = {
        "campaign_events_unattributed": 0,
        "campaign_events_unattributed_with_receipts": 0,
        "campaign_events_without_wamid": 0,
        "attribution_conflicts": 0,
        "log_wamids_unattributed": 0,
        "synthetic_failure_events": 0,
        "recipients_with_pending_inbox_candidates": 0,
    }
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
                if not _table_exists(conn, table):
                    if required == "ledger" and allow_missing_ledger:
                        src[name] = {"status": "absent", "allowed": True}
                        continue
                    src[name] = {"status": "absent", "allowed": False}
                    raise SourceError(f"required source {name} is missing")
                if not _can_select(conn, table):
                    src[name] = {"status": "error",
                                 "error": "InsufficientPrivilege: no SELECT on " + table}
                    raise SourceError(f"source {name} is not readable")
                src[name] = {"status": "pending", "rows": 0}
            has_attempts = src["campaign_send_attempts"]["status"] == "pending"
            has_inbox = src["campaign_status_event_inbox"]["status"] == "pending"

            # wamid → recipient phone, from authoritative associations only.
            wamid_owner: Dict[str, str] = {}

            def claim(wamid: str, phone: str) -> bool:
                prev = wamid_owner.get(wamid)
                if prev is not None and prev != phone:
                    attribution["attribution_conflicts"] += 1
                    return False
                wamid_owner[wamid] = phone
                recipients[phone].copy(wamid)
                return True

            # 1. Recipient rows (the campaign's audience). The row's delivery
            #    columns describe the wamid it holds (the last one written).
            current = "campaign_send_logs"
            rows = _read(conn,
                "SELECT id, customer_phone_e164, status, attempt_count, provider_message_id, "
                "error_code, delivered_at, read_at, failed_at "
                "FROM campaign_send_logs WHERE tenant_id = :t AND campaign_id = :c",
                {"t": tenant_id, "c": campaign_id})
            log_phone: Dict[int, str] = {}
            for log_id, phone, status, attempts, wamid, err, dl, rd, fl in rows:
                p = norm_phone(phone)
                log_phone[int(log_id)] = p
                r = recipients.setdefault(p, Recipient(p))
                r.has_log_row = True
                r.log_status = status
                r.log_attempts = int(attempts or 0)
                r.log_error_code = (err or "").strip() or None
                r.log_failed_at = fl is not None
                r.log_wamid = wamid or None
                r.agg_delivered = dl is not None
                r.agg_read = rd is not None
                if wamid:
                    claim(wamid, p)       # a copy exists; its receipts come later
            src[current].update(status="complete", rows=len(rows))
            campaign_phones = set(log_phone.values())

            # 2. Attempt ledger, keyed by (send_log_id, attempt_no).
            attempt_ids: List[int] = []
            if has_attempts:
                current = "campaign_send_attempts"
                atts = _read(conn,
                    "SELECT id, send_log_id, attempt_no, state, provider_message_id, error_code, "
                    "delivered_at, read_at, failed_at, post_accept_error_code "
                    "FROM campaign_send_attempts WHERE tenant_id = :t AND campaign_id = :c",
                    {"t": tenant_id, "c": campaign_id})
                for att_id, log_id, no, state, wamid, err, dl, rd, fl, perr in atts:
                    attempt_ids.append(int(att_id))
                    p = log_phone.get(int(log_id))
                    if p is None:
                        attribution["attribution_conflicts"] += 1
                        continue
                    r = recipients[p]
                    r.ledger[int(no)] = LedgerAttempt(int(no), state, wamid, err)
                    if wamid and claim(wamid, p):
                        c = r.copies[wamid]
                        c.read |= rd is not None
                        c.delivered |= dl is not None or rd is not None
                        if fl is not None:
                            c.failed = True
                            c.failed_reason = c.failed_reason or perr
                src[current].update(status="complete", rows=len(atts))

            # 3. Copies named by exported logs (campaign-scoped accept lines).
            for wamid, phone in sorted((seed_wamids or {}).items()):
                p = norm_phone(phone)
                if p not in campaign_phones or not claim(wamid, p):
                    attribution["log_wamids_unattributed"] += 1

            # 4. Campaign message_events: one outbound row per accepted copy.
            #    Attributed by wamid if already owned, else by the row's own
            #    conversation → customer, which must be one campaign recipient.
            current = "message_events"
            seen_events: set = set()
            unattributed: Dict[str, Copy] = {}
            events = _read(conn,
                "SELECT me.id, me.metadata, cu.phone, cu.normalized_phone, "
                "conv.metadata->>'customer_phone' "
                "FROM message_events me "
                "LEFT JOIN conversations conv ON conv.id = me.conversation_id "
                "AND conv.tenant_id = me.tenant_id "
                "LEFT JOIN customers cu ON cu.id = conv.customer_id AND cu.tenant_id = me.tenant_id "
                "WHERE me.tenant_id = :t AND me.direction = 'outbound' "
                "AND me.event_type = 'campaign' AND (me.metadata->>'campaign_id') = :c",
                {"t": tenant_id, "c": str(campaign_id)})
            for ev_id, md, cu_phone, cu_norm, conv_phone in events:
                seen_events.add(int(ev_id))
                md = md or {}
                wamid = _event_wamid(md)
                if not wamid:
                    attribution["campaign_events_without_wamid"] += 1
                    continue
                candidates = {norm_phone(x) for x in (cu_phone, cu_norm, conv_phone) if x}
                matches = candidates & campaign_phones
                owner_p = wamid_owner.get(wamid)
                if owner_p is None and len(matches) == 1:
                    owner_p = next(iter(matches))
                    claim(wamid, owner_p)
                elif owner_p is not None and matches and owner_p not in matches:
                    attribution["attribution_conflicts"] += 1
                if owner_p is None:
                    c = unattributed.setdefault(wamid, Copy(wamid=wamid))
                    _apply_event_flags(c, md)
                    continue
                _apply_event_flags(recipients[owner_p].copies[wamid], md)
            src[current].update(status="complete", rows=len(events))

            # 5. message_delivery_events linked to this campaign's send-log
            #    rows (a copy whose wamid was overwritten on the row is still
            #    linked when its receipt matched the row at the time).
            current = "message_delivery_events"
            seen_mde: set = set()
            for chunk in _chunks(sorted(log_phone)):
                for mde_id, wamid, status, raw_code, err, log_id in _read(conn,
                        "SELECT id, wamid, status, raw_code, error_code, campaign_send_log_id "
                        "FROM message_delivery_events WHERE tenant_id = :t "
                        "AND campaign_send_log_id = ANY(:ids)", {"t": tenant_id, "ids": chunk}):
                    seen_mde.add(int(mde_id))
                    if (wamid or "").startswith("synth:"):
                        attribution["synthetic_failure_events"] += 1
                        recipients[log_phone[int(log_id)]].uncertain_observations += 1
                        continue
                    if claim(wamid, log_phone[int(log_id)]):
                        _apply_status(recipients[wamid_owner[wamid]].copies[wamid],
                                      status, raw_code or err)
                    src[current]["rows"] += 1

            # 6. Receipts for the complete wamid set, until it stops growing.
            read_wamids: set = set()
            seen_inbox: set = set()
            first = True
            while True:
                pending = sorted((set(wamid_owner) | set(unattributed)) - read_wamids)
                if not pending and not first:
                    break
                for chunk in _chunks(pending):
                    current = "message_delivery_events"
                    for mde_id, wamid, status, raw_code, err in _read(conn,
                            "SELECT id, wamid, status, raw_code, error_code "
                            "FROM message_delivery_events WHERE tenant_id = :t "
                            "AND wamid = ANY(:w)", {"t": tenant_id, "w": chunk}):
                        if int(mde_id) in seen_mde:
                            continue
                        seen_mde.add(int(mde_id))
                        src[current]["rows"] += 1
                        c = (recipients[wamid_owner[wamid]].copies[wamid] if wamid in wamid_owner
                             else unattributed[wamid])
                        _apply_status(c, status, raw_code or err)
                    current = "message_events"
                    for ev_id, md in _read(conn,
                            "SELECT id, metadata FROM message_events WHERE tenant_id = :t "
                            "AND direction = 'outbound' AND ((metadata->>'wa_message_id') = ANY(:w) "
                            "OR (metadata->'provider_send'->>'wamid') = ANY(:w))",
                            {"t": tenant_id, "w": chunk}):
                        if int(ev_id) in seen_events:
                            continue
                        seen_events.add(int(ev_id))
                        src[current]["rows"] += 1
                        wamid = _event_wamid(md or {})
                        c = (recipients[wamid_owner[wamid]].copies[wamid] if wamid in wamid_owner
                             else unattributed.get(wamid))
                        if c is not None:
                            _apply_event_flags(c, md or {})
                    if has_inbox:
                        current = "campaign_status_event_inbox"
                        for ib_id, wamid, status in _read(conn,
                                "SELECT id, provider_message_id, status "
                                "FROM campaign_status_event_inbox WHERE provider_message_id = ANY(:w)",
                                {"w": chunk}):
                            if int(ib_id) in seen_inbox:
                                continue
                            seen_inbox.add(int(ib_id))
                            src[current]["rows"] += 1
                            c = (recipients[wamid_owner[wamid]].copies[wamid]
                                 if wamid in wamid_owner else unattributed[wamid])
                            _apply_status(c, status)
                read_wamids |= set(pending)
                first = False
            if has_inbox:
                # Receipts stored against this campaign's attempts.
                current = "campaign_status_event_inbox"
                for chunk in _chunks(attempt_ids):
                    for ib_id, wamid, status in _read(conn,
                            "SELECT id, provider_message_id, status FROM campaign_status_event_inbox "
                            "WHERE attempt_id = ANY(:a)", {"a": chunk}):
                        if int(ib_id) in seen_inbox:
                            continue
                        seen_inbox.add(int(ib_id))
                        src[current]["rows"] += 1
                        if wamid in wamid_owner:
                            _apply_status(recipients[wamid_owner[wamid]].copies[wamid], status)
                        else:
                            attribution["attribution_conflicts"] += 1
                # Unapplied receipts for wamids nobody knows yet, addressed to
                # one of this campaign's recipients. The inbox has no tenant,
                # so this cannot be bounded to the campaign: the recipient is
                # kept unresolved rather than guessed.
                digits = sorted({p.lstrip("+") for p in campaign_phones})
                flagged: set = set()
                for chunk in _chunks(digits):
                    for (rid,) in _read(conn,
                            "SELECT DISTINCT recipient_id FROM campaign_status_event_inbox "
                            "WHERE applied_at IS NULL AND attempt_id IS NULL "
                            "AND recipient_id = ANY(:d) AND NOT (provider_message_id = ANY(:w))",
                            {"d": chunk, "w": sorted(read_wamids)}):
                        p = norm_phone(rid)
                        if p in recipients and p not in flagged:
                            flagged.add(p)
                            recipients[p].unresolved_evidence.append(
                                "unapplied inbox receipt for an unknown wamid")
                attribution["recipients_with_pending_inbox_candidates"] = len(flagged)
                src[current]["status"] = "complete"
            for name in ("message_events", "conversations", "customers", "message_delivery_events"):
                src[name]["status"] = "complete"
            current = None

            # Every copy is known now: tie the row's columns to the anchor
            # only where that is provably per-copy evidence.
            per_copy_rows = 0
            for p in campaign_phones:
                r = recipients[p]
                if row_evidence_is_per_copy(r):
                    _apply_row_columns(r)
                    per_copy_rows += 1
            meta["row_columns_as_per_copy_evidence"] = per_copy_rows

            attribution["campaign_events_unattributed"] = len(unattributed)
            attribution["campaign_events_unattributed_with_receipts"] = sum(
                1 for c in unattributed.values() if c.has_delivery or c.failed)
            attribution["unattributed_with_delivery_evidence"] = sum(
                1 for c in unattributed.values() if c.has_delivery)
            attribution["wamids_read"] = len(read_wamids)
            blocking = [k for k in ("campaign_events_unattributed", "campaign_events_without_wamid",
                                    "attribution_conflicts", "log_wamids_unattributed")
                        if attribution[k]]
            attribution["complete"] = not blocking
            attribution["blocking"] = blocking
            meta["attribution"] = attribution
            meta["_wamids_read"] = read_wamids
            if check_schema:
                meta["preflight"] = schema_preflight(conn, campaign_id=campaign_id, tenant_id=tenant_id)
    except SourceError as exc:
        meta["error"] = str(exc)
        meta.pop("_wamids_read", None)
        raise SourceError(json.dumps(meta, ensure_ascii=False, default=str)) from exc
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — every other failure is fatal, never partial
        if current:
            src[current] = {"status": "error",
                            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"}
        meta["error"] = f"read failed in {current or 'setup'}"
        meta.pop("_wamids_read", None)
        raise SourceError(json.dumps(meta, ensure_ascii=False, default=str)) from exc
    finally:
        engine.dispose()
    return meta


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
    aggregate_only = 0
    incomplete_history = 0
    per = []
    for phone, r in recipients.items():
        cat = classify(r, delivery_evidence=delivery_evidence)
        counts[cat] += 1
        hist = attempt_history(r)
        incomplete_history += 0 if hist.complete else 1
        delivered_copies = r.delivered_copies()
        proven = r.has_proven_delivery()
        if proven:
            proven_delivery += 1
            if not delivered_copies:
                aggregate_only += 1
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
                "has_proven_delivery": proven,
                "delivered_copies": delivered_copies,
                "delivery_evidence_scope": ("per_copy" if delivered_copies else
                                            "recipient_aggregate" if proven else None),
                "copies": len(r.copies),
                "resend_proposal": proposal,
                "failed_reasons": sorted({c.failed_reason for c in r.copies.values()
                                          if c.failed_reason}),
                "history_complete": hist.complete,
                "unknown_attempts": hist.unknown_attempts,
                "history_reasons": hist.reasons,
                "proven_unsent_codes": hist.unsent_codes,
                "observed_log_codes": sorted({c for c in r.observed_codes if c}),
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
        "recipients_with_aggregate_only_delivery": aggregate_only,
        "recipients_with_incomplete_history": incomplete_history,
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
    wamids_read: Optional[set] = None
    if use_db:
        seeds = collect_log_wamids(args.log_json, campaign_id=args.campaign_id) \
            if args.log_json else {}
        try:
            db_meta = apply_database(
                recipients, url=args.database_url,
                campaign_id=args.campaign_id, tenant_id=args.tenant_id,
                allow_missing_ledger=args.allow_missing_ledger,
                check_schema=args.check_schema, seed_wamids=seeds,
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
        wamids_read = db_meta.pop("_wamids_read")
        sources["database"] = db_meta
        statuses = {n: v.get("status") for n, v in db_meta["sources"].items()}
        db_complete = all(st == "complete" for st in statuses.values())
        if not db_complete:
            reasons.append("database sources not all complete: " + json.dumps(statuses))
        attr = db_meta["attribution"]
        if not attr["complete"]:
            reasons.append("campaign evidence not attributed to one recipient: "
                           + ", ".join(f"{k}={attr[k]}" for k in attr["blocking"]))
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
    if wamids_read is not None:
        # A copy added after the snapshot was read has unread receipts.
        late = sum(1 for r in recipients.values() for w in r.copies if w not in wamids_read)
        sources["database"]["attribution"]["copies_added_after_reads"] = late
        if late:
            reasons.append(f"{late} copies found after the database reads (receipts unread)")
    report = build_report(
        recipients, delivery_evidence=use_db and db_complete, sources=sources,
        emit_recipients=args.emit_recipients,
        decision_eligible=use_db and not reasons, ineligible_reasons=reasons,
    )
    _write(report, args.out, args.emit_recipients)
    return 0


if __name__ == "__main__":
    sys.exit(main())
