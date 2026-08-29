"""Shared JUnit cleanliness guard for CI and local tests.

Counts must come from leaf ``<testsuite>`` elements. Pytest 9 writes
``tests`` / ``skipped`` / ``failures`` / ``errors`` on the child suite,
not on the ``<testsuites>`` wrapper.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List


def _local_tag(elem: ET.Element) -> str:
    return (elem.tag or "").split("}")[-1]


def _int_attr(elem: ET.Element, name: str) -> int:
    raw = elem.attrib.get(name)
    if raw is None or str(raw).strip() == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unreadable JUnit attribute {name}={raw!r}") from exc


def _child_suites(elem: ET.Element) -> List[ET.Element]:
    return [child for child in list(elem) if _local_tag(child) == "testsuite"]


def _iter_leaf_suites(elem: ET.Element) -> Iterable[ET.Element]:
    """Yield suites that own counts, never a parent that also wraps suites."""
    children = _child_suites(elem)
    if children:
        for child in children:
            yield from _iter_leaf_suites(child)
        return
    if _local_tag(elem) == "testsuite":
        yield elem


def evaluate_junit_report(path: str | Path) -> Dict[str, Any]:
    xml_path = Path(path)
    empty = {
        "ok": False,
        "reason": "missing",
        "tests": 0,
        "skipped": 0,
        "failures": 0,
        "errors": 0,
        "path": str(xml_path),
    }
    if not xml_path.exists() or not xml_path.is_file():
        return empty
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return {**empty, "reason": "invalid_xml"}
    except OSError:
        return {**empty, "reason": "unreadable"}

    tag = _local_tag(root)
    if tag not in {"testsuite", "testsuites"}:
        return {**empty, "reason": "unrecognized_root"}

    try:
        suites = list(_iter_leaf_suites(root))
        tests = sum(_int_attr(suite, "tests") for suite in suites)
        skipped = sum(_int_attr(suite, "skipped") for suite in suites)
        failures = sum(_int_attr(suite, "failures") for suite in suites)
        errors = sum(_int_attr(suite, "errors") for suite in suites)
    except ValueError:
        return {**empty, "reason": "unreadable_counts"}

    summary = {
        "ok": False,
        "reason": "not_clean",
        "tests": tests,
        "skipped": skipped,
        "failures": failures,
        "errors": errors,
        "path": str(xml_path),
        "suites": len(suites),
    }
    if not suites or tests < 1:
        summary["reason"] = "zero_tests"
        return summary
    if skipped or failures or errors:
        return summary
    summary["ok"] = True
    summary["reason"] = "clean"
    return summary


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject JUnit reports with skips or failures")
    parser.add_argument("report", type=Path, help="Path to pytest --junitxml output")
    args = parser.parse_args(argv)
    result = evaluate_junit_report(args.report)
    print(
        "junit guard: "
        f"ok={result['ok']} reason={result['reason']} "
        f"tests={result['tests']} skipped={result['skipped']} "
        f"failures={result['failures']} errors={result['errors']}"
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
