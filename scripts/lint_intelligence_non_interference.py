#!/usr/bin/env python
"""GOV-002 trusted intelligence non-interference diff guard.

Stdlib only. On pull_request CI this file MUST be loaded from BASE:

    git show "$BASE_SHA:scripts/lint_intelligence_non_interference.py"

HEAD must not be able to teach the guard to accept a violation.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

SCANNER_REL = "scripts/lint_intelligence_non_interference.py"
EXCEPTIONS_REL = "backend/modules/ai/governance/intelligence_exceptions.json"

CHANGE_CLASSES = (
    "MODEL_CHANGE",
    "PROMPT_CHANGE",
    "PERSONA_CHANGE",
    "PHRASE_MAP_CHANGE",
    "KEYWORD_ROUTER_CHANGE",
    "CUSTOMER_REGEX_CHANGE",
    "CANNED_REPLY_CHANGE",
    "TENANT_SPECIFIC_SEMANTIC_CHANGE",
    "PHONE_SPECIFIC_SEMANTIC_CHANGE",
    "PRODUCT_SPECIFIC_SEMANTIC_CHANGE",
    "SAME_PR_SELF_WAIVER",
    "GOVERNANCE_CORE_CHANGE",
    "PROTECTED_CONTRACT_REMOVAL",
    "PROTECTED_CONTRACT_WEAKENING",
    "UNSAFE_PARTIAL_REPAIR",
    "BASE_NOT_AVAILABLE",
    "MALFORMED_EXCEPTION_REGISTRY",
)

SEMANTIC_PREFIXES = (
    "backend/modules/ai/brain/intent/",
    "backend/modules/ai/brain/interpret/",
    "backend/modules/ai/brain/decision/",
    "backend/modules/ai/brain/state/",
    "backend/modules/ai/brain/turn/",
    "backend/modules/ai/brain/commerce/",
    "backend/modules/ai/knowledge/",
)
SEMANTIC_FILES = frozenset({"backend/modules/ai/brain/pipeline.py"})

MODEL_PREFIXES = ("backend/modules/ai/orchestrator/providers/",)
MODEL_FILES = frozenset(
    {
        "backend/modules/ai/orchestrator/customer_chat_models.py",
        "backend/modules/ai/orchestrator/provider_router.py",
        "backend/modules/ai/orchestrator/llm_cost_audit.py",
    }
)

PROMPT_PREFIXES = (
    "backend/modules/ai/prompts/",
    "services/ai-orchestrator/prompt/",
)
PROMPT_FILES = frozenset(
    {
        "backend/modules/ai/brain/compose/prompt_builder.py",
        "backend/modules/ai/brain/persona/prompts.py",
        "backend/modules/ai/brain/intent/coupon_capability_probe.py",
    }
)
EVIDENCE_FILES = frozenset(
    {
        "backend/modules/ai/brain/commerce/customer_order_evidence.py",
        "backend/modules/ai/brain/compose/prompt_state_serializer.py",
        "backend/modules/ai/brain/compose/prompt_payload_slim.py",
    }
)

PERSONA_PREFIXES = ("backend/modules/ai/brain/persona/",)
PERSONA_FILES = frozenset(
    {
        "backend/modules/ai/brain/persona_expression.py",
        "backend/modules/ai/brain/persona_ownership.py",
        "backend/modules/ai/prompts/nahla_persona.py",
    }
)
CANNED_FILES = frozenset(
    {
        "backend/modules/ai/brain/compose/responder.py",
        "backend/modules/ai/brain/compose/templates.py",
    }
)

GOVERNANCE_CORE = frozenset(
    {
        "scripts/lint_intelligence_non_interference.py",
        "backend/modules/ai/governance/intelligence_non_interference.py",
        "backend/modules/ai/governance/intelligence_exceptions.json",
        "backend/modules/ai/governance/__init__.py",
        "backend/tests/test_intelligence_non_interference_guard.py",
        "backend/tests/test_constitution_compliance.py",
        ".github/workflows/ci.yml",
        ".github/workflows/gov002-intelligence-non-interference.yml",
    }
)
GOVERNANCE_DOCS = frozenset(
    {
        "AGENTS.md",
        "docs/engineering/intelligence-non-interference-policy.md",
        "docs/engineering/ai-pr-constitution-checklist.md",
        "docs/engineering/gov002-workflow-trust-root.md",
        "pytest.ini",
        ".github/CODEOWNERS",
    }
)
AUTHORIZATION_ONLY = frozenset(
    {
        "backend/modules/ai/governance/intelligence_exceptions.json",
        "docs/engineering/intelligence-non-interference-policy.md",
        "docs/engineering/ai-pr-constitution-checklist.md",
        "AGENTS.md",
    }
)

PROTECTED_CONTRACT_MODULES = (
    "backend/tests/test_order_support_d1_natural_ownership.py",
    "backend/tests/test_order_support_d1b_turn_arbiter_preservation.py",
    "backend/tests/test_product_attribute_questions_order_flow.py",
    "backend/tests/test_product_correction_topic_shift.py",
    "backend/tests/test_order_history_intent_routing.py",
    "backend/tests/test_commerce_contract_preserve_order_support.py",
    "backend/tests/test_post_decision_order_support_preservation.py",
)

OWNERSHIP_PREFIXES = (
    "backend/modules/ai/brain/state/",
    "backend/modules/ai/brain/commerce/",
    "backend/modules/ai/brain/decision/",
    "backend/modules/ai/brain/turn/",
    "backend/modules/ai/brain/intent/",
)

RUNTIME_AI_PREFIXES = (
    "backend/modules/ai/brain/",
    "backend/modules/ai/orchestrator/",
    "backend/modules/ai/prompts/",
    "backend/modules/ai/knowledge/",
    "backend/modules/ai/compose/",
)

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_MODEL_SLUG_RE = re.compile(
    r"(gpt-|claude-|gemini-|o1-|o3-|gpt-5\.6-)",
    re.IGNORECASE,
)
_INSTRUCTION_RE = re.compile(
    r"(you must|you should|always answer|never say|interpret the|"
    r"system prompt|do not tell|instructions?:)",
    re.IGNORECASE,
)
_STRUCTURAL_REGEX_HINT = re.compile(
    r"(\\d|https?://|uuid|wamid|sha256|[0-9]{3,}|\\\\b[0-9]|order_id|phone_e164)",
    re.IGNORECASE,
)
_IDENTITY_NAMES = frozenset(
    {
        "tenant_id",
        "tenant",
        "phone",
        "customer_phone",
        "customer_id",
        "sku",
        "product_id",
        "external_id",
        "phone_number_id",
    }
)
_PHONE_NAMES = frozenset({"phone", "customer_phone", "phone_number_id"})
_PRODUCT_NAMES = frozenset({"sku", "product_id", "external_id"})
_TENANT_NAMES = frozenset({"tenant_id", "tenant"})
_STATUS_ENUMS = frozenset(
    {
        "ordering",
        "checkout",
        "confirmed",
        "pending",
        "shipped",
        "delivered",
        "cancelled",
        "canceled",
        "draft",
        "paid",
        "unpaid",
        "true",
        "false",
        "none",
        "llm",
        "inbound",
        "outbound",
    }
)
_GOV_MARK_NAMES = frozenset({"governance_contract"})
_HISTORICAL_PROMPT_EXCEPTION_COMMIT = "02aff3455c777b2d7cc6a4d4a234ae1b0b0b3c00"
_HISTORICAL_PROMPT_EXCEPTION_FILE = (
    "backend/modules/ai/brain/intent/coupon_capability_probe.py"
)


@dataclass
class Finding:
    file: str
    line: int
    change_class: str
    reason: str
    authorized_exception_id: str = ""
    diff_hunk: str = ""
    symbol: str = ""
    change_digest: str = ""


@dataclass
class OwnerException:
    exception_id: str
    change_class: str
    exact_file_scope: Tuple[str, ...]
    exact_reason: str
    owner_approval_ref: str
    created_at: str
    expires_at: str
    expected_change_digest: str = ""
    exact_symbol_scope: str = ""
    single_use: bool = True
    consumed: bool = False
    valid: bool = True
    malformed_reason: str = ""


@dataclass
class ScanResult:
    findings: List[Finding] = field(default_factory=list)
    bootstrap: bool = False
    trusted_base_scanner: bool = False
    flags: Dict[str, str] = field(default_factory=dict)
    used_exception_ids: Set[str] = field(default_factory=set)

    @property
    def unauthorized(self) -> List[Finding]:
        return [f for f in self.findings if not f.authorized_exception_id]


def posix(path: str) -> str:
    return path.replace("\\", "/")


def has_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))


def looks_customer_language(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 3:
        return False
    if has_arabic(s):
        return True
    if " " in s and len(s) >= 8 and any(c.isalpha() for c in s):
        return True
    return False


def is_customer_semantic_literal(text: str) -> bool:
    """True for customer-language literals, not IDs/URLs/enums/protocol values."""
    s = (text or "").strip()
    if not s or not looks_customer_language(s):
        return False
    if s.lower() in _STATUS_ENUMS:
        return False
    if regex_is_structural(s):
        return False
    if re.match(r"^https?://", s, re.IGNORECASE):
        return False
    if re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        s,
        re.IGNORECASE,
    ):
        return False
    if re.match(r"^[A-Z]{2,8}-?\d{4,}$", s):
        return False
    if re.match(r"^\+?\d{8,15}$", s):
        return False
    return True


def compute_change_digest(
    *,
    change_class: str,
    file: str,
    symbol: str,
    payload: str,
) -> str:
    raw = f"{change_class}\n{posix(file)}\n{symbol or ''}\n{payload or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def delta_payload(base_fp: str, head_fp: str) -> str:
    """Canonical BASE→HEAD change used for exception digests. Never a generic reason."""
    return f"BASE\n{base_fp or ''}\nHEAD\n{head_fp or ''}"


def _run_git(repo: str, args: Sequence[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def git_show(repo: str, sha: str, path: str) -> Optional[str]:
    code, out, _err = _run_git(repo, ["show", f"{sha}:{path}"])
    if code != 0:
        return None
    return out


def git_exists(repo: str, sha: str, path: str) -> bool:
    code, _out, _err = _run_git(repo, ["cat-file", "-e", f"{sha}:{path}"])
    return code == 0


def git_rev_parse(repo: str, rev: str) -> Optional[str]:
    code, out, _err = _run_git(repo, ["rev-parse", "--verify", rev])
    if code != 0:
        return None
    return out.strip()


def changed_paths(repo: str, base: str, head: str) -> List[Tuple[str, str]]:
    code, out, err = _run_git(
        repo,
        ["diff", "--name-status", "--no-renames", f"{base}..{head}"],
    )
    if code != 0:
        raise RuntimeError(f"git diff failed: {err.strip()}")
    rows: List[Tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        rows.append((parts[0].strip(), posix(parts[1].strip())))
    return rows


def matches_prefix(path: str, prefixes: Sequence[str], files: Iterable[str] = ()) -> bool:
    p = posix(path)
    if p in set(files):
        return True
    return any(p.startswith(pref) for pref in prefixes)


def is_semantic_surface(path: str) -> bool:
    return matches_prefix(path, SEMANTIC_PREFIXES, SEMANTIC_FILES)


def is_model_selection(path: str) -> bool:
    return matches_prefix(path, MODEL_PREFIXES, MODEL_FILES)


def is_prompt_instruction(path: str) -> bool:
    p = posix(path)
    if p in EVIDENCE_FILES:
        return False
    return matches_prefix(path, PROMPT_PREFIXES, PROMPT_FILES)


def is_persona_runtime(path: str) -> bool:
    p = posix(path)
    if p.endswith("prompts.py") and "/persona/" in p:
        return True
    return matches_prefix(path, PERSONA_PREFIXES, PERSONA_FILES)


def is_governance_core(path: str) -> bool:
    return posix(path) in GOVERNANCE_CORE


def is_governance_doc(path: str) -> bool:
    return posix(path) in GOVERNANCE_DOCS


def is_runtime_ai(path: str) -> bool:
    p = posix(path)
    if p.startswith("backend/modules/ai/governance/"):
        return False
    if p.startswith("backend/tests/"):
        return False
    return any(p.startswith(pref) for pref in RUNTIME_AI_PREFIXES)


def is_ownership_production(path: str) -> bool:
    return matches_prefix(path, OWNERSHIP_PREFIXES)


def parse_ast(source: str, filename: str = "<src>") -> Optional[ast.AST]:
    try:
        return ast.parse(source or "", filename=filename)
    except SyntaxError:
        return None


def const_str(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return None


def dump_node(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def is_re_compile(call: ast.Call) -> bool:
    func = call.func
    return isinstance(func, ast.Attribute) and func.attr == "compile"


def regex_fingerprints(source: str) -> Dict[str, Tuple[int, str]]:
    tree = parse_ast(source)
    out: Dict[str, Tuple[int, str]] = {}
    if tree is None:
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = _name_of(target)
                if name.endswith("_RE") or name.endswith("_REGEX"):
                    out[f"assign:{name}"] = (getattr(node, "lineno", 1), dump_node(node.value))
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            name = _name_of(node.target)
            if name.endswith("_RE") or name.endswith("_REGEX"):
                out[f"assign:{name}"] = (getattr(node, "lineno", 1), dump_node(node.value))
        if isinstance(node, ast.Call) and is_re_compile(node):
            key = f"compile:{getattr(node, 'lineno', 1)}:{dump_node(node)}"
            out[key] = (getattr(node, "lineno", 1), dump_node(node))
    return out


def regex_is_structural(pattern: str) -> bool:
    if has_arabic(pattern):
        return False
    if looks_customer_language(pattern) and not _STRUCTURAL_REGEX_HINT.search(pattern):
        return False
    if _STRUCTURAL_REGEX_HINT.search(pattern):
        return True
    if not any(c.isalpha() for c in pattern):
        return True
    return False


def extract_compile_patterns(source: str) -> List[str]:
    tree = parse_ast(source)
    if tree is None:
        return []
    patterns: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_re_compile(node) and node.args:
            text = const_str(node.args[0])
            if text is not None:
                patterns.append(text)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            name = ""
            value = None
            if isinstance(node, ast.Assign):
                name = _name_of(node.targets[0]) if node.targets else ""
                value = node.value
            else:
                name = _name_of(node.target)
                value = node.value
            if name.endswith("_RE") or name.endswith("_REGEX"):
                text = const_str(value) if value is not None else None
                if text is not None:
                    patterns.append(text)
                elif isinstance(value, ast.Call) and value.args:
                    inner = const_str(value.args[0])
                    if inner is not None:
                        patterns.append(inner)
    return patterns


def collection_strings(node: ast.AST) -> Optional[List[str]]:
    elts: Sequence[ast.AST]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        elts = node.elts
    elif isinstance(node, ast.Dict):
        elts = [k for k in node.keys if k is not None]
    else:
        return None
    values: List[str] = []
    for elt in elts:
        text = const_str(elt)
        if text is None:
            return None
        values.append(text)
    return values


def phrase_map_assigns(source: str) -> Dict[str, Tuple[int, str]]:
    tree = parse_ast(source)
    out: Dict[str, Tuple[int, str]] = {}
    if tree is None:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            names = [_name_of(t) for t in node.targets]
            value = node.value
        else:
            names = [_name_of(node.target)]
            value = node.value
        if value is None:
            continue
        strings = collection_strings(value)
        if not strings:
            continue
        customerish = [s for s in strings if is_customer_semantic_literal(s)]
        name_hit = any(
            any(tok in (n or "").upper() for tok in ("PHRASE", "KEYWORD", "TRIGGER", "DENYLIST", "ALLOWLIST"))
            for n in names
        )
        if name_hit and customerish:
            key = ",".join(names) or f"line{getattr(node, 'lineno', 1)}"
            out[key] = (getattr(node, "lineno", 1), dump_node(value))
        elif customerish:
            key = ",".join(names) or f"line{getattr(node, 'lineno', 1)}"
            out[key] = (getattr(node, "lineno", 1), dump_node(value))
    return out


def keyword_in_checks(source: str) -> Set[str]:
    tree = parse_ast(source)
    hits: Set[str] = set()
    if tree is None:
        return hits
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                texts = [const_str(node.left), const_str(comparator)]
                for text in texts:
                    if not text or not is_customer_semantic_literal(text):
                        continue
                    if isinstance(op, (ast.Eq, ast.NotEq)):
                        hits.add(f"{getattr(node, 'lineno', 1)}:eq:{text}")
                    elif isinstance(op, (ast.In, ast.NotIn)):
                        hits.add(f"{getattr(node, 'lineno', 1)}:in:{text}")
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = ""
        if isinstance(func, ast.Attribute):
            attr = func.attr
        elif isinstance(func, ast.Name):
            attr = func.id
        arg0 = const_str(node.args[0]) if node.args else None
        if attr in {"startswith", "endswith"} and arg0 and is_customer_semantic_literal(arg0):
            hits.add(f"{getattr(node, 'lineno', 1)}:{attr}:{arg0}")
        if attr in {"search", "match", "fullmatch"} and arg0 and is_customer_semantic_literal(arg0):
            hits.add(f"{getattr(node, 'lineno', 1)}:re_{attr}:{arg0}")
    return hits


def helper_literal_hits(source: str) -> Set[str]:
    """Customer literals passed into helper calls used as ownership branches."""
    tree = parse_ast(source)
    hits: Set[str] = set()
    if tree is None:
        return hits

    def _calls_in(expr: ast.AST) -> None:
        for node in ast.walk(expr):
            if not isinstance(node, ast.Call):
                continue
            args = list(node.args) + [kw.value for kw in node.keywords]
            for arg in args:
                text = const_str(arg)
                if text and is_customer_semantic_literal(text):
                    hits.add(f"{getattr(node, 'lineno', 1)}:helper:{text}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While)):
            _calls_in(node.test)
        elif isinstance(node, ast.IfExp):
            _calls_in(node.test)
        elif isinstance(node, ast.Assert):
            _calls_in(node.test)
        elif isinstance(node, ast.Return) and node.value is not None:
            _calls_in(node.value)
    return hits


def identity_eq_hits(source: str) -> List[Tuple[int, str, str]]:
    tree = parse_ast(source)
    hits: List[Tuple[int, str, str]] = []
    if tree is None:
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops):
            continue
        left_name = _name_of(node.left)
        if left_name not in _IDENTITY_NAMES:
            continue
        for comparator in node.comparators:
            value = None
            if isinstance(comparator, ast.Constant):
                value = comparator.value
            if value is None:
                continue
            if value in (None, "", 0, False, True):
                continue
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit() and len(value) >= 3):
                hits.append((getattr(node, "lineno", 1), left_name, repr(value)))
    return hits


def model_selection_fingerprint(source: str) -> str:
    tree = parse_ast(source)
    parts: List[str] = []
    if tree is None:
        return source
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _MODEL_SLUG_RE.search(node.value):
                parts.append(f"S:{node.value}")
        if isinstance(node, ast.Assign):
            names = [_name_of(t) for t in node.targets]
            if any("MODEL" in (n or "").upper() for n in names):
                parts.append(f"A:{','.join(names)}={dump_node(node.value)}")
        if isinstance(node, ast.FunctionDef) and "model" in node.name.lower():
            parts.append(f"F:{node.name}:{dump_node(node)}")
    return "\n".join(sorted(parts))


def prompt_instruction_fingerprint(source: str) -> str:
    tree = parse_ast(source)
    parts: List[str] = []
    if tree is None:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if len(text) < 40:
                continue
            if _INSTRUCTION_RE.search(text) or "You classify" in text or "system" in text.lower():
                parts.append(text.strip())
    return "\n---\n".join(sorted(parts))


def canned_return_fingerprint(source: str) -> Set[str]:
    tree = parse_ast(source)
    hits: Set[str] = set()
    if tree is None:
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        text = const_str(node.value)
        if text and looks_customer_language(text) and len(text.strip()) >= 8:
            hits.add(f"{getattr(node, 'lineno', 1)}:{text.strip()}")
    return hits


def is_gov_mark_decorator(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr in _GOV_MARK_NAMES:
        return True
    if isinstance(node, ast.Name) and node.id in _GOV_MARK_NAMES:
        return True
    if isinstance(node, ast.Call):
        return is_gov_mark_decorator(node.func)
    return False


def function_fingerprint(func: ast.AST) -> str:
    if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return dump_node(func)
    keep = [d for d in func.decorator_list if not is_gov_mark_decorator(d)]
    return ast.dump(
        ast.Tuple(
            elts=[
                ast.Constant(func.name),
                func.args,
                ast.Module(body=list(func.body), type_ignores=[]),
                ast.List(elts=keep, ctx=ast.Load()),
            ],
            ctx=ast.Load(),
        ),
        include_attributes=False,
    )


def test_function_fingerprints(source: str) -> Dict[str, str]:
    tree = parse_ast(source)
    out: Dict[str, str] = {}
    if tree is None:
        return {"__parse_error__": "1"}
    if not isinstance(tree, ast.Module):
        return out

    def add_func(prefix: str, func: ast.AST) -> None:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        if not func.name.startswith("test_"):
            return
        key = f"{prefix}{func.name}" if prefix else func.name
        out[key] = function_fingerprint(func)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add_func("", node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                add_func(f"{node.name}::", child)
    return out


def marked_test_names(source: str) -> Set[str]:
    tree = parse_ast(source)
    names: Set[str] = set()
    if tree is None:
        return names
    module_marked = False
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign):
            if any(_name_of(t) == "pytestmark" for t in node.targets):
                dumped = dump_node(node.value)
                if "governance_contract" in dumped:
                    module_marked = True
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        marked = any(is_gov_mark_decorator(d) for d in node.decorator_list) or module_marked
        if isinstance(node, ast.ClassDef) and marked:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"):
                    names.add(f"{node.name}::{child.name}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_") and marked:
            names.add(node.name)
    return names


def load_exceptions(raw: Optional[str]) -> Tuple[List[OwnerException], List[str]]:
    if not (raw or "").strip():
        return [], []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return [], ["exception registry is not valid JSON"]
    rows = payload.get("exceptions") if isinstance(payload, dict) else payload
    if rows is None:
        return [], []
    if not isinstance(rows, list):
        return [], ["exceptions must be a list"]
    valid: List[OwnerException] = []
    malformed: List[str] = []
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            malformed.append(f"exceptions[{idx}] is not an object")
            continue
        scope = row.get("exact_file_scope") or []
        if isinstance(scope, str):
            scope = [scope]
        if not isinstance(scope, list) or not scope:
            malformed.append(f"exceptions[{idx}] missing exact_file_scope")
            continue
        digest = str(row.get("expected_change_digest") or "").strip()
        owner_ref = str(row.get("owner_approval_ref") or "").strip()
        created = str(row.get("created_at") or "").strip()
        expires = str(row.get("expires_at") or "").strip()
        exception_id = str(row.get("exception_id") or "").strip()
        change_class = str(row.get("change_class") or "").strip()
        missing = []
        if not exception_id:
            missing.append("exception_id")
        if not change_class:
            missing.append("change_class")
        if not digest:
            missing.append("expected_change_digest")
        if not owner_ref:
            missing.append("owner_approval_ref")
        if not created:
            missing.append("created_at")
        if not expires:
            missing.append("expires_at")
        if "single_use" not in row:
            missing.append("single_use")
        if "consumed" not in row:
            missing.append("consumed")
        if missing:
            malformed.append(
                f"exceptions[{idx}] ({exception_id or 'unknown'}) missing {','.join(missing)}"
            )
            continue
        try:
            datetime.strptime(created, "%Y-%m-%d")
            datetime.strptime(expires, "%Y-%m-%d")
        except ValueError:
            malformed.append(f"exceptions[{idx}] has invalid created_at/expires_at")
            continue
        single_use = row.get("single_use")
        consumed = row.get("consumed")
        if single_use is not True:
            malformed.append(f"exceptions[{idx}] single_use must be true")
            continue
        if not isinstance(consumed, bool):
            malformed.append(f"exceptions[{idx}] consumed must be boolean")
            continue
        valid.append(
            OwnerException(
                exception_id=exception_id,
                change_class=change_class,
                exact_file_scope=tuple(str(s) for s in scope),
                exact_reason=str(row.get("exact_reason") or ""),
                owner_approval_ref=owner_ref,
                created_at=created,
                expires_at=expires,
                expected_change_digest=digest,
                exact_symbol_scope=str(row.get("exact_symbol_scope") or ""),
                single_use=single_use,
                consumed=consumed,
            )
        )
    return valid, malformed


def exception_active(exc: OwnerException, *, as_of: Optional[date] = None) -> bool:
    if not exc.valid or not exc.exception_id or not exc.change_class:
        return False
    if exc.consumed:
        return False
    if exc.single_use is not True:
        return False
    if not exc.expected_change_digest or not exc.owner_approval_ref or not exc.expires_at:
        return False
    as_of = as_of or date.today()
    try:
        exp = datetime.strptime(exc.expires_at, "%Y-%m-%d").date()
    except ValueError:
        return False
    if as_of > exp:
        return False
    return True


def authorize(
    exceptions: Sequence[OwnerException],
    finding: Finding,
    *,
    used_ids: Optional[Set[str]] = None,
) -> Finding:
    posix_file = posix(finding.file)
    used_ids = used_ids if used_ids is not None else set()
    for exc in exceptions:
        if exc.change_class != finding.change_class:
            continue
        if not exception_active(exc):
            continue
        if posix_file not in exc.exact_file_scope:
            continue
        if exc.exact_symbol_scope and exc.exact_symbol_scope != (finding.symbol or ""):
            continue
        if exc.expected_change_digest != finding.change_digest:
            continue
        if exc.single_use and exc.exception_id in used_ids:
            continue
        finding.authorized_exception_id = exc.exception_id
        used_ids.add(exc.exception_id)
        return finding
    return finding


def add_finding(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    *,
    file: str,
    line: int,
    change_class: str,
    reason: str,
    diff_hunk: str = "",
    symbol: str = "",
    payload: str = "",
) -> None:
    # Authorization digest is bound to the caller-supplied canonical delta.
    # Generic reason/diff_hunk must not become the digest authority.
    digest = compute_change_digest(
        change_class=change_class,
        file=file,
        symbol=symbol,
        payload=payload,
    )
    finding = Finding(
        file=posix(file),
        line=line,
        change_class=change_class,
        reason=reason,
        diff_hunk=diff_hunk[:240],
        symbol=symbol,
        change_digest=digest,
    )
    result.findings.append(
        authorize(exceptions, finding, used_ids=result.used_exception_ids)
    )


def _scan_regex(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    if not is_semantic_surface(path):
        return
    if head_src is None:
        return
    base_fp = regex_fingerprints(base_src or "")
    head_fp = regex_fingerprints(head_src)
    if base_fp == head_fp:
        return
    base_patterns = set(extract_compile_patterns(base_src or ""))
    head_patterns = set(extract_compile_patterns(head_src))
    added = head_patterns - base_patterns
    removed = base_patterns - head_patterns
    structural_only = True
    for pat in added | removed:
        if not regex_is_structural(pat):
            structural_only = False
            break
    if structural_only and not added and not removed:
        # fingerprint changed (formatting) but no customer-language pattern shift
        if all(regex_is_structural(p) for p in head_patterns):
            return
    if structural_only and added and all(regex_is_structural(p) for p in added):
        return
    kind = "modified semantic regex" if (base_src and path) else "new semantic regex"
    if not base_src:
        kind = "new semantic regex"
    elif added and not removed:
        kind = "new semantic regex"
    add_finding(
        result,
        exceptions,
        file=path,
        line=1,
        change_class="CUSTOMER_REGEX_CHANGE",
        reason=f"{kind} in semantic ownership surface",
        diff_hunk="; ".join(sorted(added | removed)[:4]),
        symbol="",
        payload=delta_payload(
            "\n".join(f"{k}:{v[1]}" for k, v in sorted(base_fp.items())),
            "\n".join(f"{k}:{v[1]}" for k, v in sorted(head_fp.items())),
        ),
    )


def _scan_phrase_keyword(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    if not is_semantic_surface(path) or head_src is None:
        return
    base_maps = phrase_map_assigns(base_src or "")
    head_maps = phrase_map_assigns(head_src)
    for key, (line, dump) in head_maps.items():
        if base_maps.get(key, (0, ""))[1] != dump:
            add_finding(
                result,
                exceptions,
                file=path,
                line=line,
                change_class="PHRASE_MAP_CHANGE",
                reason=f"customer-language collection '{key}' added or modified",
                symbol=key,
                payload=delta_payload(base_maps.get(key, (0, ""))[1], dump),
            )
    base_kw = keyword_in_checks(base_src or "")
    head_kw = keyword_in_checks(head_src)
    for extra in sorted(head_kw - base_kw):
        parts = extra.split(":", 2)
        line_s = parts[0] if parts else "1"
        kind = parts[1] if len(parts) > 1 else "in"
        text = parts[2] if len(parts) > 2 else extra
        if kind.startswith("re_"):
            cls = "CUSTOMER_REGEX_CHANGE"
            reason = f"customer-language {kind} in semantic ownership surface"
        else:
            cls = "KEYWORD_ROUTER_CHANGE"
            reason = f"customer-language {kind} branch in semantic ownership surface"
        add_finding(
            result,
            exceptions,
            file=path,
            line=int(line_s) if line_s.isdigit() else 1,
            change_class=cls,
            reason=reason,
            diff_hunk=text[:120],
            symbol=kind,
            payload=extra,
        )
    base_helpers = helper_literal_hits(base_src or "")
    head_helpers = helper_literal_hits(head_src)
    for extra in sorted(head_helpers - base_helpers):
        parts = extra.split(":", 2)
        line_s = parts[0] if parts else "1"
        text = parts[2] if len(parts) > 2 else extra
        add_finding(
            result,
            exceptions,
            file=path,
            line=int(line_s) if line_s.isdigit() else 1,
            change_class="KEYWORD_ROUTER_CHANGE",
            reason="helper call wrapping a customer-language literal in an ownership branch",
            diff_hunk=text[:120],
            symbol="helper",
            payload=extra,
        )


def _scan_identity(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    if not is_semantic_surface(path) or head_src is None:
        return
    base_hits = set(identity_eq_hits(base_src or ""))
    head_hits = identity_eq_hits(head_src)
    for line, name, value in head_hits:
        if (line, name, value) in base_hits:
            continue
        # New identity-specific branch; line numbers can shift, compare name+value.
        if any(n == name and v == value for _ln, n, v in base_hits):
            continue
        if name in _TENANT_NAMES:
            cls = "TENANT_SPECIFIC_SEMANTIC_CHANGE"
        elif name in _PHONE_NAMES:
            cls = "PHONE_SPECIFIC_SEMANTIC_CHANGE"
        elif name in _PRODUCT_NAMES:
            cls = "PRODUCT_SPECIFIC_SEMANTIC_CHANGE"
        else:
            cls = "TENANT_SPECIFIC_SEMANTIC_CHANGE"
        add_finding(
            result,
            exceptions,
            file=path,
            line=line,
            change_class=cls,
            reason=f"semantic branch conditioned on {name}=={value}",
            symbol=name,
            payload=delta_payload("", f"{name}=={value}"),
        )


def _scan_model(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    if not is_model_selection(path) or head_src is None:
        return
    base_fp = model_selection_fingerprint(base_src or "")
    head_fp = model_selection_fingerprint(head_src)
    if base_fp == head_fp:
        return
    add_finding(
        result,
        exceptions,
        file=path,
        line=1,
        change_class="MODEL_CHANGE",
        reason="model identifier / routing / fallback selection changed",
        payload=delta_payload(base_fp, head_fp),
    )


def _scan_prompt(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    if not is_prompt_instruction(path) or head_src is None:
        return
    base_fp = prompt_instruction_fingerprint(base_src or "")
    head_fp = prompt_instruction_fingerprint(head_src)
    if base_fp == head_fp:
        return
    add_finding(
        result,
        exceptions,
        file=path,
        line=1,
        change_class="PROMPT_CHANGE",
        reason="model instruction / system prompt text changed",
        payload=delta_payload(base_fp, head_fp),
    )


def _scan_persona(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    if not is_persona_runtime(path) or path.endswith("prompts.py"):
        # prompts.py already covered as PROMPT_CHANGE
        if path.endswith("prompts.py"):
            return
    if not is_persona_runtime(path) or head_src is None:
        return
    base_tree = parse_ast(base_src or "")
    head_tree = parse_ast(head_src)
    if head_tree is None:
        add_finding(
            result,
            exceptions,
            file=path,
            line=1,
            change_class="PERSONA_CHANGE",
            reason="persona runtime file is unparseable on HEAD",
            payload=delta_payload(
                dump_node(base_tree or ast.parse("pass")),
                "UNPARSEABLE",
            ),
        )
        return
    base_fp = dump_node(base_tree or ast.parse("pass"))
    head_fp = dump_node(head_tree)
    if base_fp == head_fp:
        return
    add_finding(
        result,
        exceptions,
        file=path,
        line=1,
        change_class="PERSONA_CHANGE",
        reason="persona runtime behavior surface changed",
        payload=delta_payload(base_fp, head_fp),
    )


def _scan_canned(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    path: str,
    base_src: Optional[str],
    head_src: Optional[str],
) -> None:
    canned = posix(path) in CANNED_FILES or (
        is_persona_runtime(path) and path.endswith("_answer.py")
    )
    if not canned or head_src is None:
        return
    extra = canned_return_fingerprint(head_src) - canned_return_fingerprint(base_src or "")
    for item in sorted(extra):
        line_s, _, text = item.partition(":")
        add_finding(
            result,
            exceptions,
            file=path,
            line=int(line_s) if line_s.isdigit() else 1,
            change_class="CANNED_REPLY_CHANGE",
            reason="new fixed customer-facing return in compose/persona surface",
            diff_hunk=text[:160],
            symbol=line_s,
            payload=delta_payload("", item),
        )


def _scan_protected_contracts(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    repo: str,
    base: str,
    head: str,
    changed: Sequence[Tuple[str, str]],
) -> bool:
    """Return True when a protected contract fingerprint changed."""
    changed_map = {path: status for status, path in changed}
    weakened = False
    protected_files = set(PROTECTED_CONTRACT_MODULES)
    # Also discover marked tests in any BASE test file that changed.
    for status, path in changed:
        if path.startswith("backend/tests/") and path.endswith(".py"):
            base_src = git_show(repo, base, path) or ""
            if marked_test_names(base_src):
                protected_files.add(path)

    for path in sorted(protected_files):
        base_src = git_show(repo, base, path)
        head_src = git_show(repo, head, path)
        if base_src is None and head_src is None:
            continue
        if base_src is None:
            continue
        if head_src is None:
            add_finding(
                result,
                exceptions,
                file=path,
                line=1,
                change_class="PROTECTED_CONTRACT_REMOVAL",
                reason="protected governance contract module removed",
                payload=delta_payload(base_src or "", ""),
            )
            weakened = True
            continue
        base_fp = test_function_fingerprints(base_src)
        head_fp = test_function_fingerprints(head_src)
        base_names = set(base_fp) - {"__parse_error__"}
        head_names = set(head_fp) - {"__parse_error__"}
        for name in sorted(base_names - head_names):
            add_finding(
                result,
                exceptions,
                file=path,
                line=1,
                change_class="PROTECTED_CONTRACT_REMOVAL",
                reason=f"protected test '{name}' removed or renamed",
                symbol=name,
                payload=delta_payload(base_fp.get(name, ""), ""),
            )
            weakened = True
        for name in sorted(base_names & head_names):
            if base_fp[name] != head_fp[name]:
                add_finding(
                    result,
                    exceptions,
                    file=path,
                    line=1,
                    change_class="PROTECTED_CONTRACT_WEAKENING",
                    reason=f"protected test '{name}' body/assertions/fixtures changed",
                    symbol=name,
                    payload=delta_payload(base_fp[name], head_fp[name]),
                )
                weakened = True
        _ = changed_map
    return weakened


def _scan_governance_and_waiver(
    result: ScanResult,
    exceptions: Sequence[OwnerException],
    changed: Sequence[Tuple[str, str]],
    *,
    repo: str,
    base: str,
    head: str,
    bootstrap: bool,
) -> None:
    paths = [posix(p) for _s, p in changed]
    core_hits = [p for p in paths if is_governance_core(p)]
    runtime_hits = [p for p in paths if is_runtime_ai(p)]
    auth_hits = [p for p in paths if p in AUTHORIZATION_ONLY]
    non_auth = [p for p in paths if p not in AUTHORIZATION_ONLY and not is_governance_doc(p)]

    def _paths_delta(rel_paths: Sequence[str]) -> str:
        parts: List[str] = []
        for rel in sorted(rel_paths):
            parts.append(
                delta_payload(
                    git_show(repo, base, rel) or "",
                    git_show(repo, head, rel) or "",
                )
            )
        return "\n".join(parts)

    if "backend/modules/ai/governance/intelligence_exceptions.json" in paths:
        if runtime_hits:
            add_finding(
                result,
                exceptions,
                file="backend/modules/ai/governance/intelligence_exceptions.json",
                line=1,
                change_class="SAME_PR_SELF_WAIVER",
                reason="exception registry changed in the same PR as AI runtime code",
                payload=delta_payload(
                    git_show(repo, base, EXCEPTIONS_REL) or "",
                    git_show(repo, head, EXCEPTIONS_REL) or "",
                )
                + "\nRUNTIME\n"
                + "\n".join(sorted(runtime_hits)),
            )

    if core_hits and runtime_hits and not bootstrap:
        add_finding(
            result,
            exceptions,
            file=core_hits[0],
            line=1,
            change_class="GOVERNANCE_CORE_CHANGE",
            reason="governance scanner/CI/registry changed in a non-governance (runtime) PR",
            payload=_paths_delta(core_hits) + "\nRUNTIME\n" + "\n".join(sorted(runtime_hits)),
        )
    elif core_hits and not bootstrap:
        allowed_with_core = set(GOVERNANCE_CORE) | set(GOVERNANCE_DOCS) | set(
            PROTECTED_CONTRACT_MODULES
        )
        featureish = [
            p
            for p in non_auth
            if p not in allowed_with_core
            and not p.startswith("backend/tests/test_intelligence_non_interference")
            and not p.startswith("backend/tests/test_constitution_compliance")
        ]
        if featureish:
            add_finding(
                result,
                exceptions,
                file=core_hits[0],
                line=1,
                change_class="GOVERNANCE_CORE_CHANGE",
                reason="governance core changed together with unrelated files",
                payload=_paths_delta(core_hits) + "\nFEATURE\n" + "\n".join(sorted(featureish)),
            )
    _ = auth_hits


def scan_repository(
    repo: str,
    base: str,
    head: str,
    *,
    bootstrap: bool = False,
    trusted_base_scanner: bool = False,
) -> ScanResult:
    result = ScanResult(bootstrap=bootstrap, trusted_base_scanner=trusted_base_scanner)
    base_sha = git_rev_parse(repo, base)
    head_sha = git_rev_parse(repo, head)
    if not base_sha or not head_sha:
        add_finding(
            result,
            [],
            file=".",
            line=1,
            change_class="BASE_NOT_AVAILABLE",
            reason="BASE_SHA or HEAD_SHA could not be resolved; fail closed",
            payload="unresolved-sha",
        )
        return result

    exceptions, malformed = load_exceptions(git_show(repo, base_sha, EXCEPTIONS_REL))
    for msg in malformed:
        add_finding(
            result,
            [],
            file=EXCEPTIONS_REL,
            line=1,
            change_class="MALFORMED_EXCEPTION_REGISTRY",
            reason=msg,
            payload=msg,
        )
    try:
        changed = changed_paths(repo, base_sha, head_sha)
    except RuntimeError as exc:
        add_finding(
            result,
            exceptions,
            file=".",
            line=1,
            change_class="BASE_NOT_AVAILABLE",
            reason=str(exc),
            payload=str(exc),
        )
        return result

    _scan_governance_and_waiver(
        result,
        exceptions,
        changed,
        repo=repo,
        base=base_sha,
        head=head_sha,
        bootstrap=bootstrap,
    )
    weakened = _scan_protected_contracts(
        result, exceptions, repo, base_sha, head_sha, changed
    )

    ownership_changed = False
    for status, path in changed:
        if is_ownership_production(path) and not path.startswith("backend/tests/"):
            ownership_changed = True
        base_src = git_show(repo, base_sha, path) if status != "A" else None
        head_src = git_show(repo, head_sha, path) if status != "D" else None
        if path.endswith(".py"):
            _scan_regex(result, exceptions, path, base_src, head_src)
            _scan_phrase_keyword(result, exceptions, path, base_src, head_src)
            _scan_identity(result, exceptions, path, base_src, head_src)
            _scan_model(result, exceptions, path, base_src, head_src)
            _scan_prompt(result, exceptions, path, base_src, head_src)
            _scan_persona(result, exceptions, path, base_src, head_src)
            _scan_canned(result, exceptions, path, base_src, head_src)

    if weakened and ownership_changed:
        own_paths = [
            posix(p)
            for _s, p in changed
            if is_ownership_production(p) and not p.startswith("backend/tests/")
        ]
        weak = [
            f"{f.file}:{f.symbol}:{f.change_digest}"
            for f in result.findings
            if f.change_class
            in {"PROTECTED_CONTRACT_REMOVAL", "PROTECTED_CONTRACT_WEAKENING"}
        ]
        add_finding(
            result,
            exceptions,
            file=".",
            line=1,
            change_class="UNSAFE_PARTIAL_REPAIR",
            reason=(
                "semantic ownership production change combined with protected "
                "contract weakening; partial first-divergence repair is unsafe to merge"
            ),
            payload="OWN\n" + "\n".join(sorted(own_paths)) + "\nWEAK\n" + "\n".join(weak),
        )

    result.flags = _flags_from_findings(result.findings)
    return result


def _flags_from_findings(findings: Sequence[Finding]) -> Dict[str, str]:
    present = {f.change_class for f in findings}
    mapping = {
        "MODEL_CHANGED": "MODEL_CHANGE",
        "PROMPT_CHANGED": "PROMPT_CHANGE",
        "PERSONA_CHANGED": "PERSONA_CHANGE",
        "PHRASE_MAP_CHANGED": "PHRASE_MAP_CHANGE",
        "KEYWORD_ROUTER_CHANGED": "KEYWORD_ROUTER_CHANGE",
        "CUSTOMER_REGEX_CHANGED": "CUSTOMER_REGEX_CHANGE",
    }
    return {flag: ("YES" if cls in present else "NO") for flag, cls in mapping.items()}


def format_report(result: ScanResult) -> str:
    if result.bootstrap:
        bootstrap_line = "BOOTSTRAP_HEAD_TRUST_EXCEPTION=YES_ONE_TIME"
        trusted_line = "TRUSTED_BASE_SCANNER_REQUIRED=no"
    else:
        bootstrap_line = "BOOTSTRAP_HEAD_TRUST_EXCEPTION=no"
        trusted_line = (
            "TRUSTED_BASE_SCANNER_REQUIRED=yes"
            if result.trusted_base_scanner
            else "TRUSTED_BASE_SCANNER_REQUIRED=no"
        )
    lines = [
        "INTELLIGENCE_NON_INTERFERENCE_POLICY=ACTIVE",
        trusted_line,
        bootstrap_line,
        f"GOV002_BOOTSTRAP={'yes' if result.bootstrap else 'no'}",
        "SINGLE_USE_SCOPE=PER_SCAN",
        "PERSISTENT_CONSUMPTION=NO",
    ]
    for key in (
        "MODEL_CHANGED",
        "PROMPT_CHANGED",
        "PERSONA_CHANGED",
        "PHRASE_MAP_CHANGED",
        "KEYWORD_ROUTER_CHANGED",
        "CUSTOMER_REGEX_CHANGED",
    ):
        lines.append(f"{key}={result.flags.get(key, 'NO')}")
    if not result.findings:
        lines.append("FINDINGS=none")
        return "\n".join(lines) + "\n"
    lines.append("FINDINGS:")
    for finding in result.findings:
        lines.append(f"FILE={finding.file}")
        lines.append(f"LINE={finding.line}")
        lines.append(f"CHANGE_CLASS={finding.change_class}")
        lines.append(f"REASON={finding.reason}")
        lines.append(f"SYMBOL={finding.symbol or ''}")
        lines.append(f"CHANGE_DIGEST={finding.change_digest or ''}")
        lines.append(f"AUTHORIZED_EXCEPTION_ID={finding.authorized_exception_id or ''}")
        if finding.diff_hunk:
            lines.append(f"DIFF_HUNK={finding.diff_hunk}")
        lines.append("---")
    return "\n".join(lines) + "\n"


def classify_audit_finding(finding: Finding, *, commit: str, files: Sequence[str]) -> str:
    if finding.authorized_exception_id:
        return "AUTHORIZED_HISTORICAL_EXCEPTION"
    if (
        commit.startswith(_HISTORICAL_PROMPT_EXCEPTION_COMMIT[:12])
        and finding.change_class == "PROMPT_CHANGE"
        and finding.file == _HISTORICAL_PROMPT_EXCEPTION_FILE
    ):
        return "AUTHORIZED_HISTORICAL_EXCEPTION"
    if finding.change_class in {
        "CUSTOMER_REGEX_CHANGE",
        "PHRASE_MAP_CHANGE",
        "KEYWORD_ROUTER_CHANGE",
        "TENANT_SPECIFIC_SEMANTIC_CHANGE",
        "PHONE_SPECIFIC_SEMANTIC_CHANGE",
        "PRODUCT_SPECIFIC_SEMANTIC_CHANGE",
        "CANNED_REPLY_CHANGE",
        "SAME_PR_SELF_WAIVER",
    }:
        return "PROBABLE_POLICY_VIOLATION"
    if finding.change_class in {"MODEL_CHANGE", "PROMPT_CHANGE", "PERSONA_CHANGE"}:
        return "NEEDS_OWNER_REVIEW"
    if finding.file.startswith("backend/modules/ai/brain/commerce/") or finding.file.startswith(
        "backend/modules/ai/brain/state/"
    ):
        return "STRUCTURAL_NON_VIOLATION"
    _ = files
    return "NEEDS_OWNER_REVIEW"


def audit_range(repo: str, start: str, end: str) -> List[dict]:
    code, out, err = _run_git(
        repo,
        ["rev-list", "--reverse", f"{start}..{end}"],
    )
    if code != 0:
        raise RuntimeError(err.strip() or "rev-list failed")
    shas = [line.strip() for line in out.splitlines() if line.strip()]
    rows: List[dict] = []
    for sha in shas:
        parent = git_rev_parse(repo, f"{sha}^")
        if not parent:
            continue
        result = scan_repository(repo, parent, sha, bootstrap=True, trusted_base_scanner=False)
        files = [p for _s, p in changed_paths(repo, parent, sha)]
        grouped = []
        for finding in result.findings:
            grouped.append(
                {
                    **finding.__dict__,
                    "bucket": classify_audit_finding(finding, commit=sha, files=files),
                }
            )
        if grouped:
            msg_code, msg, _ = _run_git(repo, ["log", "-1", "--format=%s", sha])
            rows.append(
                {
                    "commit": sha,
                    "subject": msg.strip() if msg_code == 0 else "",
                    "findings": grouped,
                }
            )
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GOV-002 intelligence non-interference guard")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--trusted-base-scanner", action="store_true")
    parser.add_argument("--audit-from", default="")
    parser.add_argument("--audit-to", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    repo = os.path.abspath(args.repo)
    if args.audit_from:
        audit_to = args.audit_to or "HEAD"
        try:
            rows = audit_range(repo, args.audit_from, audit_to)
        except RuntimeError as exc:
            sys.stderr.write(f"BASE_NOT_AVAILABLE: {exc}\n")
            return 1
        sys.stdout.write(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
        return 0

    base = args.base.strip()
    head = args.head.strip()
    if not base or not head:
        sys.stderr.write("BASE_NOT_AVAILABLE: --base and --head are required\n")
        sys.stdout.write(
            format_report(
                ScanResult(
                    findings=[
                        Finding(
                            file=".",
                            line=1,
                            change_class="BASE_NOT_AVAILABLE",
                            reason="--base and --head are required",
                        )
                    ]
                )
            )
        )
        return 1

    result = scan_repository(
        repo,
        base,
        head,
        bootstrap=bool(args.bootstrap),
        trusted_base_scanner=bool(args.trusted_base_scanner),
    )
    sys.stdout.write(format_report(result))
    if result.unauthorized:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
