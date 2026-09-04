"""CODEOWNERS owner gate — global wildcard plus preserved governance rules.

These tests parse the repository CODEOWNERS file. They do not call GitHub,
change branch protection, or request reviews.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import List, Sequence, Tuple

OWNER = "@alayedharthi-stack"
CODEOWNERS_REL = ".github/CODEOWNERS"
REPO_ROOT = Path(__file__).resolve().parents[2]

EXISTING_GOVERNANCE_PATTERNS: Tuple[str, ...] = (
    "AGENTS.md",
    "backend/modules/ai/compose/constitutional_policy.py",
    "backend/modules/ai/compose/tracked_violations_baseline.json",
    "backend/modules/ai/governance/",
    "backend/tests/test_constitution_compliance.py",
    "backend/tests/test_intelligence_non_interference_guard.py",
    "scripts/lint_intelligence_non_interference.py",
    ".github/workflows/ci.yml",
    ".github/workflows/gov002-intelligence-non-interference.yml",
    "docs/engineering/ai-pr-constitution-checklist.md",
    "docs/engineering/intelligence-non-interference-policy.md",
    "docs/engineering/gov002-workflow-trust-root.md",
    "docs/engineering/merge-and-ci-policy.md",
)

WORKFLOW_POLICY_INTELLIGENCE_PATHS: Tuple[str, ...] = (
    ".github/workflows/ci.yml",
    ".github/workflows/gov002-intelligence-non-interference.yml",
    ".github/workflows/merge-freeze-gate.yml",
    "docs/engineering/intelligence-non-interference-policy.md",
    "docs/engineering/merge-and-ci-policy.md",
    "docs/engineering/gov002-workflow-trust-root.md",
    "docs/engineering/ai-pr-constitution-checklist.md",
    "AGENTS.md",
    "backend/modules/ai/compose/constitutional_policy.py",
    "backend/modules/ai/governance/intelligence_non_interference.py",
    "scripts/lint_intelligence_non_interference.py",
)


def _codeowners_text() -> str:
    path = REPO_ROOT / CODEOWNERS_REL
    return path.read_text(encoding="utf-8")


def parse_codeowners(text: str) -> List[Tuple[str, Tuple[str, ...]]]:
    rules: List[Tuple[str, Tuple[str, ...]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, owners = parts[0], tuple(parts[1:])
        rules.append((pattern, owners))
    return rules


def _matches(pattern: str, path: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/")
        return path == prefix or path.startswith(pattern)
    return path == pattern or fnmatch.fnmatch(path, pattern)


def owners_for(path: str, rules: Sequence[Tuple[str, Tuple[str, ...]]]) -> Tuple[str, ...]:
    matched: Tuple[str, ...] = ()
    for pattern, owners in rules:
        if _matches(pattern, path):
            matched = owners
    return matched


class TestGlobalWildcardOwner:
    def test_codeowners_file_exists(self) -> None:
        assert (REPO_ROOT / CODEOWNERS_REL).is_file()

    def test_global_wildcard_exists_with_sole_owner(self) -> None:
        rules = parse_codeowners(_codeowners_text())
        wildcards = [owners for pattern, owners in rules if pattern == "*"]
        assert len(wildcards) == 1
        assert wildcards[0] == (OWNER,)

    def test_wildcard_is_the_default_rule(self) -> None:
        rules = parse_codeowners(_codeowners_text())
        assert rules[0][0] == "*"
        assert rules[0][1] == (OWNER,)


class TestExistingGovernanceRulesPreserved:
    def test_existing_governance_patterns_remain(self) -> None:
        rules = parse_codeowners(_codeowners_text())
        patterns = [pattern for pattern, _owners in rules]
        for pattern in EXISTING_GOVERNANCE_PATTERNS:
            assert pattern in patterns

    def test_existing_governance_patterns_stay_owned_by_owner(self) -> None:
        rules = parse_codeowners(_codeowners_text())
        by_pattern = {pattern: owners for pattern, owners in rules if pattern != "*"}
        for pattern in EXISTING_GOVERNANCE_PATTERNS:
            assert by_pattern[pattern] == (OWNER,)


class TestWorkflowsPoliciesAndIntelligenceRemainUnderOwner:
    def test_last_match_owner_is_the_owner(self) -> None:
        rules = parse_codeowners(_codeowners_text())
        for path in WORKFLOW_POLICY_INTELLIGENCE_PATHS:
            assert owners_for(path, rules) == (OWNER,)

    def test_unlisted_file_falls_back_to_global_owner(self) -> None:
        rules = parse_codeowners(_codeowners_text())
        assert owners_for("README.md", rules) == (OWNER,)
        assert owners_for("backend/app.py", rules) == (OWNER,)
