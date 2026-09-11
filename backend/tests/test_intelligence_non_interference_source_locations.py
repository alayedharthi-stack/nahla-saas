"""A source location is diagnostic information, not a new routing rule."""
from __future__ import annotations

import pytest

from test_intelligence_non_interference_guard import (
    GUARD, _classes, _commit, _init_repo, _seed_governance, _write,
)

PATH = "backend/modules/ai/brain/commerce/example.py"
SOURCE = '''import re
def resolve(message):
    if re.search("منتج", message):
        return helper(message, "اختيار")
    if message.startswith("طلب"):
        return True
    return False
'''


def _scan(tmp_path, before, after):
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    _write(repo, PATH, before)
    base = _commit(repo, "base")
    _write(repo, PATH, after)
    head = _commit(repo, "change")
    return GUARD.scan_repository(str(repo), base, head, trusted_base_scanner=True)


@pytest.mark.parametrize("prefix", ["\n\n", "# source comment\n", "CATALOG_VERSION = 2\n\n"])
def test_moving_unchanged_checks_does_not_create_semantic_findings(tmp_path, prefix):
    result = _scan(tmp_path, SOURCE, prefix + SOURCE)
    assert not result.findings


@pytest.mark.parametrize("after", [
    SOURCE.replace('re.search("منتج", message)', 're.search("منتجات", message)'),
    SOURCE.replace('re.search("منتج", message)', 're.search("منتج", other_message)'),
    SOURCE.replace('def resolve(message):', 'def other_route(message):'),
    SOURCE + '\ndef second(message):\n    return re.search("منتج", message)\n',
])
def test_changed_or_reused_regex_is_still_reported(tmp_path, after):
    result = _scan(tmp_path, SOURCE, "\n\n" + after)
    assert "CUSTOMER_REGEX_CHANGE" in _classes(result)


def test_moving_existing_check_into_new_ownership_condition_is_reported(tmp_path):
    before = 'def route(message):\n    if allowed:\n        return helper(message, "اختيار")\n'
    after = before.replace('if allowed:', 'if different_owner:')
    assert "KEYWORD_ROUTER_CHANGE" in _classes(_scan(tmp_path, before, after))


def test_duplicate_check_in_same_function_is_reported(tmp_path):
    after = SOURCE.replace('    return False', '    if message.startswith("طلب"):\n        return 2\n    return False')
    assert "KEYWORD_ROUTER_CHANGE" in _classes(_scan(tmp_path, SOURCE, after))


@pytest.mark.parametrize("before,after", [
    ('def f(message):\n    try:\n        return helper(message, "اختيار")\n    finally:\n        pass\n',
     'def f(message):\n    try:\n        pass\n    finally:\n        return helper(message, "اختيار")\n'),
    ('def f(message):\n    match owner:\n        case "first":\n            return helper(message, "اختيار")\n',
     'def f(message):\n    match owner:\n        case "second":\n            return helper(message, "اختيار")\n'),
])
def test_reusing_literal_across_control_flow_boundaries_is_reported(tmp_path, before, after):
    assert "KEYWORD_ROUTER_CHANGE" in _classes(_scan(tmp_path, before, after))
