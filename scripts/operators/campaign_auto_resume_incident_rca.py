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

A recipient is ``clean`` only when every one of those sources was read and
nothing was found. When a pre-window copy cannot be tied to exactly one
recipient (no wamid, no conversation/customer, or two owners), no recipient
without other evidence can be proven clean: they are ``unresolved_evidence``
and the count of unplaceable rows is reported. If a source cannot be read the
tool exits non-zero instead of reporting.

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
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

# Strongest first. ``unresolved_evidence`` outranks only ``clean``.
EVIDENCE_ORDER = (
    "read", "delivered", "accepted", "uncertain", "in_flight", "failed", "unknown",
    "sent_row", "wamid_row", "unresolved_evidence",
)
_NORM = "regexp_replace({col}, '[^0-9]', '', 'g')"


class SourceUnreadable(RuntimeError):
    pass


def _ts(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _ids(raw: Optional[str]) -> List[int]:
    return sorted({int(x) for x in (raw or "").replace(" ", "").split(",") if x})


def _digits(p: Any) -> str:
    return re.sub(r"\D", "", str(p or ""))


def _strongest(kinds: Iterable[str]) -> str:
    ks = set(kinds)
    for k in EVIDENCE_ORDER:
        if k in ks:
            return k
    return "clean"


def _event_wamid(md: Dict[str, Any]) -> Optional[str]:
    return md.get("wa_message_id") or (md.get("provider_send") or {}).get("wamid") or None


def _event_kinds(md: Dict[str, Any]) -> Set[str]:
    """Evidence one outbound campaign copy carries. A copy with a wamid was
    accepted; without one its outcome is unknown."""
    ks: Set[str] = set()
    if _event_wamid(md):
        ks.add("accepted")
    else:
        ks.add("unknown")
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

    n = _NORM.format(col="customer_phone_e164")

    # ── the window ──────────────────────────────────────────────────────
    attempts = q("campaign_send_attempts",
        f"SELECT id, send_log_id, {n}, state, provider_message_id, "
        "request_started_at IS NOT NULL, accepted_at IS NOT NULL, delivered_at IS NOT NULL, "
        "read_at IS NOT NULL, failed_at IS NOT NULL, post_accept_error_code, error_code, "
        "messaging_scope_key FROM campaign_send_attempts WHERE tenant_id = :t AND campaign_id = :c "
        "AND claimed_at >= :s AND claimed_at <= :u ORDER BY id")
    log_ids = sorted({int(a[1]) for a in attempts if a[1] is not None})
    phones = sorted({a[2] for a in attempts if a[2]})
    window_wamids = {a[4] for a in attempts if a[4]}
    by_phone_logs: Dict[str, Set[int]] = {}
    for a in attempts:
        if a[2] and a[1] is not None:
            by_phone_logs.setdefault(a[2], set()).add(int(a[1]))
    accepted_phones = {a[2] for a in attempts if a[6] and a[2]}
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
        "recipients_with_more_than_one_claim": sum(
            1 for k in Counter(a[2] for a in attempts).values() if k > 1),
        "scope_key_kinds": dict(Counter((a[12] or "").split(":", 1)[0] for a in attempts)),
        "send_log_ids_claimed": log_ids,
    }

    # ── the unapproved rows (as listed before the window) ───────────────
    unapproved = sorted(set(unapproved_ids))
    un_phones: Dict[int, str] = {}
    if unapproved:
        for i, ph in q("campaign_send_logs",
                       f"SELECT id, {n} FROM campaign_send_logs WHERE tenant_id = :t "
                       "AND campaign_id = :c AND id = ANY(:ids)", ids=unapproved):
            un_phones[int(i)] = ph
    phone_set = set(phones)
    intersection = {
        "unapproved_listed": len(unapproved),
        "unapproved_found_in_campaign": len(un_phones),
        "claimed_rows_that_are_unapproved": len(set(log_ids) & set(unapproved)),
        "unapproved_rows_whose_recipient_was_claimed": sum(
            1 for ph in un_phones.values() if ph in phone_set),
        "unapproved_rows_whose_recipient_was_accepted": sum(
            1 for ph in un_phones.values() if ph in accepted_phones),
        "unapproved_ids_hit_by_row": sorted(set(log_ids) & set(unapproved)),
        "unapproved_ids_hit_by_recipient": sorted(
            i for i, ph in un_phones.items() if ph in phone_set),
        "claimed_send_log_ids_for_those_recipients": sorted(
            {i for ph in set(un_phones.values()) if ph in by_phone_logs for i in by_phone_logs[ph]}),
    }

    # ── evidence before the window, per claimed recipient ───────────────
    evidence: Dict[str, Set[str]] = {ph: set() for ph in phones}
    source_hits: Dict[str, Counter] = {s: Counter() for s in (
        "campaign_send_attempts", "campaign_send_logs", "message_events",
        "message_delivery_events")}
    unplaceable = Counter()

    def add(ph: str, kinds: Iterable[str], source: str) -> None:
        ks = set(kinds)
        if ph in evidence and ks:
            evidence[ph] |= ks
            source_hits[source].update(ks)

    if phones:
        # 1. earlier ledger attempts, any campaign of the tenant
        for ph, st, dl, rd, acc, fl in q("campaign_send_attempts",
                f"SELECT {n}, state, delivered_at IS NOT NULL, read_at IS NOT NULL, "
                "accepted_at IS NOT NULL, failed_at IS NOT NULL FROM campaign_send_attempts "
                f"WHERE tenant_id = :t AND claimed_at < :s AND {n} = ANY(:ph)", ph=phones):
            ks = set()
            if rd:
                ks.update(("read", "delivered"))
            if dl:
                ks.add("delivered")
            if acc or st == "accepted":
                ks.add("accepted")
            if st == "uncertain":
                ks.add("uncertain")
            if st in ("claimed", "request_started"):
                ks.add("in_flight")
            if fl or st == "rejected":
                ks.add("failed")
            add(ph, ks, "campaign_send_attempts")

        # 2. this campaign's rows for the recipient
        for i, ph, st, wamid, dl, rd, sent_before in q("campaign_send_logs",
                f"SELECT id, {n}, status, provider_message_id IS NOT NULL, "
                "delivered_at IS NOT NULL AND delivered_at < :s, read_at IS NOT NULL AND read_at < :s, "
                "sent_at IS NOT NULL AND sent_at < :s FROM campaign_send_logs "
                f"WHERE tenant_id = :t AND campaign_id = :c AND {n} = ANY(:ph)", ph=phones):
            st = (st or "").lower()
            ks = set()
            if int(i) in by_phone_logs.get(ph, set()):
                # The row claimed in the window: only what predates the window.
                if rd:
                    ks.update(("read", "delivered"))
                if dl:
                    ks.add("delivered")
                if sent_before:
                    ks.add("sent_row")
            else:
                if rd or st == "read":
                    ks.update(("read", "delivered"))
                if dl or st == "delivered":
                    ks.add("delivered")
                if st in ("uncertain", "sending"):
                    ks.add("uncertain")
                if sent_before or st == "sent":
                    ks.add("sent_row")
                if wamid:
                    ks.add("wamid_row")
            add(ph, ks, "campaign_send_logs")

    # Who owns each known wamid (attempts and rows of this campaign), so a
    # copy with no conversation link can still be placed.
    wamid_owner: Dict[str, Set[str]] = {}
    for w, ph in q("campaign_send_attempts",
                   f"SELECT provider_message_id, {n} FROM campaign_send_attempts "
                   "WHERE tenant_id = :t AND campaign_id = :c AND provider_message_id IS NOT NULL"):
        wamid_owner.setdefault(w, set()).add(ph)
    for w, ph in q("campaign_send_logs",
                   f"SELECT provider_message_id, {n} FROM campaign_send_logs "
                   "WHERE tenant_id = :t AND campaign_id = :c AND provider_message_id IS NOT NULL"):
        wamid_owner.setdefault(w, set()).add(ph)

    # 3. this campaign's outbound copies in message_events, before the window
    prior_wamids: Dict[str, str] = {}
    events = q("message_events",
        "SELECT me.id, me.metadata, cu.phone, cu.normalized_phone, "
        "conv.metadata->>'customer_phone' FROM message_events me "
        "LEFT JOIN conversations conv ON conv.id = me.conversation_id AND conv.tenant_id = me.tenant_id "
        "LEFT JOIN customers cu ON cu.id = conv.customer_id AND cu.tenant_id = me.tenant_id "
        "WHERE me.tenant_id = :t AND me.direction = 'outbound' AND me.event_type = 'campaign' "
        "AND (me.metadata->>'campaign_id') = CAST(:c AS text) "
        "AND (me.created_at IS NULL OR me.created_at < :s)")
    for _ev_id, md, cu_phone, cu_norm, conv_phone in events:
        md = md or {}
        wamid = _event_wamid(md)
        if wamid and wamid in window_wamids:
            continue                           # the window's own copy
        linked = {_digits(x) for x in (cu_phone, cu_norm, conv_phone) if _digits(x)}
        owners = set(wamid_owner.get(wamid, set())) if wamid else set()
        if linked and owners and not (linked & owners):
            unplaceable["attribution_conflict"] += 1
            for ph in linked | owners:
                add(ph, {"unresolved_evidence"}, "message_events")
            continue
        who = (linked & owners) or linked or owners
        if len(who) != 1:
            unplaceable["no_recipient_link" if not who else "more_than_one_recipient"] += 1
            for ph in who:
                add(ph, {"unresolved_evidence"}, "message_events")
            continue
        ph = next(iter(who))
        add(ph, _event_kinds(md), "message_events")
        if wamid:
            prior_wamids[wamid] = ph

    # 4. receipts for those copies, and for this campaign's rows
    if prior_wamids:
        for w, st in q("message_delivery_events",
                       "SELECT wamid, status FROM message_delivery_events WHERE tenant_id = :t "
                       "AND wamid = ANY(:w) AND (occurred_at IS NULL OR occurred_at < :s)",
                       w=sorted(prior_wamids)):
            add(prior_wamids[w], _status_kinds(st), "message_delivery_events")
    for w, st, ph in q("message_delivery_events",
            f"SELECT mde.wamid, mde.status, {_NORM.format(col='sl.customer_phone_e164')} "
            "FROM message_delivery_events mde JOIN campaign_send_logs sl ON sl.id = mde.campaign_send_log_id "
            "AND sl.tenant_id = mde.tenant_id WHERE mde.tenant_id = :t AND sl.campaign_id = :c "
            "AND (mde.occurred_at IS NULL OR mde.occurred_at < :s)"):
        if w in window_wamids or (w or "").startswith("synth:"):
            continue
        add(ph, _status_kinds(st), "message_delivery_events")

    # A pre-window copy nobody can be tied to could be anyone's: nobody
    # without other evidence is provably clean.
    if sum(unplaceable.values()):
        for ph, ks in evidence.items():
            if not ks:
                ks.add("unresolved_evidence")

    strongest = {ph: _strongest(ks) for ph, ks in evidence.items()}
    history = {
        "by_strongest_evidence": dict(Counter(strongest.values())),
        "accepted_recipients_by_strongest_evidence": dict(
            Counter(s for ph, s in strongest.items() if ph in accepted_phones)),
        "send_log_ids_by_strongest_evidence": {
            k: sorted(i for ph, s in strongest.items() if s == k for i in by_phone_logs.get(ph, ()))
            for k in EVIDENCE_ORDER + ("clean",) if any(s == k for s in strongest.values())
        },
        "evidence_hits_by_source": {s: dict(c) for s, c in source_hits.items() if c},
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
