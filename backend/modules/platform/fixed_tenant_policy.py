"""Fixed acceptance-tenant static policy gate.

Tenant 33 is the real-channel acceptance merchant. It must appear only in
explicit acceptance/tests/manifests — never as a production runtime default
or ops-script implicit default.

Platform tenant 1 (``PLATFORM_TENANT_ID``) is tracked separately; see
``PLATFORM_TENANT_LITERAL_REGISTRY`` for the narrow allowlist pending
auth-architecture migration.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[3]

ACCEPTANCE_TENANT_ID = 33
PLATFORM_TENANT_ID = 1

ACCEPTANCE_TENANT_ALLOWED_PATH_PREFIXES: Tuple[str, ...] = (
    "backend/tests/",
    "database/migrations/",
    "database/alembic/",
    "docs/engineering/real-channel-acceptance-scenario-manifest.json",
    "docs/engineering/real-channel-conversational-acceptance-runbook.md",
    "docs/engineering/tenant-merchant-clone-runbook.md",
    "docs/engineering/staging-acceptance-config-consolidation-runbook.md",
    "scripts/operators/real_channel_conversational_acceptance",
    "scripts/operators/real_channel_acceptance_session",
    "scripts/operators/tenant_merchant_clone",
    "scripts/probe_d360_forwarding.py",
    "scripts/merchant_assistant_constitution_smoke.py",
    "scripts/test_",
    "artifacts/tenant33-clone-manifest.json",
    "_tenant33_snapshot.json",
    "backend/modules/platform/fixed_tenant_policy.py",
    "backend/tests/test_fixed_tenant_policy.py",
)

PRODUCTION_RUNTIME_PREFIXES: Tuple[str, ...] = (
    "backend/core/",
    "backend/modules/",
    "backend/routers/",
    "backend/services/",
    "backend/main.py",
)

OPS_SCRIPT_PREFIXES: Tuple[str, ...] = (
    "backend/scripts/",
    "scripts/",
)

# Narrow registry: platform tenant 1 literals pending separate auth migration.
PLATFORM_TENANT_LITERAL_REGISTRY: Tuple[Tuple[str, str], ...] = (
    ("backend/routers/admin.py", "get_or_create_settings(db, 1)"),
    ("backend/routers/admin.py", "_platform_feature_flags(get_or_create_settings(db, 1))"),
    ("backend/routers/whatsapp_webhook.py", "PLATFORM_TENANT"),
)

_SUBCODE_NAME_RE = re.compile(r"subcode", re.I)
_TENANT33_SQL_RE = re.compile(
    r"tenant_id\s*=\s*33\b|tenant_id\s*:\s*33\b",
    re.I,
)
_OPS_DEFAULT_RE = re.compile(r"default\s*=\s*33\b")


class ViolationSeverity(str, Enum):
    FAIL_PRODUCTION = "fail_production_runtime"
    FAIL_OPS_DEFAULT = "fail_ops_implicit_default"
    FAIL_OPS_LITERAL = "fail_ops_hardcoded_tenant"


@dataclass(frozen=True)
class FixedTenantViolation:
    path: str
    line: int
    column: int
    severity: ViolationSeverity
    kind: str
    detail: str

    def format(self) -> str:
        return (
            f"{self.path}:{self.line}:{self.column} "
            f"[{self.severity.value}] {self.kind}: {self.detail}"
        )


def _norm_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def is_acceptance_tenant_allowed_path(rel: str) -> bool:
    return any(rel.startswith(prefix) or rel == prefix for prefix in ACCEPTANCE_TENANT_ALLOWED_PATH_PREFIXES)


def is_production_runtime_path(rel: str) -> bool:
    return any(rel.startswith(prefix) or rel == prefix for prefix in PRODUCTION_RUNTIME_PREFIXES)


def is_ops_script_path(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in OPS_SCRIPT_PREFIXES)


def _platform_tenant_line_allowed(rel: str, source: str, lineno: int) -> bool:
    if lineno <= 0 or lineno > len(source.splitlines()):
        return False
    line = source.splitlines()[lineno - 1]
    if str(PLATFORM_TENANT_ID) not in line:
        return False
    return any(rel == reg_path and marker in line for reg_path, marker in PLATFORM_TENANT_LITERAL_REGISTRY)


def _build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _docstring_lines(tree: ast.AST) -> Set[int]:
    lines: Set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        start = int(getattr(value, "lineno", getattr(first, "lineno", 0)) or 0)
        end = int(getattr(value, "end_lineno", start + value.value.count("\n")) or start)
        if start > 0:
            lines.update(range(start, end + 1))
    return lines


def _is_subcode_literal(node: ast.Constant, parents: Dict[ast.AST, ast.AST]) -> bool:
    cur: Optional[ast.AST] = node
    for _ in range(8):
        if cur is None:
            break
        if isinstance(cur, ast.Compare):
            names: List[str] = []
            left = cur.left
            if isinstance(left, ast.Name):
                names.append(left.id)
            elif isinstance(left, ast.Attribute):
                names.append(left.attr)
            if any(_SUBCODE_NAME_RE.search(n) for n in names):
                return True
        if isinstance(cur, ast.keyword) and cur.arg and _SUBCODE_NAME_RE.search(cur.arg):
            return True
        if isinstance(cur, ast.Assign):
            for target in cur.targets:
                if isinstance(target, ast.Name) and _SUBCODE_NAME_RE.search(target.id):
                    return True
                if isinstance(target, ast.Constant) and target.value == "subcode":
                    return True
        cur = parents.get(cur)
    return False


def _line_text(source: str, lineno: int) -> str:
    lines = source.splitlines()
    if lineno < 1 or lineno > len(lines):
        return ""
    return lines[lineno - 1]


def _classify_severity(rel: str, line: str, kind: str) -> Optional[ViolationSeverity]:
    if is_ops_script_path(rel):
        if _OPS_DEFAULT_RE.search(line):
            return ViolationSeverity.FAIL_OPS_DEFAULT
        return ViolationSeverity.FAIL_OPS_LITERAL
    if is_production_runtime_path(rel):
        return ViolationSeverity.FAIL_PRODUCTION
    return None


def scan_python_file(path: Path) -> List[FixedTenantViolation]:
    rel = _norm_path(path)
    if is_acceptance_tenant_allowed_path(rel):
        return []
    if not (is_production_runtime_path(rel) or is_ops_script_path(rel)):
        return []

    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []

    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []

    parents = _build_parent_map(tree)
    doc_lines = _docstring_lines(tree)
    violations: List[FixedTenantViolation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        lineno = int(getattr(node, "lineno", 0) or 0)
        col = int(getattr(node, "col_offset", 0) or 0)
        if lineno in doc_lines:
            continue
        if _platform_tenant_line_allowed(rel, source, lineno) and node.value == PLATFORM_TENANT_ID:
            continue

        kind: Optional[str] = None
        if node.value == ACCEPTANCE_TENANT_ID:
            if _is_subcode_literal(node, parents):
                continue
            line = _line_text(source, lineno)
            if _SUBCODE_NAME_RE.search(line):
                continue
            kind = "numeric_literal"
        elif isinstance(node.value, str) and _TENANT33_SQL_RE.search(node.value):
            kind = "sql_string_literal"
        else:
            continue

        line = _line_text(source, lineno)
        severity = _classify_severity(rel, line, kind)
        if severity is None:
            continue
        violations.append(
            FixedTenantViolation(
                path=rel,
                line=lineno,
                column=col,
                severity=severity,
                kind=kind,
                detail=f"acceptance tenant {ACCEPTANCE_TENANT_ID} must not appear in production/ops defaults",
            )
        )

    return violations


def iter_scanned_python_files() -> Iterator[Path]:
    seen: Set[str] = set()
    for prefix in (*PRODUCTION_RUNTIME_PREFIXES, *OPS_SCRIPT_PREFIXES):
        root = REPO_ROOT / prefix
        if root.is_file():
            rel = _norm_path(root)
            if rel not in seen:
                seen.add(rel)
                yield root
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = _norm_path(path)
            if rel in seen:
                continue
            seen.add(rel)
            yield path


def scan_fixed_tenant_violations(
    *,
    zone: Optional[str] = None,
) -> List[FixedTenantViolation]:
    """Scan repo for forbidden tenant-33 literals.

    ``zone``:
      - ``production`` — production runtime only
      - ``ops`` — operational scripts only
      - ``None`` — both zones
    """
    out: List[FixedTenantViolation] = []
    for path in iter_scanned_python_files():
        rel = _norm_path(path)
        if zone == "production" and not is_production_runtime_path(rel):
            continue
        if zone == "ops" and not is_ops_script_path(rel):
            continue
        out.extend(scan_python_file(path))
    return sorted(out, key=lambda v: (v.path, v.line, v.column))


def format_violation_report(violations: Sequence[FixedTenantViolation]) -> str:
    if not violations:
        return "No fixed-tenant policy violations."
    return "\n".join(v.format() for v in violations)
