"""GOV-002 — synthetic violation and pass fixtures for the intelligence guard."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent
_SCANNER = _REPO_ROOT / "scripts" / "lint_intelligence_non_interference.py"
_BACKEND = _HERE.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from modules.ai.governance.intelligence_non_interference import (  # noqa: E402
    CHANGE_CLASSES,
    ChangeClass,
)


def _load_scanner():
    spec = importlib.util.spec_from_file_location("nahla_gov002_guard", _SCANNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nahla_gov002_guard"] = mod
    spec.loader.exec_module(mod)
    return mod


GUARD = _load_scanner()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def _write(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "gov002@nahla.test")
    _git(repo, "config", "user.name", "gov002")
    _git(repo, "config", "commit.gpgsign", "false")
    try:
        _git(repo, "checkout", "-b", "main")
    except subprocess.CalledProcessError:
        pass
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


RULES_BASE = '''"""intent rules"""
import re
_ORDER_ID_RE = re.compile(r"^[A-Z]{2}\\\\d{6}$")
def match(message: str):
    return bool(_ORDER_ID_RE.search(message or ""))
'''

PROTECTED_BASE = '''
import pytest
pytestmark = pytest.mark.governance_contract

def test_count_not_product_usage():
    assert True
'''

EXCEPTIONS_EMPTY = '{"schema_version": 1, "exceptions": []}\n'


def _seed_governance(repo: Path) -> None:
    _write(repo, "scripts/lint_intelligence_non_interference.py", _SCANNER.read_text(encoding="utf-8"))
    _write(repo, "backend/modules/ai/governance/intelligence_exceptions.json", EXCEPTIONS_EMPTY)
    _write(repo, "backend/modules/ai/brain/intent/rules.py", RULES_BASE)
    _write(
        repo,
        "backend/tests/test_order_support_d1_natural_ownership.py",
        PROTECTED_BASE,
    )
    _write(repo, "backend/modules/ai/orchestrator/customer_chat_models.py", 'MODEL_LUNA = "gpt-5.6-luna"\n')
    _write(
        repo,
        "backend/modules/ai/prompts/builder.py",
        'SYSTEM = "You are a store assistant. Use the provided facts."\n',
    )
    _write(
        repo,
        "backend/modules/ai/brain/persona_expression.py",
        "def compose(text):\n    return text\n",
    )
    _write(
        repo,
        "backend/modules/ai/brain/compose/responder.py",
        "def reply(payload):\n    return payload\n",
    )
    _write(
        repo,
        "backend/modules/ai/brain/commerce/customer_order_evidence.py",
        "def collect():\n    return {\"order_count\": 0}\n",
    )


def _scan(repo: Path, base: str, head: str, *, bootstrap: bool = False):
    return GUARD.scan_repository(
        str(repo),
        base,
        head,
        bootstrap=bootstrap,
        trusted_base_scanner=not bootstrap,
    )


def _classes(result) -> set[str]:
    return {f.change_class for f in result.unauthorized}


def test_registry_classes_match_scanner() -> None:
    assert tuple(CHANGE_CLASSES) == GUARD.CHANGE_CLASSES
    assert {c.value for c in ChangeClass} <= set(CHANGE_CLASSES)


def test_a_new_semantic_customer_regex(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE + "\n_USAGE_RE = re.compile(r'كم مرة')\n",
    )
    head = _commit(repo, "add customer regex")
    result = _scan(repo, base, head)
    assert "CUSTOMER_REGEX_CHANGE" in _classes(result)


def test_b_modified_semantic_regex(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE + "\nimport re as _re\n_USAGE_RE = _re.compile(r'كيف استخدم')\n",
    )
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE + "\nimport re as _re\n_USAGE_RE = _re.compile(r'كيف استخدم|كم مرة')\n",
    )
    head = _commit(repo, "modify regex")
    result = _scan(repo, base, head)
    assert "CUSTOMER_REGEX_CHANGE" in _classes(result)


def test_c_new_customer_phrase_map(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE
        + "\nPHRASE_MAP = {'ابي اشوف كم مرة', 'كم طلب سويت'}\n",
    )
    head = _commit(repo, "phrase map")
    result = _scan(repo, base, head)
    assert "PHRASE_MAP_CHANGE" in _classes(result)


def test_d_keyword_router_addition(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE
        + "\ndef route(message):\n    if 'وين طلبي' in (message or ''):\n        return 'track'\n    return None\n",
    )
    head = _commit(repo, "keyword router")
    result = _scan(repo, base, head)
    assert "KEYWORD_ROUTER_CHANGE" in _classes(result)


def test_e_model_identifier_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(repo, "backend/modules/ai/orchestrator/customer_chat_models.py", 'MODEL_LUNA = "gpt-5.6-terra"\n')
    head = _commit(repo, "model change")
    result = _scan(repo, base, head)
    assert "MODEL_CHANGE" in _classes(result)


def test_f_system_prompt_instruction_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/prompts/builder.py",
        'SYSTEM = "You must always interpret كم مرة as a product usage question."\n',
    )
    head = _commit(repo, "prompt change")
    result = _scan(repo, base, head)
    assert "PROMPT_CHANGE" in _classes(result)


def test_g_persona_behavior_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/persona_expression.py",
        "def compose(text):\n    return 'fixed greeting'\n",
    )
    head = _commit(repo, "persona change")
    result = _scan(repo, base, head)
    assert "PERSONA_CHANGE" in _classes(result)


def test_h_tenant_specific_semantic_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE
        + "\ndef route(message, tenant_id=None):\n    if tenant_id == 33:\n        return 'special'\n    return None\n",
    )
    head = _commit(repo, "tenant hack")
    result = _scan(repo, base, head)
    assert "TENANT_SPECIFIC_SEMANTIC_CHANGE" in _classes(result)


def test_i_phone_specific_semantic_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE
        + "\ndef route(message, phone=None):\n    if phone == '966500000000':\n        return 'special'\n    return None\n",
    )
    head = _commit(repo, "phone hack")
    result = _scan(repo, base, head)
    assert "PHONE_SPECIFIC_SEMANTIC_CHANGE" in _classes(result)


def test_j_product_specific_semantic_branch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE
        + "\ndef route(message, product_id=None):\n    if product_id == '1921568272':\n        return 'special'\n    return None\n",
    )
    head = _commit(repo, "product hack")
    result = _scan(repo, base, head)
    assert "PRODUCT_SPECIFIC_SEMANTIC_CHANGE" in _classes(result)


def test_k_same_pr_self_waiver_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/governance/intelligence_exceptions.json",
        '{"schema_version":1,"exceptions":[{"exception_id":"EX-SELF","change_class":"CUSTOMER_REGEX_CHANGE","exact_file_scope":["backend/modules/ai/brain/intent/rules.py"],"exact_reason":"self","owner_approval_ref":"none","created_at":"2026-09-03","expires_at":"2027-01-01"}]}\n',
    )
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE + "\nimport re as _re\n_USAGE_RE = _re.compile(r'كم مرة')\n",
    )
    head = _commit(repo, "self waiver")
    result = _scan(repo, base, head)
    assert "SAME_PR_SELF_WAIVER" in _classes(result)
    # HEAD exception must not authorize the regex change.
    assert "CUSTOMER_REGEX_CHANGE" in _classes(result)


def test_l_protected_test_removal_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/tests/test_order_support_d1_natural_ownership.py",
        "import pytest\npytestmark = pytest.mark.governance_contract\n",
    )
    head = _commit(repo, "remove protected test")
    result = _scan(repo, base, head)
    assert "PROTECTED_CONTRACT_REMOVAL" in _classes(result)


def test_m_protected_test_weakening_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/tests/test_order_support_d1_natural_ownership.py",
        PROTECTED_BASE.replace("assert True", "assert True or False"),
    )
    head = _commit(repo, "weaken protected test")
    result = _scan(repo, base, head)
    assert "PROTECTED_CONTRACT_WEAKENING" in _classes(result)


def test_n_guard_modification_by_feature_pr_blocked(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    scanner = (repo / "scripts/lint_intelligence_non_interference.py").read_text(encoding="utf-8")
    _write(
        repo,
        "scripts/lint_intelligence_non_interference.py",
        scanner + "\n# weaken guard\n",
    )
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        RULES_BASE + "\ndef extra():\n    return 1\n",
    )
    head = _commit(repo, "feature plus guard edit")
    result = _scan(repo, base, head)
    assert "GOVERNANCE_CORE_CHANGE" in _classes(result)


def test_pass_structured_state_and_evidence_repair(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/commerce/customer_order_evidence.py",
        "def collect():\n    return {\"order_count\": 0, \"has_orders\": False}\n",
    )
    _write(
        repo,
        "backend/modules/ai/brain/state/product_information_topic.py",
        "def detect(message, state=None):\n    return bool(state)\n",
    )
    head = _commit(repo, "state and evidence")
    result = _scan(repo, base, head)
    assert _classes(result) == set()


def test_pass_structural_id_regex_not_customer_semantics(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/intent/rules.py",
        '"""intent rules"""\nimport re\n_ORDER_ID_RE = re.compile(r"^ORD-\\\\d{8}$")\ndef match(message: str):\n    return bool(_ORDER_ID_RE.search(message or ""))\n',
    )
    head = _commit(repo, "id regex")
    result = _scan(repo, base, head)
    assert "CUSTOMER_REGEX_CHANGE" not in _classes(result)


def test_pass_url_regex_outside_semantics_and_tool_persistence(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/brain/execution/tools.py",
        "import re\n_URL_RE = re.compile(r'https://example[.]com/[A-Za-z0-9_-]+')\n",
    )
    _write(
        repo,
        "backend/modules/ai/orchestrator/ai_usage_ledger.py",
        "def persist(row):\n    return row\n",
    )
    _write(
        repo,
        "backend/modules/ai/brain/postprocess/payment_reply_guard.py",
        "def strip_unproven(text, evidence):\n    return text if evidence else text\n",
    )
    head = _commit(repo, "tools persistence postprocess")
    result = _scan(repo, base, head)
    assert _classes(result) == set()


def test_pass_provider_retry_without_model_selection_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    _write(
        repo,
        "backend/modules/ai/orchestrator/providers/openai_compatible_provider.py",
        'MODEL = "gpt-5.6-luna"\ndef call():\n    return MODEL\n',
    )
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/modules/ai/orchestrator/providers/openai_compatible_provider.py",
        'MODEL = "gpt-5.6-luna"\ndef call():\n    for _ in range(2):\n        return MODEL\n    return MODEL\n',
    )
    head = _commit(repo, "retry only")
    result = _scan(repo, base, head)
    assert "MODEL_CHANGE" not in _classes(result)


def test_marker_only_change_is_not_weakening(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    _write(
        repo,
        "backend/tests/test_order_support_d1_natural_ownership.py",
        "def test_count_not_product_usage():\n    assert True\n",
    )
    base = _commit(repo, "base")
    _write(
        repo,
        "backend/tests/test_order_support_d1_natural_ownership.py",
        "import pytest\npytestmark = pytest.mark.governance_contract\n\ndef test_count_not_product_usage():\n    assert True\n",
    )
    head = _commit(repo, "add marker")
    result = _scan(repo, base, head)
    assert "PROTECTED_CONTRACT_WEAKENING" not in _classes(result)


def test_base_unavailable_fails_closed(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    _commit(repo, "base")
    result = _scan(repo, "missing-base", "HEAD")
    assert "BASE_NOT_AVAILABLE" in _classes(result)
    assert GUARD.main(["--repo", str(repo), "--base", "", "--head", "HEAD"]) == 1


def test_bootstrap_allows_introducing_the_scanner(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write(repo, "README.md", "nahla\n")
    base = _commit(repo, "no scanner")
    _seed_governance(repo)
    head = _commit(repo, "introduce scanner")
    result = _scan(repo, base, head, bootstrap=True)
    assert "GOVERNANCE_CORE_CHANGE" not in _classes(result)


def test_cli_success_output_contract(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _seed_governance(repo)
    base = _commit(repo, "base")
    _write(repo, "README.md", "ok\n")
    head = _commit(repo, "docs only")
    code = GUARD.main(
        ["--repo", str(repo), "--base", base, "--head", head, "--trusted-base-scanner"]
    )
    assert code == 0
