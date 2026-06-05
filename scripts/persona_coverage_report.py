#!/usr/bin/env python3
"""
persona_coverage_report.py
──────────────────────────
Compute Persona Coverage / Bypass metrics from production logs.

Usage (Railway):
  railway logs --environment production --since 7d > logs_7d.txt
  python scripts/persona_coverage_report.py logs_7d.txt

Parses ``[TURN]`` lines (primary) and ``[BrainTurn]`` JSON (secondary).

Metrics emitted:
  - persona_coverage_percent
  - persona_bypass_percent
  - persona_bypass_by_reason
  - persona_telemetry_gap_percent
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from typing import Any, Dict, Iterable, List, Optional


_TURN_RE = re.compile(r"\[TURN\].*")
_BRAIN_TURN_RE = re.compile(r"\[BrainTurn\]\s*(\{.*\})\s*$")


def _parse_turn_line(line: str) -> Optional[Dict[str, Any]]:
    if "[TURN]" not in line:
        return None
    out: Dict[str, Any] = {"source": "TURN"}
    for key in (
        "outbound_sent",
        "persona_stamped",
        "bypass_reason",
        "expression_owner",
        "persona_topic",
        "reply_source",
    ):
        m = re.search(rf"{key}=(\S+)", line)
        if not m:
            continue
        val = m.group(1)
        if key == "outbound_sent":
            out[key] = val.lower() == "true"
        elif key == "persona_stamped":
            if val == "true":
                out[key] = True
            elif val == "false":
                out[key] = False
            else:
                out[key] = None
        elif val != "-":
            out[key] = val
    return out


def _parse_brain_turn_line(line: str) -> Optional[Dict[str, Any]]:
    m = _BRAIN_TURN_RE.search(line.strip())
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    data["source"] = "BrainTurn"
    return data


def iter_records(lines: Iterable[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for line in lines:
        rec = _parse_turn_line(line)
        if rec:
            records.append(rec)
            continue
        rec = _parse_brain_turn_line(line)
        if rec:
            records.append(rec)
    return records


def compute_turn_metrics(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    outbound = [t for t in turns if t.get("outbound_sent") is True]
    total = len(outbound)
    if total == 0:
        return {
            "total_outbound_turns": 0,
            "persona_coverage_percent": None,
            "persona_bypass_percent": None,
            "persona_telemetry_gap_percent": None,
            "persona_bypass_by_reason": {},
            "note": "No outbound [TURN] rows with outbound_sent=true",
        }

    stamped = [t for t in outbound if t.get("persona_stamped") is True]
    bypass = [t for t in outbound if t.get("persona_stamped") is False]
    gap = [t for t in outbound if t.get("persona_stamped") is None]

    by_reason: Dict[str, int] = collections.Counter(
        str(t.get("bypass_reason") or "UNLABELED")
        for t in bypass + gap
    )

    coverage = round(100.0 * len(stamped) / total, 2)
    bypass_pct = round(100.0 * (len(bypass) + len(gap)) / total, 2)
    gap_pct = round(100.0 * len(gap) / total, 2)

    return {
        "total_outbound_turns": total,
        "persona_stamped_count": len(stamped),
        "persona_bypass_count": len(bypass),
        "persona_telemetry_gap_count": len(gap),
        "persona_coverage_percent": coverage,
        "persona_bypass_percent": bypass_pct,
        "persona_telemetry_gap_percent": gap_pct,
        "persona_bypass_by_reason": dict(by_reason.most_common()),
        "top_bypass_sources": [
            {"reason": r, "count": c, "percent": round(100.0 * c / total, 2)}
            for r, c in by_reason.most_common(10)
        ],
    }


def compute_brain_turn_metrics(brain_turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(brain_turns)
    if total == 0:
        return {"brain_turn_count": 0}

    stamped = sum(1 for b in brain_turns if b.get("persona_stamped") is True)
    by_action = collections.Counter(str(b.get("action") or "?") for b in brain_turns)
    by_mode = collections.Counter(str(b.get("response_mode") or "?") for b in brain_turns)
    by_bypass = collections.Counter(
        str(b.get("bypass_reason") or ("PERSONA" if b.get("persona_stamped") else "UNSET"))
        for b in brain_turns
    )

    return {
        "brain_turn_count": total,
        "brain_persona_stamped_count": stamped,
        "brain_persona_coverage_percent": round(100.0 * stamped / total, 2),
        "brain_action_breakdown": dict(by_action.most_common(12)),
        "brain_response_mode_breakdown": dict(by_mode.most_common()),
        "brain_bypass_by_reason": dict(by_bypass.most_common()),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Persona coverage report from logs")
    parser.add_argument("logfile", nargs="?", help="Log file path (stdin if omitted)")
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding (use utf-16 for some Windows exports)",
    )
    args = parser.parse_args(argv)

    if args.logfile:
        with open(args.logfile, encoding=args.encoding, errors="replace") as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    records = iter_records(lines)
    turns = [r for r in records if r.get("source") == "TURN"]
    brain_turns = [r for r in records if r.get("source") == "BrainTurn"]

    report = {
        "turn_metrics": compute_turn_metrics(turns),
        "brain_turn_metrics": compute_brain_turn_metrics(brain_turns),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
