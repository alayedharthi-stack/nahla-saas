#!/usr/bin/env python3
"""Read-only Trusted Context shadow telemetry audit from local log input."""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.abspath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from modules.ai.brain.truth_surface.prod_telemetry_audit import (
    DEFAULT_MIN_SAMPLES_FOR_PASS,
    audit_shadow_telemetry,
)


def _read_lines(*, file_path: str | None) -> list[str]:
    if file_path:
        with open(file_path, encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    if sys.stdin.isatty():
        return []
    return sys.stdin.read().splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit [TRUSTED_CONTEXT_SHADOW] log lines from a file or stdin.",
    )
    parser.add_argument(
        "--file",
        "-f",
        dest="file_path",
        help="Local log file path. Omit to read from stdin.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES_FOR_PASS,
        help=(
            "Minimum success events required for PASS "
            f"(default: {DEFAULT_MIN_SAMPLES_FOR_PASS}; lower only for synthetic/local testing)."
        ),
    )
    parser.add_argument(
        "--require-pass",
        action="store_true",
        help="Exit non-zero unless telemetry_log_safety_verdict is PASS.",
    )
    args = parser.parse_args(argv)
    if args.min_samples < 1:
        parser.error("--min-samples must be an integer >= 1")

    lines = _read_lines(file_path=args.file_path)
    report = audit_shadow_telemetry(lines, min_samples_for_pass=args.min_samples)
    out = {
        "source": args.file_path or ("stdin" if lines else "empty"),
        "line_count": len(lines),
        "required_min_samples": report.required_min_samples,
        "acceptance_gaps": list(report.acceptance_gaps),
        "audit": report.to_dict(),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    verdict = report.telemetry_log_safety_verdict.value
    if verdict == "FAIL":
        return 1
    if args.require_pass and verdict != "PASS":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
