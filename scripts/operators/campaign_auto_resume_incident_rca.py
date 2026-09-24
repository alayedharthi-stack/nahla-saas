#!/usr/bin/env python3
"""Read-only RCA of the sends a campaign made in one time window — built for
the 2026-09-24 incident where campaign 35 was auto-resumed at 13:47:43Z and
~99 messages were accepted before ``provider_throttling`` paused it.

The base set is every ledger attempt CLAIMED in ``[--since, --until]``.
A claim is not a send, so the stages are reported separately:
claimed → request started → accepted by Meta → delivered / read, plus
failed-after-accept (with its error keys, e.g. ``marketing_blocked`` =
Meta 131049) and rejected / not-sent / uncertain.

For every recipient claimed in the window it reports the strongest evidence
that existed BEFORE ``--since``, from every source the reconciliation uses
for a recipient's history:

* ``campaign_send_attempts`` — earlier attempts of any campaign of the tenant;
* ``campaign_send_logs`` — this campaign's other rows for the recipient
  (the same number in another format), and what the row sent in the window
  already carried before it;
* ``message_events`` — this campaign's outbound copies (one per accepted
  copy, including first copies whose wamid a second copy overwrote on the
  row), tied to the recipient through their conversation's customer or the
  attempt/row that owns the wamid, with their receipt flags;
* ``message_delivery_events`` — receipts for those copies' wamids and for
  this campaign's rows.

Recipients are matched by the platform's validated identity
(``services.recipient_identity``: ``+966…``, ``966…``, ``00966…`` and
``05…`` are one person) — the same rule the send guard uses. Prior attempts
are classified from their raw fields (wamid, accepted / receipt times,
request start), not from the state label alone.

A recipient is ``clean`` only when every one of those sources was read and
nothing was found. It is ``unresolved_evidence`` instead when its own number
has no validated identity, when a pre-window copy may be its (a spelling
without identity whose digits are its number's tail), or when any pre-window
copy cannot be tied to anyone at all (no identity, no wamid owner) — such a
copy could be anyone's. If a source cannot be read the tool exits non-zero
instead of reporting.

It also intersects the window with ``--unapproved-send-log-ids`` — the rows
the reconciliation did not approve, as listed (``planned_changes``) by the
quarantine dry run that ran BEFORE the window — by row and by recipient.

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
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "backend", ROOT / "database"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from services.recipient_identity import (  # noqa: E402
    MATCH_SUFFIX_DIGITS, canonical_recipient, digits, may_be_recipient,
)

# Strongest first. ``unresolved_evidence`` outranks only ``clean``.
EVIDENCE_ORDER = (
    "read", "delivered", "accepted", "uncertain", "request_started", "failed", "unknown",
    "sent_row", "wamid_row", "unresolved_evidence",
)
# A claim whose request never started is durable proof that nothing left
# (the request is marked started before it is sent): reported, not evidence.
NOT_EVIDENCE = ("claimed_never_started",)
# The owner's table: recipients with each kind of prior evidence (a
# recipient can count in several), then those with none.
TABLE_KINDS = ("accepted", "delivered", "read", "uncertain", "request_started")
PROVEN_NOT_ACCEPTED = ("rejected", "not_sent", "abandoned")
KNOWN_STATES = ("claimed", "request_started", "accepted", "uncertain") + PROVEN_NOT_ACCEPTED


class SourceUnreadable(RuntimeError):
    pass


def _ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _ids(raw: Optional[str]) -> List[int]:
    return sorted({int(x) for x in (raw or "").replace(" ", "").split(",") if x})


def _strongest(kinds: Iterable[str]) -> str:
    ks = set(kinds) - set(NOT_EVIDENCE)
    for k in EVIDENCE_ORDER:
        if k in ks:
            return k
    return "clean"


def _json(md: Any) -> Dict[str, Any]:
    if isinstance(md, str):
        try:
            md = json.loads(md)
        except ValueError:
            return {}
    return md if isinstance(md, dict) else {}


def _event_wamid(md: Dict[str, Any]) -> Optional[str]:
    return md.get("wa_message_id") or (md.get("provider_send") or {}).get("wamid") or None


def _event_kinds(md: Dict[str, Any]) -> Set[str]:
    """Evidence one outbound campaign copy carries. A copy with a wamid was
    accepted; without one its outcome is unknown."""
    ks: Set[str] = {"accepted"} if _event_wamid(md) else {"unknown"}
    if md.get("_status_read"):
        ks.update(("read", "delivered"))
    if md.get("_status_delivered"):
        ks.add("delivered")
    if md.get("_status_failed"):
        ks.add("failed")
    return ks


def _status_kinds(status: Any) -> Set[str]:
    st = str(status or "").lower()
    return {"read": {"read", "delivered"}, "delivered": {"delivered"},
            "failed": {"failed"}}.get(st, {"unknown"} if st else set())


def attempt_kinds(state: Any, wamid: Any, accepted: Any, delivered: Any, read: Any,
                  started: Any, failed: Any) -> Set[str]:
    """What one earlier attempt proves, from its raw fields first. A wamid,
    an acceptance or a receipt time is acceptance whatever the label says;
    a started request is ``request_started`` unless the attempt is a proven
    pre-accept failure; only a claim with nothing started and nothing
    accepted is ``claimed_never_started``. An unknown label is ``unknown``."""
    st = str(state or "").lower()
    ks: Set[str] = set()
    if read:
        ks.update(("read", "delivered", "accepted"))
    if delivered:
        ks.update(("delivered", "accepted"))
    if accepted or wamid:
        ks.add("accepted")
    if st == "uncertain":
        ks.add("uncertain")
    if st not in KNOWN_STATES:
        ks.add("unknown")
    if started and not ks and st not in ("rejected", "not_sent"):
        ks.add("request_started")
    if st == "request_started" and not (ks - {"request_started"}):
        ks.add("request_started")
    if failed or st in ("rejected", "not_sent"):
        ks.add("failed")
    if st == "claimed" and not started and not ks:
        ks.add("claimed_never_started")
    return ks


class _Recipients:
    """Evidence per window recipient, keyed by validated identity; spellings
    without identity are matched conservatively (``may_be_recipient``)."""

    def __init__(self, identities: Iterable[str]) -> None:
        self.evidence: Dict[str, Set[str]] = {i: set() for i in identities}
        self.by_tail: Dict[str, List[str]] = {}
        for i in self.evidence:
            self.by_tail.setdefault(digits(i)[-MATCH_SUFFIX_DIGITS:], []).append(i)

    def matching(self, stored: Any) -> List[str]:
        own = canonical_recipient(stored)
        if own is not None:
            return [own] if own in self.evidence else []
        d = digits(stored)
        return [i for i in self.by_tail.get(d[-MATCH_SUFFIX_DIGITS:], ()) if may_be_recipient(stored, i)]

    def add(self, stored: Any, kinds: Iterable[str], hits: Counter) -> None:
        exact = canonical_recipient(stored) is not None
        ks = set(kinds)
        for i in self.matching(stored):
            # A spelling without identity may be this recipient: that is
            # unresolved, never a proof either way.
            got = ks if exact else {"unresolved_evidence"}
            self.evidence[i] |= got
            hits.update(got)


def analyse(conn: Any, *, tenant_id: int, campaign_id: int, since: datetime, until: datetime,
            unapproved_ids: Sequence[int]) -> Dict[str, Any]:
    from sqlalchemy import text  # noqa: PLC0415
    from sqlalchemy.exc import SQLAlchemyError  # noqa: PLC0415
    read_sources: List[str] = []

    def q(source: str, sql: str, **p: Any) -> List[Any]:
        try:
            rows = conn.execute(text(sql), {"t": tenant_id, "c": campaign_id, "s": since,
                                            "u": until, **p}).all()
        except SQLAlchemyError as exc:
            raise SourceUnreadable(f"{source}: {type(exc).__name__}") from None
        if source not in read_sources:
            read_sources.append(source)
        return rows

    # ── the window ──────────────────────────────────────────────────────
    attempts = q("campaign_send_attempts",
        "SELECT id, send_log_id, customer_phone_e164, state, provider_message_id, "
        "request_started_at IS NOT NULL, accepted_at IS NOT NULL, delivered_at IS NOT NULL, "
        "read_at IS NOT NULL, failed_at IS NOT NULL, post_accept_error_code, error_code, "
        "messaging_scope_key FROM campaign_send_attempts WHERE tenant_id = :t AND campaign_id = :c "
        "AND claimed_at >= :s AND claimed_at <= :u ORDER BY id")
    key_of: Dict[int, str] = {}
    unresolved_identity: Set[str] = set()
    for a in attempts:
        ident = canonical_recipient(a[2])
        if ident is None:
            ident = f"unresolved-identity:{a[1]}"
            unresolved_identity.add(ident)
        key_of[int(a[0])] = ident
    log_ids = sorted({int(a[1]) for a in attempts if a[1] is not None})
    phones = sorted(set(key_of.values()))
    window_wamids = {a[4] for a in attempts if a[4]}
    by_phone_logs: Dict[str, Set[int]] = {}
    for a in attempts:
        if a[1] is not None:
            by_phone_logs.setdefault(key_of[int(a[0])], set()).add(int(a[1]))
    accepted_phones = {key_of[int(a[0])] for a in attempts if a[6]}
    window = {
        "attempts_claimed_in_window": len(attempts),
        "requests_started_in_window": sum(1 for a in attempts if a[5]),
        "accepted_in_window": sum(1 for a in attempts if a[6]),
        "accepted_with_wamid": sum(1 for a in attempts if a[6] and a[4]),
        "delivered": sum(1 for a in attempts if a[7] or a[8]),
        "read": sum(1 for a in attempts if a[8]),
        "failed_after_accept": sum(1 for a in attempts if a[6] and a[9]),
        "post_accept_error_keys": dict(Counter(a[10] for a in attempts if a[10])),
        "not_accepted_error_keys": dict(Counter(a[11] for a in attempts if a[11] and not a[6])),
        "by_state": dict(Counter(a[3] for a in attempts)),
        "send_log_rows_claimed": len(log_ids),
        "recipients_claimed": len(phones),
        "recipients_accepted": len(accepted_phones),
        "recipients_without_validated_identity": len(unresolved_identity),
        "recipients_with_more_than_one_claim": sum(
            1 for k in Counter(key_of.values()).values() if k > 1),
        "scope_key_kinds": dict(Counter((a[12] or "").split(":", 1)[0] for a in attempts)),
        "send_log_ids_claimed": log_ids,
    }

    # ── the unapproved rows (as listed before the window) ───────────────
    unapproved = sorted(set(unapproved_ids))
    un_keys: Dict[int, str] = {}
    if unapproved:
        for i, ph in q("campaign_send_logs",
                       "SELECT id, customer_phone_e164 FROM campaign_send_logs WHERE tenant_id = :t "
                       "AND campaign_id = :c AND id = ANY(:ids)", ids=unapproved):
            un_keys[int(i)] = canonical_recipient(ph) or f"unresolved-identity:{i}"
    phone_set = set(phones)
    intersection = {
        "unapproved_listed": len(unapproved),
        "unapproved_found_in_campaign": len(un_keys),
        "claimed_rows_that_are_unapproved": len(set(log_ids) & set(unapproved)),
        "unapproved_rows_whose_recipient_was_claimed": sum(
            1 for k in un_keys.values() if k in phone_set),
        "unapproved_rows_whose_recipient_was_accepted": sum(
            1 for k in un_keys.values() if k in accepted_phones),
        "unapproved_ids_hit_by_row": sorted(set(log_ids) & set(unapproved)),
        "unapproved_ids_hit_by_recipient": sorted(
            i for i, k in un_keys.items() if k in phone_set),
        "claimed_send_log_ids_for_those_recipients": sorted(
            {i for k in set(un_keys.values()) if k in by_phone_logs for i in by_phone_logs[k]}),
    }

    # ── evidence before the window, per claimed recipient ───────────────
    recips = _Recipients(p for p in phones if p not in unresolved_identity)
    hits: Dict[str, Counter] = {s: Counter() for s in (
        "campaign_send_attempts", "campaign_send_logs", "message_events",
        "message_delivery_events")}
    unplaceable = Counter()

    # 1. earlier ledger attempts, any campaign of the tenant
    for ph, *raw in q("campaign_send_attempts",
            "SELECT customer_phone_e164, state, provider_message_id, accepted_at IS NOT NULL, "
            "delivered_at IS NOT NULL, read_at IS NOT NULL, request_started_at IS NOT NULL, "
            "failed_at IS NOT NULL FROM campaign_send_attempts "
            "WHERE tenant_id = :t AND claimed_at < :s"):
        recips.add(ph, attempt_kinds(*raw), hits["campaign_send_attempts"])

    # 2. this campaign's rows
    claimed_rows = set(log_ids)
    for i, ph, st, wamid, dl, rd, sent_before in q("campaign_send_logs",
            "SELECT id, customer_phone_e164, status, provider_message_id IS NOT NULL, "
            "delivered_at IS NOT NULL AND delivered_at < :s, read_at IS NOT NULL AND read_at < :s, "
            "sent_at IS NOT NULL AND sent_at < :s FROM campaign_send_logs "
            "WHERE tenant_id = :t AND campaign_id = :c"):
        st = (st or "").lower()
        ks: Set[str] = set()
        if int(i) in claimed_rows:
            # The row claimed in the window: only what predates the window.
            if rd:
                ks.update(("read", "delivered"))
            if dl:
                ks.add("delivered")
            if sent_before:
                ks.update(("sent_row", "accepted"))
        else:
            if rd or st == "read":
                ks.update(("read", "delivered"))
            if dl or st == "delivered":
                ks.add("delivered")
            if st in ("uncertain", "sending"):
                ks.add("uncertain")
            # A stored sent_at / wamid is Meta's acceptance of that copy.
            if sent_before or st == "sent":
                ks.update(("sent_row", "accepted"))
            if wamid:
                ks.update(("wamid_row", "accepted"))
        recips.add(ph, ks, hits["campaign_send_logs"])

    # Who owns each wamid of this campaign (attempts, rows, receipts linked
    # to rows) — places a copy that has no conversation link.
    owners: Dict[str, Set[str]] = {}
    for w, ph in q("campaign_send_attempts",
            "SELECT provider_message_id, customer_phone_e164 FROM campaign_send_attempts "
            "WHERE tenant_id = :t AND campaign_id = :c AND provider_message_id IS NOT NULL "
            "UNION ALL SELECT provider_message_id, customer_phone_e164 FROM campaign_send_logs "
            "WHERE tenant_id = :t AND campaign_id = :c AND provider_message_id IS NOT NULL "
            "UNION ALL SELECT mde.wamid, sl.customer_phone_e164 FROM message_delivery_events mde "
            "JOIN campaign_send_logs sl ON sl.id = mde.campaign_send_log_id "
            "WHERE sl.tenant_id = :t AND sl.campaign_id = :c"):
        owners.setdefault(w, set()).add(ph)

    # 3. this campaign's outbound copies in message_events, before the window
    prior_wamids: Dict[str, List[str]] = {}
    events = q("message_events",
        "SELECT me.id, me.metadata, cu.phone, cu.normalized_phone, "
        "conv.metadata->>'customer_phone' FROM message_events me "
        "LEFT JOIN conversations conv ON conv.id = me.conversation_id AND conv.tenant_id = me.tenant_id "
        "LEFT JOIN customers cu ON cu.id = conv.customer_id AND cu.tenant_id = me.tenant_id "
        "WHERE me.tenant_id = :t AND me.direction = 'outbound' AND me.event_type = 'campaign' "
        "AND (me.metadata->>'campaign_id') = CAST(:c AS text) "
        "AND (me.created_at IS NULL OR me.created_at < :s)")
    for _ev_id, md, cu_phone, cu_norm, conv_phone in events:
        md = _json(md)
        wamid = _event_wamid(md)
        if wamid and wamid in window_wamids:
            continue                           # the window's own copy
        linked = [x for x in (cu_phone, cu_norm, conv_phone) if x]
        owned = sorted(owners.get(wamid, ())) if wamid else []
        linked_ids = {canonical_recipient(x) for x in linked} - {None}
        owned_ids = {canonical_recipient(x) for x in owned} - {None}
        loose = [x for x in linked + owned if canonical_recipient(x) is None
                 and len(digits(x).lstrip("0")) >= MATCH_SUFFIX_DIGITS]
        if linked_ids and owned_ids and not (linked_ids & owned_ids):
            unplaceable["attribution_conflict"] += 1
            for i in linked_ids | owned_ids:
                recips.add(i, {"unresolved_evidence"}, hits["message_events"])
            continue
        who = (linked_ids & owned_ids) or linked_ids or owned_ids
        if len(who) > 1:
            unplaceable["more_than_one_recipient"] += 1
            for i in who:
                recips.add(i, {"unresolved_evidence"}, hits["message_events"])
            continue
        for x in loose:
            recips.add(x, {"unresolved_evidence"}, hits["message_events"])
        if not who:
            if not loose:
                unplaceable["no_recipient_link"] += 1
            continue
        ident = next(iter(who))
        recips.add(ident, _event_kinds(md), hits["message_events"])
        if wamid:
            prior_wamids.setdefault(wamid, []).append(ident)

    # 4. receipts for those copies, and for this campaign's rows
    if prior_wamids:
        for w, st in q("message_delivery_events",
                       "SELECT wamid, status FROM message_delivery_events WHERE tenant_id = :t "
                       "AND wamid = ANY(:w) AND (occurred_at IS NULL OR occurred_at < :s)",
                       w=sorted(prior_wamids)):
            for ident in prior_wamids[w]:
                recips.add(ident, _status_kinds(st), hits["message_delivery_events"])
    for w, st, ph in q("message_delivery_events",
            "SELECT mde.wamid, mde.status, sl.customer_phone_e164 "
            "FROM message_delivery_events mde JOIN campaign_send_logs sl ON sl.id = mde.campaign_send_log_id "
            "AND sl.tenant_id = mde.tenant_id WHERE mde.tenant_id = :t AND sl.campaign_id = :c "
            "AND (mde.occurred_at IS NULL OR mde.occurred_at < :s)"):
        if w in window_wamids or (w or "").startswith("synth:"):
            continue
        recips.add(ph, _status_kinds(st), hits["message_delivery_events"])

    evidence: Dict[str, Set[str]] = {p: set(recips.evidence.get(p, ())) for p in phones}
    for p in unresolved_identity:
        evidence[p].add("unresolved_evidence")
    # A pre-window copy nobody can be tied to could be anyone's: no
    # recipient whose evidence is otherwise clean is provably clean.
    if sum(unplaceable.values()):
        for ks in evidence.values():
            if _strongest(ks) == "clean":
                ks.add("unresolved_evidence")

    strongest = {ph: _strongest(ks) for ph, ks in evidence.items()}

    def table(pop: Iterable[str]) -> Dict[str, int]:
        pop = list(pop)
        row = {"new_recipients": len(pop)}
        for k in TABLE_KINDS:
            row[f"with_prior_{k}"] = sum(1 for ph in pop if k in evidence[ph])
        row["unresolved_evidence"] = sum(1 for ph in pop if strongest[ph] == "unresolved_evidence")
        row["clean"] = sum(1 for ph in pop if strongest[ph] == "clean")
        row["duplicate_incident"] = sum(
            1 for ph in pop if evidence[ph] & {"accepted", "delivered", "read", "uncertain"})
        return row
    history = {
        "table_accepted_in_window": table(ph for ph in phones if ph in accepted_phones),
        "table_claimed_in_window": table(phones),
        "recipients_with_prior_claim_never_started": sum(
            1 for ks in evidence.values() if "claimed_never_started" in ks),
        "by_strongest_evidence": dict(Counter(strongest.values())),
        "accepted_recipients_by_strongest_evidence": dict(
            Counter(s for ph, s in strongest.items() if ph in accepted_phones)),
        "send_log_ids_by_strongest_evidence": {
            k: sorted(i for ph, s in strongest.items() if s == k for i in by_phone_logs.get(ph, ()))
            for k in EVIDENCE_ORDER + ("clean",) if any(s == k for s in strongest.values())
        },
        "evidence_hits_by_source": {s: dict(c) for s, c in hits.items() if c},
        "pre_window_campaign_copies_read": len(events),
        "pre_window_copies_not_placeable": dict(unplaceable),
        "sources_read": read_sources,
    }

    counts = dict(q("campaign_send_logs",
                    "SELECT status, count(*) FROM campaign_send_logs WHERE tenant_id = :t "
                    "AND campaign_id = :c GROUP BY status"))
    lease = q("campaign_dispatch_leases",
              "SELECT pause_reason, stop_requested_at IS NOT NULL, owner IS NOT NULL, paused_at "
              "FROM campaign_dispatch_leases WHERE campaign_id = :c")
    return {
        "window": {"since": since.isoformat() + "Z", "until": until.isoformat() + "Z", **window},
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
            try:
                out = analyse(conn, tenant_id=args.tenant_id, campaign_id=args.campaign_id,
                              since=_ts(args.since), until=_ts(args.until),
                              unapproved_ids=_ids(args.unapproved_send_log_ids))
            except SourceUnreadable as exc:
                print(json.dumps({"error": "evidence source unreadable; no recipient classified",
                                  "source": str(exc)}))
                return 3
            finally:
                conn.rollback()
    finally:
        engine.dispose()
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
