"""Merge-freeze GitHub status gate — unit tests and workflow contract.

The trusted workflow YAML is the runtime. These tests own the decision
spec and prove fail-closed behaviour without calling GitHub or merging.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

CONTEXT = "merge-freeze-gate"
EXACT_LABEL = "merge-freeze"
WORKFLOW_REL = ".github/workflows/merge-freeze-gate.yml"
REPO_ROOT = Path(__file__).resolve().parents[2]


def decide_status(open_freeze_issues: Optional[int]) -> str:
    """None or negative means API uncertainty → failure. Count>=1 → failure."""
    if open_freeze_issues is None or open_freeze_issues < 0:
        return "failure"
    return "failure" if open_freeze_issues > 0 else "success"


def is_open_freeze_issue(item: Any, label: str = EXACT_LABEL) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("pull_request"):
        return False
    if item.get("state") != "open":
        return False
    names = set()
    for lab in item.get("labels") or []:
        if isinstance(lab, dict):
            names.add(str(lab.get("name") or ""))
        else:
            names.add(str(lab))
    return label in names


def count_open_freeze_issues(items: Optional[Sequence[Any]]) -> Optional[int]:
    if items is None:
        return None
    return sum(1 for item in items if is_open_freeze_issue(item))


def collect_pr_head_shas(pulls: Optional[Sequence[Any]]) -> Optional[List[str]]:
    if pulls is None:
        return None
    shas: List[str] = []
    seen = set()
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        head = pull.get("head")
        sha = ""
        if isinstance(head, dict):
            sha = str(head.get("sha") or "").strip()
        if sha and sha not in seen:
            seen.add(sha)
            shas.append(sha)
    return shas


def status_write_plan(
    shas: Sequence[str],
    open_freeze_issues: Optional[int],
) -> Tuple[List[Tuple[str, str]], str]:
    """Pending/failure first on every head, then the final freeze result."""
    final_state = decide_status(open_freeze_issues)
    plan: List[Tuple[str, str]] = [(sha, "pending") for sha in shas]
    plan.extend((sha, final_state) for sha in shas)
    return plan, final_state


def _workflow_text() -> str:
    path = REPO_ROOT / WORKFLOW_REL
    return path.read_text(encoding="utf-8")


def _extract_python_script(workflow: str) -> str:
    start = workflow.find("python3 - <<'PY'\n")
    end = workflow.rfind("\n          PY\n")
    if start < 0 or end < 0 or end <= start:
        raise AssertionError("workflow python heredoc not found")
    body = workflow[start + len("python3 - <<'PY'\n") : end]
    lines = body.splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    pad = min(indents) if indents else 0
    return "\n".join(line[pad:] if len(line) >= pad else line for line in lines)


class TestFreezeDecision:
    def test_freeze_absent_is_success(self) -> None:
        assert decide_status(0) == "success"

    def test_freeze_active_is_failure(self) -> None:
        assert decide_status(1) == "failure"

    def test_multiple_freeze_issues_are_failure(self) -> None:
        assert decide_status(2) == "failure"
        assert decide_status(7) == "failure"

    def test_api_uncertainty_is_failure(self) -> None:
        assert decide_status(None) == "failure"
        assert decide_status(-1) == "failure"


class TestFreezeIssueSelection:
    def test_counts_only_open_issues_with_exact_label(self) -> None:
        items = [
            {
                "state": "open",
                "labels": [{"name": "merge-freeze"}],
                "title": "MERGE FREEZE — owner controlled",
            },
            {
                "state": "open",
                "labels": [{"name": "merge-freeze-maybe"}],
            },
            {
                "state": "closed",
                "labels": [{"name": "merge-freeze"}],
            },
        ]
        assert count_open_freeze_issues(items) == 1

    def test_labeled_pull_requests_are_not_freeze_issues(self) -> None:
        items = [
            {
                "state": "open",
                "labels": [{"name": "merge-freeze"}],
                "pull_request": {"url": "https://example.invalid/pr/1"},
            },
            {
                "state": "open",
                "labels": [{"name": "merge-freeze"}],
            },
        ]
        assert count_open_freeze_issues(items) == 1

    def test_multiple_open_freeze_issues(self) -> None:
        items = [
            {"state": "open", "labels": [{"name": "merge-freeze"}]},
            {"state": "open", "labels": ["merge-freeze"]},
        ]
        assert count_open_freeze_issues(items) == 2
        assert decide_status(count_open_freeze_issues(items)) == "failure"

    def test_api_error_does_not_count_as_absent(self) -> None:
        assert count_open_freeze_issues(None) is None
        assert decide_status(count_open_freeze_issues(None)) == "failure"


class TestAllOpenPrHeadsUpdated:
    def test_collects_unique_head_shas_in_order(self) -> None:
        pulls = [
            {"head": {"sha": "aaa111"}},
            {"head": {"sha": "bbb222"}},
            {"head": {"sha": "aaa111"}},
            {"head": {"sha": ""}},
            {"number": 9},
        ]
        assert collect_pr_head_shas(pulls) == ["aaa111", "bbb222"]

    def test_open_pr_query_failure_is_uncertainty(self) -> None:
        assert collect_pr_head_shas(None) is None

    def test_freeze_on_writes_pending_then_failure_for_every_head(self) -> None:
        plan, final_state = status_write_plan(["sha-a", "sha-b"], 1)
        assert final_state == "failure"
        assert plan == [
            ("sha-a", "pending"),
            ("sha-b", "pending"),
            ("sha-a", "failure"),
            ("sha-b", "failure"),
        ]

    def test_freeze_off_writes_pending_then_success_for_every_head(self) -> None:
        plan, final_state = status_write_plan(["sha-a", "sha-b"], 0)
        assert final_state == "success"
        assert plan == [
            ("sha-a", "pending"),
            ("sha-b", "pending"),
            ("sha-a", "success"),
            ("sha-b", "success"),
        ]

    def test_api_error_after_pending_stays_blocking(self) -> None:
        plan, final_state = status_write_plan(["sha-a"], None)
        assert final_state == "failure"
        assert plan[0] == ("sha-a", "pending")
        assert plan[-1] == ("sha-a", "failure")

    def test_publisher_records_pending_before_final_on_issue_toggle(self) -> None:
        writes: List[Tuple[str, str]] = []

        def publish(shas: Sequence[str], state: str) -> None:
            for sha in shas:
                writes.append((sha, state))

        heads = ["pr1head", "pr2head"]
        publish(heads, "pending")
        freeze_count = count_open_freeze_issues(
            [{"state": "open", "labels": [{"name": "merge-freeze"}]}]
        )
        publish(heads, decide_status(freeze_count))
        assert writes == [
            ("pr1head", "pending"),
            ("pr2head", "pending"),
            ("pr1head", "failure"),
            ("pr2head", "failure"),
        ]
        writes.clear()
        publish(heads, "pending")
        freeze_count = count_open_freeze_issues([])
        publish(heads, decide_status(freeze_count))
        assert writes == [
            ("pr1head", "pending"),
            ("pr2head", "pending"),
            ("pr1head", "success"),
            ("pr2head", "success"),
        ]


class TestWorkflowTrustContract:
    def test_workflow_exists(self) -> None:
        assert (REPO_ROOT / WORKFLOW_REL).is_file()

    def test_trusted_triggers_and_permissions(self) -> None:
        text = _workflow_text()
        assert "pull_request_target:" in text
        assert "workflow_dispatch:" in text
        assert "issues:" in text
        for event in ("opened", "reopened", "synchronize", "ready_for_review"):
            assert event in text
        for event in ("opened", "reopened", "closed", "labeled", "unlabeled"):
            assert event in text
        assert "contents: read" in text
        assert "issues: read" in text
        assert "pull-requests: read" in text
        assert "statuses: write" in text
        assert "\n  pull_request:\n" not in text

    def test_does_not_checkout_or_run_pr_code(self) -> None:
        text = _workflow_text()
        script = _extract_python_script(text)
        assert "actions/checkout" not in text
        assert "${{ secrets." not in text
        assert "persist-credentials" not in text
        assert "import subprocess" not in script
        assert "os.system" not in script
        try:
            import yaml  # type: ignore
        except ImportError:
            pytest.skip("PyYAML not installed")
        data = yaml.safe_load(text)
        for job in (data.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                assert "uses" not in step

    def test_fail_closed_and_context_name(self) -> None:
        text = _workflow_text()
        script = _extract_python_script(text)
        assert CONTEXT in text
        assert f'LABEL = "{EXACT_LABEL}"' in text
        assert "MERGE_FREEZE_GATE_FAIL_CLOSED" in text
        assert "fail_closed" in text
        assert "MERGE_AUTHORIZED" not in script
        assert "DEPLOY_AUTHORIZED" not in script
        assert "owner-approved" not in script

    def test_python_script_parses(self) -> None:
        script = _extract_python_script(_workflow_text())
        tree = ast.parse(script)
        names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        assert "decide_status" in names
        assert "count_open_freeze_issues" in names
        assert "collect_pr_head_shas" in names
        assert "fail_closed" in names

    def test_workflow_yaml_has_required_mapping_shape(self) -> None:
        text = _workflow_text()
        try:
            import yaml  # type: ignore
        except ImportError:
            pytest.skip("PyYAML not installed")
        data: Dict[str, Any] = yaml.safe_load(text)
        assert data["name"] == "Merge freeze gate"
        on = data.get("on") or data.get(True)
        assert isinstance(on, dict)
        assert "pull_request_target" in on
        assert "issues" in on
        assert "workflow_dispatch" in on
        perms = data["permissions"]
        assert perms["contents"] == "read"
        assert perms["issues"] == "read"
        assert perms["pull-requests"] == "read"
        assert perms["statuses"] == "write"
        job = data["jobs"]["publish-merge-freeze-gate"]
        assert job["name"] == "publish-merge-freeze-gate"
        steps = job["steps"]
        assert len(steps) == 1
        assert "uses" not in steps[0]
        env = steps[0]["env"]
        assert "secrets." not in str(env)
        assert env["GH_TOKEN"]
        assert "PR_HEAD_SHA" in env
