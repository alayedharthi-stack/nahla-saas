"""Acceptance tests for the shared JUnit cleanliness guard.

The previous CI snippet read counts from the ``<testsuites>`` root.
Pytest 9 puts them on the child ``<testsuite>``.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GUARD = _REPO_ROOT / "scripts" / "check_junit_clean.py"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_spec = importlib.util.spec_from_file_location("check_junit_clean", _GUARD)
assert _spec is not None and _spec.loader is not None
_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_guard)
evaluate_junit_report = _guard.evaluate_junit_report


def _write_xml(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def _pytest9_wrapper(*suites: str) -> str:
    inner = "\n".join(suites)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
{inner}
</testsuites>
"""


def _suite(*, name: str, tests: int, skipped: int = 0, failures: int = 0, errors: int = 0) -> str:
    cases = []
    remaining = tests
    for i in range(failures):
        cases.append(
            f'    <testcase classname="t.{name}" name="fail_{i}" time="0.01">'
            f'<failure message="boom">boom</failure></testcase>'
        )
        remaining -= 1
    for i in range(errors):
        cases.append(
            f'    <testcase classname="t.{name}" name="err_{i}" time="0.01">'
            f'<error message="err">err</error></testcase>'
        )
        remaining -= 1
    for i in range(skipped):
        cases.append(
            f'    <testcase classname="t.{name}" name="skip_{i}" time="0.00">'
            f'<skipped message="s">s</skipped></testcase>'
        )
        remaining -= 1
    for i in range(max(0, remaining)):
        cases.append(
            f'    <testcase classname="t.{name}" name="ok_{i}" time="0.01" />'
        )
    joined = "".join(f"\n{c}" for c in cases)
    return (
        f'  <testsuite name="{name}" errors="{errors}" failures="{failures}" '
        f'skipped="{skipped}" tests="{tests}" time="0.2">{joined}\n  </testsuite>'
    )


def _legacy_root_attrib_guard(xml_path: Path) -> None:
    """Exact counter logic previously inlined in the CI job."""
    if not xml_path.exists():
        raise SystemExit("missing WhatsApp catalog Postgres JUnit file")
    root = ET.parse(xml_path).getroot()
    skipped = int(root.attrib.get("skipped") or 0)
    failed = int(root.attrib.get("failures") or 0)
    errors = int(root.attrib.get("errors") or 0)
    tests = int(root.attrib.get("tests") or 0)
    if tests < 1:
        raise SystemExit("WhatsApp catalog Postgres suite ran zero tests")
    if skipped or failed or errors:
        raise SystemExit(
            f"wa catalog pg not clean: tests={tests} skipped={skipped} "
            f"failures={failed} errors={errors}"
        )


def test_legacy_root_guard_rejects_clean_pytest9_report(tmp_path: Path):
    path = _write_xml(
        tmp_path,
        "pytest9.xml",
        _pytest9_wrapper(_suite(name="pytest", tests=16)),
    )
    with pytest.raises(SystemExit, match="zero tests"):
        _legacy_root_attrib_guard(path)
    result = evaluate_junit_report(path)
    assert result["ok"] is True
    assert result["tests"] == 16


def test_guard_accepts_single_suite_success(tmp_path: Path):
    path = _write_xml(tmp_path, "single.xml", _suite(name="pytest", tests=4).lstrip())
    result = evaluate_junit_report(path)
    assert result["ok"] is True
    assert result["tests"] == 4
    assert result["skipped"] == 0
    assert result["failures"] == 0
    assert result["errors"] == 0


def test_guard_accepts_multiple_suites_and_sums_leaf_counts(tmp_path: Path):
    path = _write_xml(
        tmp_path,
        "multi.xml",
        _pytest9_wrapper(
            _suite(name="a", tests=3),
            _suite(name="b", tests=5),
        ),
    )
    result = evaluate_junit_report(path)
    assert result["ok"] is True
    assert result["tests"] == 8
    assert result["suites"] == 2


def test_guard_rejects_skipped(tmp_path: Path):
    path = _write_xml(
        tmp_path,
        "skip.xml",
        _pytest9_wrapper(_suite(name="pytest", tests=4, skipped=1)),
    )
    result = evaluate_junit_report(path)
    assert result["ok"] is False
    assert result["skipped"] == 1
    assert result["tests"] == 4


def test_guard_rejects_failure_or_error(tmp_path: Path):
    failed = evaluate_junit_report(
        _write_xml(
            tmp_path,
            "fail.xml",
            _pytest9_wrapper(_suite(name="failing", tests=2, failures=1)),
        )
    )
    assert failed["ok"] is False
    assert failed["failures"] == 1
    errored = evaluate_junit_report(
        _write_xml(
            tmp_path,
            "error.xml",
            _pytest9_wrapper(_suite(name="erring", tests=2, errors=1)),
        )
    )
    assert errored["ok"] is False
    assert errored["errors"] == 1


def test_guard_rejects_zero_tests(tmp_path: Path):
    path = _write_xml(
        tmp_path,
        "zero.xml",
        _pytest9_wrapper(_suite(name="pytest", tests=0)),
    )
    result = evaluate_junit_report(path)
    assert result["ok"] is False
    assert result["reason"] == "zero_tests"


def test_guard_rejects_missing_or_corrupt_xml(tmp_path: Path):
    missing = evaluate_junit_report(tmp_path / "nope.xml")
    assert missing["ok"] is False
    assert missing["reason"] == "missing"
    corrupt = tmp_path / "bad.xml"
    corrupt.write_text("<not-junit", encoding="utf-8")
    invalid = evaluate_junit_report(corrupt)
    assert invalid["ok"] is False
    assert invalid["reason"] == "invalid_xml"


def test_guard_does_not_double_count_nested_parent_attributes(tmp_path: Path):
    body = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests" tests="99" skipped="0" failures="0" errors="0">
  <testsuite name="outer" tests="99" skipped="0" failures="0" errors="0">
    <testsuite name="inner" tests="2" skipped="0" failures="0" errors="0">
      <testcase classname="t.inner" name="ok_0" time="0.01" />
      <testcase classname="t.inner" name="ok_1" time="0.01" />
    </testsuite>
  </testsuite>
</testsuites>
"""
    result = evaluate_junit_report(_write_xml(tmp_path, "nested.xml", body))
    assert result["ok"] is True
    assert result["tests"] == 2
    assert result["suites"] == 1


def test_guard_cli_matches_evaluate_and_exits_nonzero_on_skip(tmp_path: Path):
    clean = _write_xml(tmp_path, "clean.xml", _pytest9_wrapper(_suite(name="pytest", tests=2)))
    skipped = _write_xml(
        tmp_path,
        "skipped.xml",
        _pytest9_wrapper(_suite(name="pytest", tests=2, skipped=1)),
    )
    clean_run = subprocess.run(
        [sys.executable, str(_GUARD), str(clean)],
        capture_output=True,
        text=True,
        check=False,
    )
    skip_run = subprocess.run(
        [sys.executable, str(_GUARD), str(skipped)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean_run.returncode == 0
    assert skip_run.returncode != 0
    assert "tests=2" in clean_run.stdout
    assert "skipped=1" in skip_run.stdout


def test_ci_job_invokes_shared_junit_guard():
    ci_text = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job_start = ci_text.index("whatsapp-catalog-sync-postgres:")
    window = ci_text[job_start:job_start + 5000]
    assert "scripts/check_junit_clean.py" in window
    assert "test_whatsapp_catalog_sync_postgres_locks.py" in window
    pytest_chunk, guard_chunk = window.split("python -m pytest", 1)[1].split(
        "python scripts/check_junit_clean.py", 1
    )
    assert "--junitxml=" in pytest_chunk
    assert guard_chunk.strip()
