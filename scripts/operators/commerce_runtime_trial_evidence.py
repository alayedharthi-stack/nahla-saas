"""Read what a trial actually recorded, and claim only what the rows prove.

A trial is judged from the runtime's own durable rows, not from a transcript
and not from the log lines that scrolled past while it ran. ``handover status``
answers one question — is there outstanding work — and answers it in counts.
That is the right question before a handover and the wrong one after a trial,
where what matters is per-turn: which inbound was admitted, what terminal it
reached, whether exactly one reply intent was reserved for it, whether exactly
one send was accepted, and whether anything claimed a customer was reached
without a receipt saying so.

This job reads those rows for the **allowlisted tenants only**, inside a stated
window, and renders them with identifiers masked. It writes nothing: the
transaction is opened read-only and every statement is a ``SELECT``.

What it will not do
-------------------
Judge a thing it has no rows for. Every verdict is one of three words:

* ``proven``       — rows were found and they support the claim;
* ``refused``      — rows were found and they contradict it, and the offending
                     turns are named;
* ``not_observed`` — there is nothing to judge. A window with no turns proves
                     nothing at all, and saying so is the honest answer; a
                     clean report over an empty window is a false one.

The claims are a closed set (:data:`CLAIMS`). Each is a property the pilot's
own design says must hold, expressed so that it fails loudly rather than
quietly: ``customer_reach=reached`` with no ``delivered``/``read`` receipt
behind it is a false operational claim, whether or not the pilot is expected to
record such receipts today.

Reading a trial
---------------

    COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST=1 \\
      python -m scripts.operators.commerce_runtime_trial_evidence \\
        --since 2026-09-21T06:00:00Z

Every line is prefixed ``[COMMERCE_RUNTIME_TRIAL]``. ``RESULT=REPORTED`` means
the report was produced; ``RESULT=REFUSED`` means at least one claim was
contradicted by the rows, and exit code 1 says so to whatever ran this.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "backend"),
              os.path.join(_REPO_ROOT, "database")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

LOG_PREFIX = "[COMMERCE_RUNTIME_TRIAL]"

RESULT_REPORTED = "REPORTED"
RESULT_REFUSED = "REFUSED"
RESULT_FAILED_PRECONDITION = "FAILED_PRECONDITION"
RESULT_FAILED = "FAILED"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_FAILED = 3

# A trial is a bounded thing. Reading past this many turns for one tenant means
# the window is not a trial's window, and a truncated report that looked
# complete would be worse than a refusal to produce one.
MAX_TURNS_INSPECTED = 500
DEFAULT_WINDOW_HOURS = 24

PROVEN = "proven"
REFUSED = "refused"
NOT_OBSERVED = "not_observed"

# Receipt kinds that establish the provider accepted a send, and the ones that
# establish the customer was actually reached. They are deliberately separate:
# acceptance is the provider's word about itself, reach is evidence about the
# customer, and the pilot records the first and not the second.
ACCEPTING_RECEIPTS = frozenset({"accepted"})
REACH_RECEIPTS = frozenset({"delivered", "read"})

# Normal runtime handling resolves a row without an operator disposition.
# These are the distinct, supported operator dispositions, not reply outcomes.
DEFERRED_DISPOSITIONS = frozenset({
    "replayed", "answered", "superseded", "not_required", "unanswered",
})

# The closed set of claims this report is willing to make about a trial.
CLAIMS: Tuple[str, ...] = (
    "every_admitted_turn_reached_a_terminal",
    "at_most_one_reply_intent_per_turn",
    "at_most_one_accepted_send_per_reply_intent",
    "no_unknown_send_was_reported_completed",
    "customer_reach_is_never_claimed_without_a_receipt",
    "no_commerce_write_was_reserved",
    "every_deferred_inbound_is_accounted_for",
)


def emit(message: str) -> None:
    print(f"{LOG_PREFIX} {message}", flush=True)


def result(marker: str, **observations: Any) -> None:
    body = " ".join(f"{name}={value!r}" for name, value in observations.items())
    emit(f"RESULT={marker} {body}".rstrip())


# ── Masking ──────────────────────────────────────────────────────────────────

def mask_phone(value: Any) -> str:
    """A recipient, rendered for a report that leaves this session.

    Enough digits to tell two test handsets apart, never enough to be the
    number. A value that is not a phone number is masked as a reference rather
    than guessed at.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return "***"
    if len(digits) <= 6:
        return f"***{digits[-2:]}"
    return f"+{digits[:4]}*****{digits[-2:]}"


def mask_reference(value: Any, *, keep_prefix: int = 6, keep_suffix: int = 4) -> str:
    """A provider message id or conversation reference, shortened for a report."""
    text = str(value or "")
    if not text:
        return "***"
    if len(text) <= keep_prefix + keep_suffix + 1:
        return f"{text[:2]}***"
    return f"{text[:keep_prefix]}…{text[-keep_suffix:]}"


# ── Reading one turn's rows ──────────────────────────────────────────────────

def turn_report(turn: Mapping[str, Any]) -> Dict[str, Any]:
    """One admitted turn, rendered from its own rows.

    ``turn`` carries what the tables hold for it: the turn row, its terminal
    (or ``None`` when it never reached one), and the delivery sequences
    reserved for it, each with its attempts and their receipts. Nothing is
    inferred that the rows do not say — a turn with no terminal is reported as
    having no terminal, not as failed.
    """
    terminal = turn.get("terminal") or None
    sequences = list(turn.get("sequences") or ())

    accepted_ids: List[str] = []
    reach_receipts: List[str] = []
    receipt_kinds: List[str] = []
    for sequence in sequences:
        for attempt in sequence.get("attempts") or ():
            for receipt in attempt.get("receipts") or ():
                kind = str(receipt.get("kind") or "")
                receipt_kinds.append(kind)
                if kind in ACCEPTING_RECEIPTS:
                    accepted_ids.append(str(receipt.get("provider_message_id") or ""))
                if kind in REACH_RECEIPTS:
                    reach_receipts.append(kind)

    return {
        "turn_id": turn.get("turn_id"),
        "conversation_ref": mask_reference(turn.get("conversation_ref")),
        "inbound_message_id": mask_reference(turn.get("provider_message_id")),
        "admitted_at": turn.get("admitted_at"),
        "terminal": None if terminal is None else {
            "processing_outcome": terminal.get("processing_outcome"),
            "transport_outcome": terminal.get("transport_outcome"),
            "customer_reach": terminal.get("customer_reach"),
        },
        "reply_intents": len(sequences),
        "reply_outcomes": [str(s.get("outcome") or "") for s in sequences],
        "accepted_sends": len(accepted_ids),
        "accepted_message_ids": [mask_reference(v) for v in accepted_ids if v],
        "receipt_kinds": sorted(set(receipt_kinds)),
        "reach_receipts": len(reach_receipts),
    }


# ── Judging the whole window ─────────────────────────────────────────────────

def _claim(verdict: str, why: str, rows: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    return {"verdict": verdict, "why": why, "turns": list(rows or ())}


def verdicts(turns: Sequence[Mapping[str, Any]], *,
             effects_reserved: Optional[int] = None,
             deferred: Optional[Sequence[Mapping[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """Judge the closed set of claims against the rows, and only against them.

    ``turns`` are the raw per-turn structures, ``effects_reserved`` the number
    of commerce effects this tenant reserved in the window (``None`` when that
    table was not read), ``deferred`` the deferred inbound rows.

    A claim with nothing to judge is ``not_observed``. That is the whole point:
    an empty window must not read as a clean trial.
    """
    found: Dict[str, Dict[str, Any]] = {}
    reports = [turn_report(t) for t in turns]

    # 1. Every admitted turn reached a terminal.
    if not reports:
        found[CLAIMS[0]] = _claim(NOT_OBSERVED, "no turn was admitted in this window")
    else:
        open_turns = [r["turn_id"] for r in reports if r["terminal"] is None]
        found[CLAIMS[0]] = _claim(
            REFUSED if open_turns else PROVEN,
            "a turn with no terminal is a customer owed an answer or an honest record"
            if open_turns else f"{len(reports)} admitted, {len(reports)} with a terminal",
            open_turns)

    # 2. At most one reply intent per turn.
    if not reports:
        found[CLAIMS[1]] = _claim(NOT_OBSERVED, "no turn was admitted in this window")
    else:
        extra = [r["turn_id"] for r in reports if r["reply_intents"] > 1]
        found[CLAIMS[1]] = _claim(
            REFUSED if extra else PROVEN,
            "a second reply intent for one inbound is a second answer"
            if extra else f"{len(reports)} turns, none with more than one reply intent",
            extra)

    # 3. At most one accepted send per reply intent.
    judged = [r for r in reports if r["reply_intents"]]
    if not judged:
        found[CLAIMS[2]] = _claim(NOT_OBSERVED, "no reply intent was reserved in this window")
    else:
        # Compare each intent independently. An unsent intent must not offset
        # two acceptances belonging to a different intent on the same turn.
        doubled = [t["turn_id"] for t in turns if any(
            sum(1 for a in s.get("attempts") or ()
                for r in a.get("receipts") or ()
                if r.get("kind") in ACCEPTING_RECEIPTS) > 1
            for s in t.get("sequences") or ()
        )]
        intent_count = sum(r["reply_intents"] for r in judged)
        found[CLAIMS[2]] = _claim(
            REFUSED if doubled else PROVEN,
            "one reply intent has more than one recorded accepted send"
            if doubled else f"{intent_count} reply intents, none with more than one recorded acceptance",
            doubled)

    # 4. An unknown send outcome was never reported as a completed turn.
    finished = [r for r in reports if r["terminal"] is not None]
    unknown = [r for r in finished if r["terminal"]["transport_outcome"] == "unknown"]
    if not unknown:
        found[CLAIMS[3]] = _claim(NOT_OBSERVED, "no send outcome was unknown in this window")
    else:
        lying = [r["turn_id"] for r in unknown
                 if r["terminal"]["processing_outcome"] == "completed"]
        found[CLAIMS[3]] = _claim(
            REFUSED if lying else PROVEN,
            "an unknown send is not evidence the customer was answered"
            if lying else f"{len(unknown)} unknown sends, none reported completed",
            lying)

    # 5. Customer reach is never claimed without a receipt establishing it.
    claimed = [r for r in finished if r["terminal"]["customer_reach"] == "reached"]
    if not claimed:
        found[CLAIMS[4]] = _claim(
            NOT_OBSERVED,
            "no turn claimed the customer was reached; the pilot records no delivery receipt, "
            "so acceptance stands alone and is reported as acceptance")
    else:
        unevidenced = [r["turn_id"] for r in claimed if not r["reach_receipts"]]
        found[CLAIMS[4]] = _claim(
            REFUSED if unevidenced else PROVEN,
            "'reached' without a delivered or read receipt is a claim with no evidence"
            if unevidenced else f"{len(claimed)} reach claims, each with a receipt behind it",
            unevidenced)

    # 6. No commerce write was reserved. The pilot has no commerce-write tool.
    if effects_reserved is None:
        found[CLAIMS[5]] = _claim(NOT_OBSERVED, "the effect ledger was not read")
    else:
        found[CLAIMS[5]] = _claim(
            REFUSED if effects_reserved else PROVEN,
            f"{effects_reserved} commerce effects were reserved; the pilot has no write tool"
            if effects_reserved else "no commerce effect was reserved in this window")

    # 7. Every deferred inbound is accounted for.
    rows = list(deferred or ())
    if deferred is None:
        found[CLAIMS[6]] = _claim(NOT_OBSERVED, "the deferred inbound table was not read")
    elif not rows:
        found[CLAIMS[6]] = _claim(NOT_OBSERVED, "no inbound was deferred in this window")
    else:
        unresolved = [r.get("id") for r in rows if not (
            (r.get("state") == "resolved" and r.get("disposition") is None)
            or (r.get("state") == "disposed"
                and r.get("disposition") in DEFERRED_DISPOSITIONS)
        )]
        found[CLAIMS[6]] = _claim(
            REFUSED if unresolved else PROVEN,
            "an inbound is pending or has inconsistent resolution/disposition state"
            if unresolved else f"{len(rows)} inbounds, each recorded as runtime-resolved or operator-disposed; "
                               "accounting alone is not a claim of customer delivery",
            unresolved)

    return found


def refused_claims(found: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """The claims the rows contradicted, in the closed set's own order."""
    return [name for name in CLAIMS if found.get(name, {}).get("verdict") == REFUSED]


# ── Rendering ────────────────────────────────────────────────────────────────

def render(tenant_id: int, reports: Sequence[Mapping[str, Any]],
           found: Mapping[str, Mapping[str, Any]]) -> List[str]:
    """The report, as the lines an operator reads."""
    lines = [f"tenant={tenant_id} turns={len(reports)}"]
    for report in reports:
        terminal = report["terminal"]
        shape = ("no_terminal" if terminal is None else
                 f"{terminal['processing_outcome']}/{terminal['transport_outcome']}"
                 f"/reach={terminal['customer_reach']}")
        lines.append(
            f"  turn={report['turn_id']} conversation={report['conversation_ref']} "
            f"inbound={report['inbound_message_id']} terminal={shape} "
            f"reply_intents={report['reply_intents']} accepted_sends={report['accepted_sends']} "
            f"receipts={','.join(report['receipt_kinds']) or 'none'}")
    for name in CLAIMS:
        entry = found.get(name) or {}
        turns = entry.get("turns") or ()
        named = f" turns={list(turns)}" if turns else ""
        lines.append(f"  claim {name}={entry.get('verdict')} ({entry.get('why')}){named}")
    return lines


# ── Reading the database ─────────────────────────────────────────────────────

def configured_tenants(environ: Optional[dict] = None) -> List[int]:
    """The pilot's own allowlist. Nothing outside it is ever read."""
    from core.commerce_runtime import pilot_guard as pg

    env = environ if environ is not None else os.environ
    raw = str(env.get(pg.ENV_TENANT_ALLOWLIST, "") or "").strip()
    tenants: List[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError:
            continue
        if value > 0 and value not in tenants:
            tenants.append(value)
    return sorted(tenants)


def session() -> Any:
    from database.session import SessionLocal  # noqa: PLC0415

    return SessionLocal()


def _rows(db: Any, statement: str, **params: Any) -> List[Dict[str, Any]]:
    import sqlalchemy as sa  # noqa: PLC0415

    return [dict(row._mapping) for row in db.execute(sa.text(statement), params)]


def read_window(db: Any, *, tenant_id: int, since: _dt.datetime,
                until: _dt.datetime) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    """Every admitted turn for one tenant in one window, with its own rows.

    Returns ``(turns, effects_reserved, deferred)``. The turn list is capped at
    :data:`MAX_TURNS_INSPECTED`; reaching the cap raises rather than returning a
    partial window that would read as a whole one.
    """
    turns = _rows(db, """
        SELECT t.id AS turn_id, t.provider_message_id, t.admitted_at,
               c.conversation_ref
          FROM commerce_runtime_turns t
          JOIN commerce_runtime_conversations c ON c.id = t.conversation_id
         WHERE t.tenant_id = :tenant AND t.admitted_at >= :since AND t.admitted_at < :until
         ORDER BY t.admitted_at, t.id
         LIMIT :limit
    """, tenant=tenant_id, since=since, until=until, limit=MAX_TURNS_INSPECTED + 1)
    if len(turns) > MAX_TURNS_INSPECTED:
        raise ValueError("window_exceeds_trial_size")

    ids = [t["turn_id"] for t in turns]
    terminals: Dict[Any, Dict[str, Any]] = {}
    sequences: Dict[Any, List[Dict[str, Any]]] = {}
    if ids:
        for row in _rows(db, """
            SELECT turn_id, processing_outcome, transport_outcome, customer_reach
              FROM commerce_runtime_turn_terminals
             WHERE tenant_id = :tenant AND turn_id = ANY(:ids)
        """, tenant=tenant_id, ids=ids):
            terminals[row["turn_id"]] = row

        receipts: Dict[Any, List[Dict[str, Any]]] = {}
        for row in _rows(db, """
            SELECT r.attempt_id, r.kind, r.provider_message_id
              FROM commerce_runtime_delivery_receipts r
              JOIN commerce_runtime_delivery_attempts a ON a.id = r.attempt_id
              JOIN commerce_runtime_delivery_sequences s ON s.id = a.sequence_id
             WHERE s.tenant_id = :tenant AND s.turn_id = ANY(:ids)
             ORDER BY r.receipt_no
        """, tenant=tenant_id, ids=ids):
            receipts.setdefault(row["attempt_id"], []).append(row)

        attempts: Dict[Any, List[Dict[str, Any]]] = {}
        for row in _rows(db, """
            SELECT a.id AS attempt_id, a.sequence_id
              FROM commerce_runtime_delivery_attempts a
              JOIN commerce_runtime_delivery_sequences s ON s.id = a.sequence_id
             WHERE s.tenant_id = :tenant AND s.turn_id = ANY(:ids)
             ORDER BY a.attempt_no
        """, tenant=tenant_id, ids=ids):
            row["receipts"] = receipts.get(row["attempt_id"], [])
            attempts.setdefault(row["sequence_id"], []).append(row)

        for row in _rows(db, """
            SELECT id AS sequence_id, turn_id, outcome
              FROM commerce_runtime_delivery_sequences
             WHERE tenant_id = :tenant AND turn_id = ANY(:ids)
             ORDER BY id
        """, tenant=tenant_id, ids=ids):
            row["attempts"] = attempts.get(row["sequence_id"], [])
            sequences.setdefault(row["turn_id"], []).append(row)

    for turn in turns:
        turn["terminal"] = terminals.get(turn["turn_id"])
        turn["sequences"] = sequences.get(turn["turn_id"], [])

    effects = _rows(db, """
        SELECT count(*) AS reserved FROM commerce_runtime_effects
         WHERE tenant_id = :tenant AND created_at >= :since AND created_at < :until
    """, tenant=tenant_id, since=since, until=until)
    deferred = _rows(db, """
        SELECT id, state, disposition, reason
          FROM commerce_runtime_deferred_inbound
         WHERE tenant_id = :tenant AND created_at >= :since AND created_at < :until
         ORDER BY id
    """, tenant=tenant_id, since=since, until=until)
    return turns, int(effects[0]["reserved"]) if effects else 0, deferred


def _read_only(db: Any) -> bool:
    """Make the transaction read-only, and say whether that was established."""
    import sqlalchemy as sa  # noqa: PLC0415

    try:
        db.execute(sa.text("SET TRANSACTION READ ONLY"))
        return True
    except Exception:  # noqa: BLE001 - a backend that cannot promise it says so
        return False


# ── Entry point ──────────────────────────────────────────────────────────────

def _moment(value: str, *, default: _dt.datetime) -> _dt.datetime:
    text = str(value or "").strip()
    if not text:
        return default
    parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def parse_args(argv: Optional[Sequence[str]] = None) -> Any:
    parser = argparse.ArgumentParser(prog="commerce_runtime_trial_evidence",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--since", default="",
                        help=f"ISO-8601 start of the window (default: {DEFAULT_WINDOW_HOURS}h ago)")
    parser.add_argument("--until", default="", help="ISO-8601 end of the window (default: now)")
    return parser.parse_args(list(argv if argv is not None else []))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        until = _moment(args.until, default=now)
        since = _moment(args.since, default=until - _dt.timedelta(hours=DEFAULT_WINDOW_HOURS))
    except ValueError:
        result(RESULT_FAILED_PRECONDITION, reason="window_unparsable")
        return EXIT_USAGE
    if since >= until:
        result(RESULT_FAILED_PRECONDITION, reason="window_is_empty")
        return EXIT_USAGE

    tenants = configured_tenants()
    if not tenants:
        result(RESULT_FAILED_PRECONDITION, reason="no_tenant_allowlist",
               hint="COMMERCE_RUNTIME_PILOT_TENANT_ALLOWLIST names the tenants to report on")
        return EXIT_USAGE

    emit(f"window={since.isoformat()}..{until.isoformat()} "
         f"tenants={','.join(str(t) for t in tenants)}")

    db = None
    refused: List[str] = []
    try:
        db = session()
        read_only = _read_only(db)
        emit(f"read_only_transaction={read_only}")
        if not read_only:
            raise RuntimeError("read_only_transaction_not_established")
        for tenant_id in tenants:
            turns, effects, deferred = read_window(db, tenant_id=tenant_id,
                                                   since=since, until=until)
            found = verdicts(turns, effects_reserved=effects, deferred=deferred)
            for line in render(tenant_id, [turn_report(t) for t in turns], found):
                emit(line)
            refused.extend(f"{tenant_id}:{name}" for name in refused_claims(found))
    except Exception as exc:  # noqa: BLE001 - a report we cannot read is never a clean one
        result(RESULT_FAILED, error=type(exc).__name__, detail=str(exc)[:120])
        return EXIT_FAILED
    finally:
        if db is not None:
            try:
                db.rollback()
                db.close()
            except Exception:  # noqa: BLE001 - a session we cannot close is dropped
                emit("session_close_failed=true")

    if refused:
        result(RESULT_REFUSED, contradicted=refused)
        return EXIT_REFUSED
    result(RESULT_REPORTED, tenants=len(tenants))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main(sys.argv[1:]))
