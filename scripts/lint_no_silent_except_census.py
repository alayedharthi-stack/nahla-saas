#!/usr/bin/env python
"""
scripts/lint_no_silent_except_census.py
───────────────────────────────────────
P0 Silent Except Baseline Census (observation only).

Reads the live tree and the committed baseline WITHOUT modifying either.
Use before a baseline resync to understand technical debt distribution.

Usage:
  python scripts/lint_no_silent_except_census.py
  python scripts/lint_no_silent_except_census.py --write docs/runbooks/P0_SILENT_EXCEPT_BASELINE_CENSUS.md

Exit codes:
  0 — report emitted
  2 — invocation error
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

# Reuse the canonical scanner — no duplicated AST rules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lint_no_silent_except import (  # noqa: E402
    BASELINE_PATH,
    REPO_ROOT,
    SCAN_DIRS,
    Violation,
    _baseline_key,
    _iter_py_files,
    _load_baseline_counts,
    scan_file,
)

_MSG_SHORT = {
    "silent pass on broad except": "silent pass",
    "silent return on broad except": "silent return",
    "logger.debug-only on broad except (use logger.exception)": "logger.debug-only",
}


def _collect_hits() -> List[Violation]:
    hits: List[Violation] = []
    for py in _iter_py_files(SCAN_DIRS):
        hits.extend(scan_file(py))
    return hits


def _rel_path(path: str) -> str:
    return str(Path(path).resolve().relative_to(REPO_ROOT)).replace("\\", "/")


def _build_report(hits: List[Violation], baseline: Dict[str, int]) -> str:
    key_counts = Counter(_baseline_key(p, ln, m) for p, ln, m in hits)
    type_counts = Counter(_MSG_SHORT.get(m, m) for _, _, m in hits)

    file_counts = Counter()
    for p, _, m in hits:
        file_counts[_rel_path(p)] += 1

    unbaselined_instances = 0
    excess_by_key: Dict[str, int] = {}
    for key, found in key_counts.items():
        allowed = baseline.get(key, 0)
        if found > allowed:
            excess = found - allowed
            unbaselined_instances += excess
            excess_by_key[key] = excess

    baselined_instances = len(hits) - unbaselined_instances
    baseline_entries = sum(baseline.values())
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: List[str] = [
        "# P0 Silent Except Baseline Census",
        "",
        f"> Generated: `{generated_at}`",
        f"> Command: `python scripts/lint_no_silent_except_census.py`",
        f"> Baseline file: `{BASELINE_PATH.relative_to(REPO_ROOT)}` (read-only)",
        "",
        "Observation-only. Does **not** modify baseline or production code.",
        "",
        "## Executive summary",
        "",
        "| Metric | Count |",
        "|--------|------:|",
        f"| Live violations (instances) | {len(hits)} |",
        f"| Unique keys (`path::message`) | {len(key_counts)} |",
        f"| Baseline entries (committed) | {baseline_entries} |",
        f"| Covered by baseline | {baselined_instances} |",
        f"| **Unbaselined (CI reports as \"NEW\")** | **{unbaselined_instances}** |",
        f"| Baseline gap | {len(hits)} - {baseline_entries} = **{len(hits) - baseline_entries}** |",
        "",
        "## By violation type",
        "",
        "| Type | Instances |",
        "|------|----------:|",
    ]
    for label in ("silent pass", "silent return", "logger.debug-only"):
        lines.append(f"| {label} | {type_counts.get(label, 0)} |")
    other = sum(v for k, v in type_counts.items() if k not in _MSG_SHORT.values())
    if other:
        lines.append(f"| other | {other} |")

    lines.extend([
        "",
        "## Top 20 files by violation count",
        "",
        "| Rank | File | Instances |",
        "|-----:|------|----------:|",
    ])
    for rank, (path, count) in enumerate(file_counts.most_common(20), start=1):
        lines.append(f"| {rank} | `{path}` | {count} |")

    lines.extend([
        "",
        "## Top 20 unbaselined keys (excess over baseline)",
        "",
        "These drive CI failure today - legacy debt not captured in the frozen baseline.",
        "",
        "| Rank | Excess | Found | Baseline | Key |",
        "|-----:|-------:|------:|---------:|-----|",
    ])
    for rank, (key, excess) in enumerate(
        sorted(excess_by_key.items(), key=lambda x: (-x[1], x[0]))[:20],
        start=1,
    ):
        found = key_counts[key]
        allowed = baseline.get(key, 0)
        lines.append(f"| {rank} | +{excess} | {found} | {allowed} | `{key}` |")

    lines.extend([
        "",
        "## Interpretation",
        "",
        "- The lint gate is **designed** to allow pre-existing violations via baseline.",
        "- CI fails when `unbaselined > 0`, not when a PR introduces delta vs `main`.",
        f"- A resync from `{baseline_entries}` to `{len(hits)}` entries documents debt;",
        "  it does not fix violations in code.",
        "",
        "## Next steps (platform, not PR-scoped)",
        "",
        "1. Review this census.",
        "2. `P0 Silent Except Baseline Resync` with this report attached.",
        "3. `P0 Silent Except Gate PR Delta Mode` so PRs are not hostage to baseline drift.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    write_path: Path | None = None
    args = sys.argv[1:]
    if "--write" in args:
        idx = args.index("--write")
        if idx + 1 >= len(args):
            print("census: --write requires a path", file=sys.stderr)
            return 2
        write_path = REPO_ROOT / args[idx + 1]

    hits = _collect_hits()
    baseline = _load_baseline_counts()
    report = _build_report(hits, baseline)

    if write_path is not None:
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(report, encoding="utf-8")
        print(f"census: wrote {write_path.relative_to(REPO_ROOT)}")
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass
    print(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
