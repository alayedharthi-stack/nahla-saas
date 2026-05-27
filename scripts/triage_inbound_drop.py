#!/usr/bin/env python3
"""scripts/triage_inbound_drop.py
─────────────────────────────────
Wave 2.2-INV (May 2026) — Read-only triage for "the message arrived in
WhatsApp but never appeared in Nahla / the AI didn't reply".

Input
-----

A masked sender (e.g. ``*0706``), a wamid (``wamid.HBgM…``), and one or
more log files / directories. The tool classifies the inbound against
the W2.2-INV scenario matrix (A–O) and prints the most likely cause
plus the exact log lines that proved it.

The classification is **purely log-based**. No DB hit, no network,
no behavioural side-effect. Stdlib only — runs anywhere Python 3.9+
is installed.

Usage
-----

::

    python scripts/triage_inbound_drop.py \\
        --sender '*0706' --log /var/log/nahla/app.log

    python scripts/triage_inbound_drop.py \\
        --wamid 'wamid.HBgMOTY2NTU1NTA3MDYV...' --log app.log app.log.1

    # Pipe directly from a kubectl / fly / railway tail:
    railway logs --tail | python scripts/triage_inbound_drop.py \\
        --sender '*0706' --log -

Exit code is 0 unless one of A/B/C/F/N is the verdict (true silent
loss → 2) or unrecoverable input (1).

This tool is the operational counterpart to the W2.2-INV
investigation. It does not modify any code, configuration, or data.

Scenario Matrix
---------------

  A  WhatsApp delivered but our instance never received     [no RAW]
  B  body parse fail / body={}                              [no RAW]
  C  spawn_background rejected                              [no RAW; BG event]
  D1 missing_phone_id                                       [RAW; gap]
  D2 unknown_phone_id (re-pair drift)                       [RAW; gap]
  D3 ambiguous_phone_id                                     [RAW; gap]
  D4 wrong_provider / bad_secret                            [RAW; gap]
  D5 scope_mismatch / field_not_messages                    [RAW; gap]
  E1 dedup_drop_memory (provider retry of same wamid)       [LIFECYCLE end_dropped]
  E2 dedup_drop_db (mark-before-persist trap)               [LIFECYCLE end_dropped]
  F  db_session_fail                                        [LIFECYCLE end_dropped]
  G  unsupported_type (sticker / reaction / …)              [LIFECYCLE end_dropped, persist_only]
  H  unsub_short_circuit                                    [LIFECYCLE end_dropped]
  I  order-flow short-circuit (payment/receipt/map/evidence) [LIFECYCLE end_ok, deterministic ack]
  J  pre-brain handoff guard fires                           [LIFECYCLE end_ok? inbound NOT saved]
  K  historical-skip (timestamp before whatsapp_ai_live_since) [LIFECYCLE end_ok, no Brain]
  L  payment-asset early bypass                              [LIFECYCLE end_ok, asset sent]
  M  AI pause / handoff active                               [LIFECYCLE end_ok, no Brain]
  N  Brain crash mid-pipeline                                [LIFECYCLE end_uncaught]
  O  happy path                                              [LIFECYCLE end_ok + brain_invoked]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ── Scenario metadata ───────────────────────────────────────────────


@dataclass
class Scenario:
    code: str
    title_ar: str
    title_en: str
    severity: str  # "silent_loss" | "visible_no_reply" | "happy"
    next_action_ar: str


SCENARIOS: Dict[str, Scenario] = {
    "A":  Scenario("A",  "WhatsApp سلّمها لكن لم تصلنا",
                   "WhatsApp delivered, our instance never received",
                   "silent_loss",
                   "افحص لوحة 360dialog (delivery state) + شبكة الـ instance."),
    "B":  Scenario("B",  "JSON parse fail (body={})",
                   "body parse failed",
                   "silent_loss",
                   "افحص [webhook/360dialog/...] body parse failed وأعد إنتاج الـ payload."),
    "C":  Scenario("C",  "spawn_background رُفض (queue full)",
                   "spawn_background rejected",
                   "silent_loss",
                   "زِد MAX_BACKGROUND_TASKS أو راقب moments من الضغط (W2.0.6 candidate)."),
    "D1": Scenario("D1", "missing_phone_id في الـ payload",
                   "missing_phone_id",
                   "silent_loss",
                   "metadata.phone_number_id مفقود — تواصل مع 360dialog."),
    "D2": Scenario("D2", "unknown_phone_id (تاجر جدّد الـ pairing)",
                   "no WhatsAppConnection row for this phone_number_id",
                   "silent_loss",
                   "شغّل admin/coexistence/sync-record للتاجر؛ phone_number_id الجديد غير مسجَّل."),
    "D3": Scenario("D3", "ambiguous_phone_id",
                   "phone_number_id matches multiple tenants",
                   "silent_loss",
                   "حالة integrity حرجة — راجع phone_number_id duplicates في WhatsAppConnection."),
    "D4": Scenario("D4", "wrong_provider / bad_secret",
                   "wrong provider or bad coexistence secret",
                   "silent_loss",
                   "تحقق من إعدادات provider/secret في إعدادات الـ tenant."),
    "D5": Scenario("D5", "scope_mismatch / field_not_messages",
                   "URL scope or field misclassification",
                   "silent_loss",
                   "التاجر سجّل URL خاطئ في 360dialog — يجب أن يكون messages على /webhook/whatsapp/360dialog أو /webhook/whatsapp/360dialog/coexistence حسب الحاجة."),
    "E1": Scenario("E1", "dedup_drop_memory (provider retry)",
                   "in-memory dedup hit — provider retried same wamid",
                   "visible_no_reply",
                   "السلوك صحيح؛ الرسالة الأصلية مُعالَجة. لو لم تظهر، انظر لـ E2."),
    "E2": Scenario("E2", "dedup_drop_db (mark-before-persist trap)",
                   "DB dedup hit — wamid was marked before any save_message landed",
                   "silent_loss",
                   "هذه W2.0.4 — يجب نقل IdempotencyGuard.mark_processed إلى ما بعد الحفظ."),
    "F":  Scenario("F",  "db_session_fail",
                   "no DB session available",
                   "silent_loss",
                   "DB pool مُستنفد — راجع SQLAlchemy pool settings + connection leaks."),
    "G":  Scenario("G",  "unsupported_type (sticker/reaction/…)",
                   "type not in brain allow-list",
                   "visible_no_reply",
                   "المحادثة ظاهرة بـ placeholder، لكن الـ Brain لم يُستدعَ. مرشَّح لـ W2.2 sticker bridge المُجمَّد."),
    "H":  Scenario("H",  "unsub_short_circuit",
                   "unsubscribe gate active",
                   "visible_no_reply",
                   "السلوك صحيح. لو الـ tag خاطئ، راجع unsubscribe state."),
    "I":  Scenario("I",  "order-flow short-circuit",
                   "payment/receipt/map/evidence ack",
                   "visible_no_reply",
                   "deterministic ack أُرسل (بعد W2.0.3 conversation_id يُمرَّر دائماً). السلوك صحيح."),
    "J":  Scenario("J",  "pre-brain handoff guard",
                   "handoff guard fired — inbound text NOT saved as MessageEvent",
                   "silent_loss",  # inbound text vanishes!
                   "هذا drop class جديد — outbound ack موجود لكن inbound text لم يُحفَظ. مرشَّح لـ W2.0.5."),
    "K":  Scenario("K",  "historical-skip (timestamp قديم)",
                   "wa_message_ts before whatsapp_ai_live_since",
                   "visible_no_reply",
                   "السلوك صحيح؛ الرسالة محفوظة كـ historical_sync. الـ Brain متوقف عن قصد."),
    "L":  Scenario("L",  "payment-asset early bypass",
                   "AI Library payment asset served deterministically",
                   "visible_no_reply",
                   "السلوك صحيح؛ التاجر صرّح بالـ asset."),
    "M":  Scenario("M",  "AI paused / handoff active",
                   "should_skip_ai true (manual pause or active handoff)",
                   "visible_no_reply",
                   "السلوك صحيح؛ موظف يديره يدوياً. ابحث عن لوحة الـ AI Pause."),
    "N":  Scenario("N",  "Brain crash mid-pipeline",
                   "uncaught exception inside dispatch",
                   "silent_loss",
                   "حادثة استثنائية — انظر traceback في نفس الـ trace_id."),
    "O":  Scenario("O",  "happy path",
                   "brain_invoked + end_ok",
                   "happy",
                   "لا شيء — الرسالة عُولِجت بشكل صحيح."),
}


# ── Log markers (regexes) ───────────────────────────────────────────


_RE_RAW = re.compile(
    r"\[D360_RAW_INBOUND\]"
)
_RE_BG_REJECTED = re.compile(
    r"\[INBOUND_LIFECYCLE\]\s+standalone\s+event=bg_rejected"
)
_RE_BODY_PARSE_FAIL = re.compile(
    r"\[webhook/360dialog/[^\]]+\]\s+body\s+parse\s+failed"
)
_RE_SPAWN_FAIL = re.compile(
    r"\[webhook/360dialog/[^\]]+\]\s+spawn_background\s+failed"
)
_RE_DISPATCH_GAP = re.compile(
    r"\[D360_DISPATCH_GAP\]\s+reason=(?P<reason>\S+)"
)
_RE_BRANCH = re.compile(
    r"\[D360_BRANCH\]\s+branch=(?P<branch>\S+)"
)
_RE_LIFECYCLE = re.compile(
    r"\[INBOUND_LIFECYCLE\]\s+trace_id=(?P<trace_id>\S+).*?"
    r"final=(?P<final>\S+).*?path=(?P<path>\S+)",
    re.DOTALL,
)
_RE_HANDOFF = re.compile(
    r"\[Merchant/HANDOFF_GUARD\]\s+PRE-BRAIN"
)
_RE_HIST_SKIP = re.compile(
    r"\[HISTORICAL_MESSAGE_SKIP_AI\]"
)
_RE_PAYMENT_BYPASS = re.compile(
    r"\[PAYMENT_INFO\]\s+early-bypass\s+APPLIED"
)
_RE_BRAIN_INVOKED = re.compile(
    r"event=brain_invoked|EVENT_BRAIN_INVOKED|->brain_invoked"
)


_DISPATCH_GAP_TO_SCENARIO = {
    "missing_phone_id":     "D1",
    "unknown_phone_id":     "D2",
    "ambiguous_phone_id":   "D3",
    "wrong_provider":       "D4",
    "bad_secret":           "D4",
    "scope_mismatch":       "D5",
    "field_not_messages":   "D5",
    "field_ignored":        "D5",
}


# ── Triage state ────────────────────────────────────────────────────


@dataclass
class TriageEvidence:
    raw_lines: List[str] = field(default_factory=list)
    bg_rejected_lines: List[str] = field(default_factory=list)
    body_parse_lines: List[str] = field(default_factory=list)
    spawn_fail_lines: List[str] = field(default_factory=list)
    dispatch_gap_lines: List[str] = field(default_factory=list)
    dispatch_gap_reasons: List[str] = field(default_factory=list)
    branch_lines: List[str] = field(default_factory=list)
    lifecycle_lines: List[str] = field(default_factory=list)
    lifecycle_finals: List[str] = field(default_factory=list)
    lifecycle_paths: List[str] = field(default_factory=list)
    handoff_lines: List[str] = field(default_factory=list)
    hist_skip_lines: List[str] = field(default_factory=list)
    payment_bypass_lines: List[str] = field(default_factory=list)
    brain_invoked_lines: List[str] = field(default_factory=list)
    other_matches: List[str] = field(default_factory=list)


def _key_match(line: str, *, sender: Optional[str], wamid: Optional[str]) -> bool:
    """Decide if a log line is about our target.

    Accepts a hit on any of:

    * The exact masked sender (``*XXXX``) — used by the W2.0.1.5 D360
      probe lines and the [INBOUND_LIFECYCLE] summary.
    * The 4-digit tail alone — used by per-tenant logs that print the
      unmasked phone number (``to=9665550706``, ``from=...0706``,
      ``HANDOFF_GUARD`` snippet copies, ``HISTORICAL_MESSAGE_SKIP_AI``).
      Risk of false positives is bounded by the marker regexes that
      gate each bucket.
    * The exact wamid OR its 32-char tail — covers the trace_id
      ``il_<provider>_<wamid>`` form built by ``make_trace_id``.
    """
    if sender and sender in line:
        return True
    if sender:
        tail4 = sender.lstrip("*")
        if tail4 and tail4 in line:
            return True
    if wamid and wamid in line:
        return True
    if wamid:
        tail = wamid[-32:]
        if tail and tail in line:
            return True
    return False


def _read_lines(paths: Sequence[str]) -> Iterable[str]:
    """Yield lines from the given paths. ``-`` reads from stdin.
    Directories are walked recursively for ``*.log`` and plain text
    files. Never raises on a single file read failure."""
    for raw in paths:
        if raw == "-":
            for line in sys.stdin:
                yield line.rstrip("\n")
            continue
        p = Path(raw)
        if p.is_dir():
            for sub in sorted(p.rglob("*")):
                if sub.is_file():
                    try:
                        with sub.open("r", encoding="utf-8", errors="replace") as fh:
                            for line in fh:
                                yield line.rstrip("\n")
                    except Exception as exc:
                        print(
                            f"[triage] WARNING: failed to read {sub}: {exc}",
                            file=sys.stderr,
                        )
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    yield line.rstrip("\n")
        except FileNotFoundError:
            print(f"[triage] WARNING: not found: {raw}", file=sys.stderr)
        except Exception as exc:
            print(
                f"[triage] WARNING: failed to read {raw}: {exc}",
                file=sys.stderr,
            )


def collect_evidence(
    *,
    lines: Iterable[str],
    sender: Optional[str],
    wamid: Optional[str],
) -> TriageEvidence:
    """Single pass over the log stream — bucket every relevant line
    into the evidence struct. Pure function on the iterable."""
    ev = TriageEvidence()
    for line in lines:
        if not _key_match(line, sender=sender, wamid=wamid):
            # Cheap pre-filter: still capture the BG_REJECTED standalone
            # event because it does NOT carry a sender (it fires before
            # we have one). Operators correlate by timestamp.
            if _RE_BG_REJECTED.search(line):
                ev.bg_rejected_lines.append(line)
            continue
        if _RE_RAW.search(line):
            ev.raw_lines.append(line)
            continue
        if _RE_BODY_PARSE_FAIL.search(line):
            ev.body_parse_lines.append(line)
            continue
        if _RE_SPAWN_FAIL.search(line):
            ev.spawn_fail_lines.append(line)
            continue
        if _RE_BG_REJECTED.search(line):
            ev.bg_rejected_lines.append(line)
            continue
        m = _RE_DISPATCH_GAP.search(line)
        if m:
            ev.dispatch_gap_lines.append(line)
            ev.dispatch_gap_reasons.append(m.group("reason"))
            continue
        m = _RE_BRANCH.search(line)
        if m:
            ev.branch_lines.append(line)
            continue
        m = _RE_LIFECYCLE.search(line)
        if m:
            ev.lifecycle_lines.append(line)
            ev.lifecycle_finals.append(m.group("final"))
            ev.lifecycle_paths.append(m.group("path"))
            continue
        if _RE_HANDOFF.search(line):
            ev.handoff_lines.append(line)
            continue
        if _RE_HIST_SKIP.search(line):
            ev.hist_skip_lines.append(line)
            continue
        if _RE_PAYMENT_BYPASS.search(line):
            ev.payment_bypass_lines.append(line)
            continue
        if _RE_BRAIN_INVOKED.search(line):
            ev.brain_invoked_lines.append(line)
            continue
        # Anything else carrying the key — useful for "weird" diags.
        ev.other_matches.append(line)
    return ev


def classify(evidence: TriageEvidence) -> Tuple[str, List[str]]:
    """Return (scenario_code, reasoning_breadcrumbs).

    The scenario priority follows the W2.2-INV ranking — Region 0
    (no RAW) before Region 1 (RAW yes, LIFECYCLE no) before Region 3
    (LIFECYCLE yes). Within each region we pick the most specific
    marker available."""
    reasons: List[str] = []

    # ── Region 0 — no RAW ────────────────────────────────────────────
    if not evidence.raw_lines and not evidence.lifecycle_lines:
        if evidence.body_parse_lines:
            reasons.append("Found [webhook/.../body parse failed].")
            return ("B", reasons)
        if evidence.spawn_fail_lines:
            reasons.append("Found [webhook/.../spawn_background failed].")
            return ("C", reasons)
        if evidence.bg_rejected_lines:
            reasons.append(
                "Found standalone bg_rejected near this timeframe; "
                "the BG queue rejected the inbound before _handle_360dialog_body."
            )
            return ("C", reasons)
        reasons.append(
            "No [D360_RAW_INBOUND] AND no [INBOUND_LIFECYCLE] for the key — "
            "WhatsApp may have delivered to 360dialog but our instance "
            "never received it."
        )
        return ("A", reasons)

    # ── Region 1 — RAW yes, LIFECYCLE no ─────────────────────────────
    if evidence.raw_lines and not evidence.lifecycle_lines:
        if evidence.dispatch_gap_reasons:
            reason = evidence.dispatch_gap_reasons[0]
            scenario = _DISPATCH_GAP_TO_SCENARIO.get(reason)
            if scenario:
                reasons.append(
                    f"[D360_DISPATCH_GAP] reason={reason} — change was "
                    "accepted at the webhook but never reached _dispatch_message."
                )
                return (scenario, reasons)
            reasons.append(
                f"[D360_DISPATCH_GAP] reason={reason} — unknown reason "
                "code; defaulting to D5."
            )
            return ("D5", reasons)
        # No gap line, no lifecycle → likely silent loss between RAW
        # emission and lifecycle open. Surface as A-class.
        reasons.append(
            "RAW emitted but no DISPATCH_GAP and no LIFECYCLE — investigate "
            "exception logs near this trace_id."
        )
        return ("A", reasons)

    # ── Region 3 — LIFECYCLE yes ─────────────────────────────────────
    # Pick the LAST lifecycle line (most recent retry / final attempt).
    last_idx = len(evidence.lifecycle_lines) - 1
    final = evidence.lifecycle_finals[last_idx] if evidence.lifecycle_finals else ""
    path = evidence.lifecycle_paths[last_idx] if evidence.lifecycle_paths else ""
    reasons.append(f"LIFECYCLE final={final} path={path}")

    if final == "end_uncaught_exception":
        return ("N", reasons)

    if final == "end_dropped":
        if "dedup_drop_memory" in path:
            return ("E1", reasons)
        if "dedup_drop_db" in path:
            return ("E2", reasons)
        if "db_session_fail" in path:
            return ("F", reasons)
        if "unsupported_type" in path:
            return ("G", reasons)
        if "unsub_short_circuit" in path:
            return ("H", reasons)
        # Fall-through: end_dropped with no recognised path token.
        reasons.append(
            "end_dropped with unrecognised path tail — investigate the "
            "events list manually."
        )
        return ("N", reasons)

    # final == end_ok or unknown
    if evidence.handoff_lines:
        reasons.append(
            "[Merchant/HANDOFF_GUARD] PRE-BRAIN fired — inbound text was "
            "NOT saved as a MessageEvent (only outbound ack persisted)."
        )
        return ("J", reasons)
    if evidence.hist_skip_lines:
        return ("K", reasons)
    if evidence.payment_bypass_lines:
        return ("L", reasons)
    if "auto_link_ok" in path or any(
        tok in path for tok in (
            "payment_short_circuit",
            "receipt_short_circuit",
            "map_short_circuit",
        )
    ):
        return ("I", reasons)
    if evidence.brain_invoked_lines or "brain_invoked" in path:
        return ("O", reasons)
    # end_ok but no brain — likely AI pause.
    reasons.append(
        "end_ok without brain_invoked — most likely AI paused or handoff "
        "active. Confirm via the dashboard's AI Pause panel."
    )
    return ("M", reasons)


def render_report(
    *,
    scenario_code: str,
    reasons: List[str],
    evidence: TriageEvidence,
    sender: Optional[str],
    wamid: Optional[str],
) -> str:
    """Format the human-readable Arabic + English report."""
    sc = SCENARIOS[scenario_code]
    lines: List[str] = []
    lines.append("=" * 72)
    lines.append(f"  TRIAGE VERDICT — scenario {sc.code} ({sc.severity})")
    lines.append("=" * 72)
    if sender:
        lines.append(f"  sender (masked): {sender}")
    if wamid:
        lines.append(f"  wamid:           {wamid}")
    lines.append("")
    lines.append(f"  AR: {sc.title_ar}")
    lines.append(f"  EN: {sc.title_en}")
    lines.append("")
    lines.append("  Why this verdict:")
    for r in reasons:
        lines.append(f"    - {r}")
    lines.append("")
    lines.append(f"  Next action:")
    lines.append(f"    >> {sc.next_action_ar}")
    lines.append("")

    def _section(title: str, items: List[str], cap: int = 5) -> None:
        if not items:
            return
        lines.append(f"  -- {title} ({len(items)}) --")
        for it in items[:cap]:
            lines.append(f"    {it.strip()}")
        if len(items) > cap:
            lines.append(f"    ... (+{len(items) - cap} more)")
        lines.append("")

    _section("RAW",                  evidence.raw_lines)
    _section("DISPATCH_GAP",         evidence.dispatch_gap_lines)
    _section("BRANCH",               evidence.branch_lines)
    _section("LIFECYCLE summary",    evidence.lifecycle_lines)
    _section("HANDOFF guard",        evidence.handoff_lines)
    _section("Historical skip",      evidence.hist_skip_lines)
    _section("Payment bypass",       evidence.payment_bypass_lines)
    _section("Brain invoked",        evidence.brain_invoked_lines)
    _section("BG rejected",          evidence.bg_rejected_lines)
    _section("Body parse fail",      evidence.body_parse_lines)
    _section("Spawn BG fail",        evidence.spawn_fail_lines)

    return "\n".join(lines)


# ── CLI ─────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="triage_inbound_drop",
        description=(
            "Read-only triage for vanished WhatsApp inbounds. Classifies "
            "a message against the W2.2-INV scenario matrix (A–O) using "
            "log evidence only."
        ),
    )
    p.add_argument(
        "--sender",
        help="Masked sender, e.g. '*0706'. Falls back to last-4 of --phone.",
    )
    p.add_argument(
        "--phone",
        help="Full phone number — auto-converted to masked form.",
    )
    p.add_argument(
        "--wamid",
        help="Inbound message id (wamid.HBgM…).",
    )
    p.add_argument(
        "--log",
        nargs="+",
        required=True,
        help="One or more log files, directories, or '-' for stdin.",
    )
    return p


def _resolve_sender(args: argparse.Namespace) -> Optional[str]:
    if args.sender:
        s = args.sender.strip()
        if not s.startswith("*"):
            s = "*" + s[-4:]
        return s
    if args.phone:
        digits = re.sub(r"\D", "", args.phone)
        if digits:
            return "*" + digits[-4:]
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    sender = _resolve_sender(args)
    wamid = (args.wamid or "").strip() or None
    if not sender and not wamid:
        print(
            "[triage] need at least one of --sender / --phone / --wamid",
            file=sys.stderr,
        )
        return 1

    evidence = collect_evidence(
        lines=_read_lines(args.log),
        sender=sender,
        wamid=wamid,
    )
    scenario, reasons = classify(evidence)
    print(render_report(
        scenario_code=scenario,
        reasons=reasons,
        evidence=evidence,
        sender=sender,
        wamid=wamid,
    ))
    sc = SCENARIOS[scenario]
    if sc.severity == "silent_loss":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
