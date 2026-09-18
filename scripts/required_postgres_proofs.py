#!/usr/bin/env python3
"""Strict runner for required PostgreSQL proof suites.

Runs every suite listed in ``scripts/required_postgres_proofs.json`` as its
own pytest process against a real PostgreSQL and judges the JUnit output
against the suite's node-id inventory. The verdict is PROVEN only when, for
every suite, the required configuration was present before pytest started,
the collected node ids equal the inventory exactly, every test passed, and
the JUnit counts show zero skipped, zero failures and zero errors. A skip is
never a pass; there is no diagnostic or advisory mode. Suites carry a
``kind``: ``proof`` (the PostgreSQL proofs themselves) or
``runner_regression`` (tests of this runner and of the shared connection
helper); the verdict counts them separately and requires both.

Report freshness: any previous report at ``--report`` and any previous
JUnit files in ``--junit-dir`` are removed before validation starts, and
every early failure (unreadable manifest, missing configuration, missing
module) writes a fresh NOT RUN report, so a stale PROVEN report can never
appear current.

This runner is independent of the commerce reliability gate
(``scripts/commerce_reliability_gate.py``): it carries no allowances, and a
PROVEN verdict here says nothing about that gate's acceptance, which remains
blocked by its own unapproved baseline debt.

Exit codes
==========
0  PROVEN — every suite proved.
1  NOT PROVEN — missing or uninventoried collection, a skip, a failure, an
   error, a missing module or a non-zero pytest exit in at least one suite.
2  NOT RUN — manifest unreadable or required environment missing or wrong;
   pytest is not started for any suite.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EXIT_PROVEN = 0
EXIT_NOT_PROVEN = 1
EXIT_NOT_RUN = 2

KIND_PROOF = "proof"
KIND_RUNNER_REGRESSION = "runner_regression"
KINDS = (KIND_PROOF, KIND_RUNNER_REGRESSION)

OUTCOME_PASSED = "passed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"
OUTCOME_ERROR = "error"


class ManifestError(ValueError):
    """The manifest is unreadable or inconsistent."""


# ── Manifest ─────────────────────────────────────────────────────────────────


def load_manifest(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"manifest unreadable: {path}: {exc}") from None
    suites = data.get("suites")
    if not isinstance(suites, list) or not suites:
        raise ManifestError("manifest has no suites")
    seen_ids: set = set()
    for suite in suites:
        if not isinstance(suite, dict):
            raise ManifestError("suite entry is not an object")
        suite_id = suite.get("id")
        module = suite.get("module")
        nodeids = suite.get("nodeids")
        env = suite.get("required_env", {})
        if not isinstance(suite_id, str) or not suite_id or suite_id in seen_ids:
            raise ManifestError(f"suite id missing or duplicated: {suite_id!r}")
        seen_ids.add(suite_id)
        if not isinstance(module, str) or not module.endswith(".py") or module.startswith("/"):
            raise ManifestError(f"{suite_id}: module must be a repository-relative .py path")
        if not isinstance(nodeids, list) or not nodeids or any(not isinstance(n, str) for n in nodeids):
            raise ManifestError(f"{suite_id}: nodeids must be a non-empty list of strings")
        if len(set(nodeids)) != len(nodeids):
            raise ManifestError(f"{suite_id}: duplicate nodeids")
        for nodeid in nodeids:
            if not nodeid.startswith(module + "::"):
                raise ManifestError(f"{suite_id}: nodeid does not belong to module: {nodeid}")
        if not isinstance(env, dict) or any(not isinstance(k, str) for k in env):
            raise ManifestError(f"{suite_id}: required_env must be an object of variable names")
        for key, value in env.items():
            if value is not None and not isinstance(value, str):
                raise ManifestError(f"{suite_id}: required_env[{key}] must be a string or null")
        if suite.get("kind", KIND_PROOF) not in KINDS:
            raise ManifestError(f"{suite_id}: kind must be one of {KINDS}")
    return data


def environment_blockers(suite: Dict[str, Any], environ: Dict[str, str]) -> List[str]:
    """Configuration that must be present before pytest starts (fail closed)."""
    blockers: List[str] = []
    for key, expected in (suite.get("required_env") or {}).items():
        actual = environ.get(key)
        if actual is None or actual.strip() == "":
            blockers.append(f"missing_env:{key}")
        elif expected is not None and actual != expected:
            blockers.append(f"env_value_mismatch:{key}:expected={expected}")
    return blockers


# ── JUnit ────────────────────────────────────────────────────────────────────


def _local_tag(elem: ET.Element) -> str:
    return (elem.tag or "").split("}")[-1]


def _leaf_suites(elem: ET.Element) -> List[ET.Element]:
    children = [c for c in list(elem) if _local_tag(c) == "testsuite"]
    if children:
        out: List[ET.Element] = []
        for child in children:
            out.extend(_leaf_suites(child))
        return out
    return [elem] if _local_tag(elem) == "testsuite" else []


def junit_nodeid(module: str, classname: str, name: str) -> Optional[str]:
    """Rebuild a pytest node id from JUnit classname + name for ``module``.

    Returns None when the testcase does not belong to the module.
    """
    dotted = module[:-3].replace("/", ".")
    if classname == dotted:
        return f"{module}::{name}"
    if classname.startswith(dotted + "."):
        inner = classname[len(dotted) + 1:].replace(".", "::")
        return f"{module}::{inner}::{name}"
    return None


def read_junit(path: Path, module: str) -> Dict[str, Any]:
    """Outcomes per node id plus the leaf-suite counts."""
    if not path.is_file():
        return {"error": "junit_missing", "outcomes": {}, "foreign": [], "counts": None}
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return {"error": "junit_unreadable", "outcomes": {}, "foreign": [], "counts": None}
    outcomes: Dict[str, Tuple[str, str]] = {}
    foreign: List[str] = []
    for case in root.iter():
        if _local_tag(case) != "testcase":
            continue
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        nodeid = junit_nodeid(module, classname, name)
        if nodeid is None:
            foreign.append(f"{classname}::{name}")
            continue
        outcome, detail = OUTCOME_PASSED, ""
        for child in list(case):
            tag = _local_tag(child)
            if tag == "skipped":
                outcome, detail = OUTCOME_SKIPPED, (child.get("message") or "")
            elif tag == "failure":
                outcome, detail = OUTCOME_FAILED, (child.get("message") or "")
            elif tag == "error":
                outcome, detail = OUTCOME_ERROR, (child.get("message") or "")
        outcomes[nodeid] = (outcome, detail[:300])
    counts = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for suite in _leaf_suites(root):
        for key in counts:
            try:
                counts[key] += int(suite.get(key) or 0)
            except ValueError:
                return {"error": "junit_counts_unreadable", "outcomes": outcomes, "foreign": foreign, "counts": None}
    return {"error": None, "outcomes": outcomes, "foreign": foreign, "counts": counts}


# ── Judgement ────────────────────────────────────────────────────────────────


def judge_suite(suite: Dict[str, Any], junit: Dict[str, Any], pytest_exit: int) -> Dict[str, Any]:
    required = list(suite["nodeids"])
    blockers: List[str] = []
    if junit["error"]:
        blockers.append(junit["error"])
    outcomes: Dict[str, Tuple[str, str]] = junit["outcomes"]
    for nodeid in required:
        if nodeid not in outcomes:
            blockers.append(f"missing_collection:{nodeid}")
    for nodeid in outcomes:
        if nodeid not in required:
            blockers.append(f"uninventoried_collection:{nodeid}")
    for nodeid in junit["foreign"]:
        blockers.append(f"foreign_collection:{nodeid}")
    for nodeid, (outcome, detail) in outcomes.items():
        if outcome != OUTCOME_PASSED:
            blockers.append(f"{outcome}:{nodeid}:{detail}" if detail else f"{outcome}:{nodeid}")
    counts = junit["counts"]
    if counts is not None:
        if counts["tests"] != len(required):
            blockers.append(f"count_mismatch:tests={counts['tests']}:required={len(required)}")
        for key in ("skipped", "failures", "errors"):
            if counts[key]:
                blockers.append(f"count_nonzero:{key}={counts[key]}")
    if pytest_exit != 0:
        blockers.append(f"pytest_exit_code:{pytest_exit}")
    passed = sum(1 for o, _ in outcomes.values() if o == OUTCOME_PASSED)
    return {
        "id": suite["id"],
        "kind": suite.get("kind", KIND_PROOF),
        "module": suite["module"],
        "required": len(required),
        "collected": len(outcomes),
        "passed": passed,
        "skipped": sum(1 for o, _ in outcomes.values() if o == OUTCOME_SKIPPED),
        "failed": sum(1 for o, _ in outcomes.values() if o == OUTCOME_FAILED),
        "errors": sum(1 for o, _ in outcomes.values() if o == OUTCOME_ERROR),
        "pytest_exit_code": pytest_exit,
        "blockers": blockers,
        "proven": not blockers,
    }


# ── Execution ────────────────────────────────────────────────────────────────


def run_suite(root: Path, suite: Dict[str, Any], junit_dir: Path) -> Tuple[int, Path]:
    junit_path = junit_dir / f"{suite['id']}.xml"
    if junit_path.exists():
        junit_path.unlink()
    cmd = [
        sys.executable, "-m", "pytest", suite["module"], "-q", "-p", "no:cacheprovider",
        f"--junitxml={junit_path}",
    ]
    print(f"=== suite {suite['id']}: {' '.join(cmd[1:])}", flush=True)
    proc = subprocess.run(cmd, cwd=str(root))
    print(f"=== suite {suite['id']}: pytest exit code {proc.returncode}", flush=True)
    return proc.returncode, junit_path


def _write_report(report: Optional[Path], verdict: Dict[str, Any]) -> None:
    if report is None:
        return
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(verdict, indent=2, ensure_ascii=False), encoding="utf-8")


def _reset_outputs(report: Optional[Path], junit_dir: Path) -> None:
    """Invalidate every earlier output before validation starts.

    A previous PROVEN report or JUnit file must never survive into a run
    that stops early; the report starts as NOT RUN and is only replaced by
    this run's own verdict.
    """
    if report is not None and report.exists():
        report.unlink()
    _write_report(report, {"verdict": "NOT RUN", "reason": "validation_not_started",
                           "note": "placeholder written before validation; replaced by this run's verdict"})
    if junit_dir.exists():
        for stale in junit_dir.glob("*.xml"):
            stale.unlink()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--junit-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None, help="write the JSON verdict here")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root (default: the parent of scripts/)")
    parser.add_argument("--list", action="store_true", help="print the inventory and exit")
    args = parser.parse_args(argv)

    if not args.list:
        _reset_outputs(args.report, args.junit_dir)

    try:
        manifest = load_manifest(args.manifest)
    except ManifestError as exc:
        _write_report(args.report, {"verdict": "NOT RUN", "reason": "manifest", "detail": str(exc)})
        print(f"REQUIRED POSTGRES PROOFS: NOT RUN — {exc}", flush=True)
        return EXIT_NOT_RUN

    if args.list:
        for suite in manifest["suites"]:
            print(f"[{suite['id']}] kind={suite.get('kind', KIND_PROOF)} {suite['module']} "
                  f"({len(suite['nodeids'])} required)")
            for nodeid in suite["nodeids"]:
                print(f"  {nodeid}")
        return EXIT_PROVEN

    root = args.root.resolve()
    config_blockers: Dict[str, List[str]] = {}
    for suite in manifest["suites"]:
        blockers = environment_blockers(suite, dict(os.environ))
        if not (root / suite["module"]).is_file():
            blockers.append(f"module_missing:{suite['module']}")
        if blockers:
            config_blockers[suite["id"]] = blockers
    if config_blockers:
        for suite_id, blockers in config_blockers.items():
            for blocker in blockers:
                print(f"  {suite_id}: {blocker}", flush=True)
        _write_report(args.report, {"verdict": "NOT RUN", "reason": "configuration", "suites": config_blockers})
        print("REQUIRED POSTGRES PROOFS: NOT RUN — required configuration missing; pytest was not started",
              flush=True)
        return EXIT_NOT_RUN

    args.junit_dir.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, Any]] = []
    for suite in manifest["suites"]:
        pytest_exit, junit_path = run_suite(root, suite, args.junit_dir)
        result = judge_suite(suite, read_junit(junit_path, suite["module"]), pytest_exit)
        result["junit"] = str(junit_path)
        results.append(result)

    proven = all(r["proven"] for r in results)
    total_required = sum(r["required"] for r in results)
    total_passed = sum(r["passed"] for r in results)
    by_kind = {kind: {"required": 0, "passed": 0} for kind in KINDS}
    for r in results:
        by_kind[r["kind"]]["required"] += r["required"]
        by_kind[r["kind"]]["passed"] += r["passed"]
    print("REQUIRED POSTGRES PROOFS — per suite", flush=True)
    for r in results:
        print(f"  {r['id']} [{r['kind']}]: required={r['required']} collected={r['collected']} passed={r['passed']} "
              f"skipped={r['skipped']} failed={r['failed']} errors={r['errors']} "
              f"pytest_exit={r['pytest_exit_code']} verdict={'PROVEN' if r['proven'] else 'NOT PROVEN'}",
              flush=True)
        for blocker in r["blockers"]:
            print(f"    - {blocker}", flush=True)
    verdict = {
        "verdict": "PROVEN" if proven else "NOT PROVEN",
        "required": total_required,
        "passed": total_passed,
        "by_kind": by_kind,
        "suites": results,
        "note": ("Proof of the inventoried suites only; the commerce reliability gate's acceptance is a "
                 "separate result and is not measured or implied here."),
    }
    _write_report(args.report, verdict)
    proofs, regressions = by_kind[KIND_PROOF], by_kind[KIND_RUNNER_REGRESSION]
    print(f"REQUIRED POSTGRES PROOFS: {verdict['verdict']} ({total_passed}/{total_required} required tests passed: "
          f"{proofs['passed']}/{proofs['required']} proofs + {regressions['passed']}/{regressions['required']} "
          f"runner/fixture regressions, 0 skips tolerated)", flush=True)
    return EXIT_PROVEN if proven else EXIT_NOT_PROVEN


if __name__ == "__main__":
    sys.exit(main())
