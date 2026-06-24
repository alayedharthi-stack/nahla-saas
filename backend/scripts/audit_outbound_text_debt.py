#!/usr/bin/env python3
"""
Audit hardcoded Arabic customer-facing strings in outbound paths.

Usage (from repo root):
  python backend/scripts/audit_outbound_text_debt.py
  python backend/scripts/audit_outbound_text_debt.py --json

Does not fail CI by default — reports debt inventory for Phase 1.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.outbound_text_allowlist import (  # noqa: E402
    AUDIT_FILE_SUFFIXES,
    AUDIT_SCAN_ROOTS,
    classify_string_literal,
    extract_arabic_string_literals,
    is_likely_internal_line,
)


def scan_file(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    rel = path.relative_to(BACKEND).as_posix()
    for lineno, line in enumerate(text.splitlines(), start=1):
        if is_likely_internal_line(line):
            continue
        for lit in extract_arabic_string_literals(line):
            kind = classify_string_literal(lit, filepath=rel)
            if kind in ("internal_only",):
                continue
            findings.append({
                "file": rel,
                "line": lineno,
                "kind": kind,
                "preview": lit[:100],
            })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit outbound text debt")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    all_findings: list[dict] = []
    for root_name in AUDIT_SCAN_ROOTS:
        root = BACKEND / root_name
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix not in AUDIT_FILE_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            all_findings.extend(scan_file(path))

    by_kind = Counter(f["kind"] for f in all_findings)
    by_file = defaultdict(int)
    for f in all_findings:
        if f["kind"] == "deterministic_customer_facing_debt":
            by_file[f["file"]] += 1

    report = {
        "total_findings": len(all_findings),
        "by_kind": dict(by_kind),
        "debt_files_top10": sorted(
            by_file.items(), key=lambda x: -x[1],
        )[:10],
        "findings": all_findings,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Outbound text debt audit")
        print(f"  Total findings: {report['total_findings']}")
        print("  By kind:")
        for kind, count in sorted(by_kind.items()):
            print(f"    {kind}: {count}")
        print("  Top debt files:")
        for fp, count in report["debt_files_top10"]:
            print(f"    {fp}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
