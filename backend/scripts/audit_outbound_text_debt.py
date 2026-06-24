#!/usr/bin/env python3
"""
Audit hardcoded Arabic strings with customer-facing risk classification.

Usage (from repo root):
  python backend/scripts/audit_outbound_text_debt.py
  python backend/scripts/audit_outbound_text_debt.py --json

Reports classified buckets — not a single inflated raw count.
Does not fail CI by default.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from core.outbound_text_allowlist import (  # noqa: E402
    AUDIT_FILE_SUFFIXES,
    AUDIT_SCAN_ROOTS,
    extract_arabic_string_literals,
)
from core.outbound_text_audit_classification import (  # noqa: E402
    ALL_BUCKETS,
    AUDIT_EXCLUDED_PATH_PARTS,
    KB_DISCLAIMER_LINES,
    build_summary,
    classify_audit_finding,
    parse_current_function,
    should_skip_line,
)


def _should_scan_path(path: Path) -> bool:
    parts = {p.lower() for p in path.parts}
    return not any(ex in parts for ex in AUDIT_EXCLUDED_PATH_PARTS)


def scan_file(path: Path) -> tuple[list[dict], int]:
    findings: list[dict] = []
    raw_count = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings, raw_count
    rel = path.relative_to(BACKEND).as_posix()
    current_function: str | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        current_function = parse_current_function(line, current_function)
        if should_skip_line(line):
            continue
        for lit in extract_arabic_string_literals(line):
            raw_count += 1
            classification = classify_audit_finding(
                filepath=rel,
                line_content=line,
                literal=lit,
                current_function=current_function,
            )
            entry = {
                "file": rel,
                "line": lineno,
                "bucket": classification["bucket"],
                "base_kind": classification.get("base_kind"),
                "preview": lit[:100],
            }
            for key in ("kb_risk_path", "kb_delivery_mode", "runtime_note"):
                if key in classification:
                    entry[key] = classification[key]
            findings.append(entry)
    return findings, raw_count


def collect_findings() -> tuple[list[dict], int, list[str]]:
    all_findings: list[dict] = []
    raw_total = 0
    scanned_roots: list[str] = []
    for root_name in AUDIT_SCAN_ROOTS:
        root = BACKEND / root_name
        if not root.exists():
            continue
        scanned_roots.append(root_name)
        for path in root.rglob("*"):
            if path.suffix not in AUDIT_FILE_SUFFIXES:
                continue
            if not _should_scan_path(path):
                continue
            file_findings, raw_count = scan_file(path)
            raw_total += raw_count
            all_findings.extend(file_findings)
    return all_findings, raw_total, scanned_roots


def format_text_report(summary: dict, findings: list[dict]) -> str:
    lines: list[str] = [
        "Outbound text debt audit (risk-classified)",
        "",
        "=== Summary ===",
        f"  raw_arabic_string_count: {summary['raw_arabic_string_count']}",
        f"  total_findings (classified): {summary['total_findings']}",
        f"  production_code_count: {summary['production_code_count']}",
        f"  tests_count: {summary['tests_count']}",
        f"  actual_customer_facing_risk_count: {summary['actual_customer_facing_risk_count']}",
        f"  unique_customer_facing_risk_count: {summary['unique_customer_facing_risk_count']}",
        f"  regex_or_intent_count: {summary['regex_or_intent_count']}",
        f"  prompt_only_count: {summary['prompt_only_count']}",
        f"  internal_only_count: {summary['internal_only_count']}",
        f"  technical_allowlist_count: {summary['technical_allowlist_count']}",
        f"  meta_template_count: {summary['meta_template_count']}",
        f"  duplicates_count: {summary['duplicates_count']}",
        "",
        "  By bucket:",
    ]
    for bucket in ALL_BUCKETS:
        count = summary.get("by_bucket", {}).get(bucket, 0)
        if count:
            lines.append(f"    {bucket}: {count}")

    lines.extend([
        "",
        "=== Scan scope ===",
        f"  scanned_paths: {', '.join(summary['scanned_paths'])}",
        "  excluded_paths:",
    ])
    for item in summary["excluded_paths"]:
        lines.append(f"    - {item}")

    lines.extend(["", "=== KB scope (important) ==="])
    for item in KB_DISCLAIMER_LINES:
        lines.append(f"  * {item}")

    kb_paths = summary.get("kb_literal_reply_risk_paths") or []
    if kb_paths:
        lines.extend(["", "=== KB literal-reply risk paths (audit tags only) ==="])
        seen: set[str] = set()
        for item in kb_paths:
            key = item.get("kb_risk_path", "")
            if key in seen:
                continue
            seen.add(key)
            mode = item.get("kb_delivery_mode", "")
            lines.append(f"  - {key} ({mode})")

    lines.extend(["", "=== Top 10 customer-facing risk files ==="])
    for fp, count in summary.get("top_risk_files", []):
        lines.append(f"  {fp}: {count}")

    lines.extend(["", "=== Top 10 noise files (regex/prompt/internal) ==="])
    for fp, count in summary.get("top_noise_files", []):
        lines.append(f"  {fp}: {count}")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit outbound text debt with risk buckets")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    findings, raw_total, scanned_roots = collect_findings()
    summary = build_summary(
        findings,
        raw_arabic_string_count=raw_total,
        scanned_paths=scanned_roots,
    )

    report = {
        **summary,
        "findings": findings,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(summary, findings))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
