#!/usr/bin/env python
"""Commerce Conversation Reliability Gate — runner.

Runs the required PR #1084 regression modules in their native pytest
invocation (unchanged), then the reliability harness for the requested tier,
and judges the JUnit results against the reviewed manifest with
``tests/commerce_reliability/reliability_evaluator.py``.

    python scripts/commerce_reliability_gate.py --tier unit
    NAHLA_RELIABILITY_REQUIRE_PG=1 NAHLA_RELIABILITY_PG_ADMIN_DSN=postgresql://... \\
        python scripts/commerce_reliability_gate.py --tier postgres

Exit status is 0 only when the gate passes. A passing gate proves harness
integrity and baseline compatibility; it is NOT production acceptance.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.commerce_reliability import reliability_evaluator as ev  # noqa: E402

MANIFEST_PATH = REPO_ROOT / "tests" / "commerce_reliability" / "reliability_manifest.json"
MANIFEST_ENV = "NAHLA_RELIABILITY_MANIFEST"
TODAY_ENV = "NAHLA_RELIABILITY_TODAY"

HARNESS_DIR = "tests/commerce_reliability"
HARNESS_MODULES = {
    ev.TIER_UNIT: [
        f"{HARNESS_DIR}/test_evaluator_selfcheck.py",
        f"{HARNESS_DIR}/test_runtime_v1_delivery.py",
        f"{HARNESS_DIR}/test_runtime_v1_browse.py",
    ],
    ev.TIER_POSTGRES: [
        f"{HARNESS_DIR}/test_runtime_state_postgres.py",
    ],
}


def _git_head() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _run_pytest(label: str, targets: List[str], junit_path: Path, env: Dict[str, str]) -> int:
    cmd = [
        sys.executable, "-m", "pytest", *targets,
        "-v", "--tb=short", "-rxXs", "-p", "no:cacheprovider",
        "-o", "junit_family=xunit1", f"--junitxml={junit_path}",
    ]
    print(f"\n=== {label}\n$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env)
    print(f"=== {label}: pytest exit code {proc.returncode}", flush=True)
    return int(proc.returncode)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tier", choices=list(ev.TIERS), required=True)
    parser.add_argument("--junit-dir", type=Path, default=Path("/tmp/commerce-reliability"))
    parser.add_argument("--report", type=Path, default=None, help="write the JSON gate report here")
    args = parser.parse_args(argv)

    manifest = ev.load_manifest(MANIFEST_PATH)
    module_hashes = {
        m["path"]: ev.sha256_of(REPO_ROOT / m["path"])
        for m in manifest["required_modules"] if (REPO_ROOT / m["path"]).is_file()
    }
    args.junit_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env[MANIFEST_ENV] = str(MANIFEST_PATH)   # the plugin judges against the reviewed manifest only
    env.pop(TODAY_ENV, None)                 # expiry is judged against the real date
    env.pop("PYTEST_ADDOPTS", None)

    header: Dict[str, Any] = {
        "tier": args.tier,
        "manifest": str(MANIFEST_PATH.relative_to(REPO_ROOT)),
        "manifest_sha256": ev.sha256_of(MANIFEST_PATH),
        "manifest_base_sha": manifest["base_sha"],
        "git_head": _git_head(),
        "started_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
    }
    runner_blockers: List[str] = []
    invocations: List[Dict[str, Any]] = []
    records: List[ev.TestRecord] = []

    pg = ev.resolve_postgres_config(os.environ)
    if args.tier == ev.TIER_POSTGRES and (pg["error"] or not pg["required"]):
        # Fail fast: the PostgreSQL tier never runs against an implicit database.
        runner_blockers.append(pg["error"] or f"postgres_tier_requires_{ev.PG_REQUIRE_ENV}=1")
    else:
        plan: List[Dict[str, Any]] = []
        if args.tier == ev.TIER_UNIT:
            native = [m["path"] for m in manifest["required_modules"] if m.get("tier", ev.TIER_UNIT) == ev.TIER_UNIT]
            plan.append({"label": "PR #1084 regression modules (native invocation, unchanged)",
                         "targets": native, "junit": args.junit_dir / "pr1084-regressions.xml"})
        plan.append({"label": f"reliability harness - {args.tier} tier",
                     "targets": HARNESS_MODULES[args.tier], "junit": args.junit_dir / f"harness-{args.tier}.xml"})
        for step in plan:
            junit_path: Path = step["junit"]
            if junit_path.exists():
                junit_path.unlink()
            code = _run_pytest(step["label"], step["targets"], junit_path, env)
            invocations.append({"label": step["label"], "targets": step["targets"],
                                "junit": str(junit_path), "pytest_exit_code": code})
            if code not in (0, 1):
                # 0 = all passed, 1 = some failed (judged from JUnit); anything else is a broken run.
                runner_blockers.append(f"pytest_exit_code:{code}:{step['label']}")
            if not junit_path.exists():
                runner_blockers.append(f"junit_missing:{step['label']}")
                continue
            records.extend(ev.parse_junit(junit_path))

    verdict = ev.evaluate_gate(
        manifest, tier=args.tier, records=records, env=os.environ, module_hashes=module_hashes,
    )
    verdict.blockers = runner_blockers + verdict.blockers
    verdict.passed = not verdict.blockers
    header["invocations"] = invocations
    header["finished_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    print("\n" + ev.render_gate_report(verdict, header={
        k: v for k, v in header.items() if k in ("tier", "git_head", "manifest_base_sha", "manifest_sha256")
    }), flush=True)

    report = {
        "header": header,
        "passed": verdict.passed,
        "blockers": verdict.blockers,
        "warnings": verdict.warnings,
        "sections": verdict.sections,
        "records": [
            {"nodeid": r.nodeid, "outcome": r.outcome, "message": r.message[:200]} for r in records
        ],
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"gate report written: {args.report}", flush=True)
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
