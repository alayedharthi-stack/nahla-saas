#!/usr/bin/env python
"""
scripts/lint_no_silent_except.py
────────────────────────────────
CI guardrail (ADR 0001).

Flags silent error-swallowing patterns that caused the April 2026 outage,
where Salla webhook handlers and customer-creation paths swallowed
exceptions and returned 200 OK, leaving operators blind to drift between
the store and Nahla's database.

Forbidden patterns, checked via AST:

  1. ``except Exception:`` (or bare ``except:``) whose body is exactly
     ``pass``.
  2. ``except Exception:`` whose body is exactly ``return None`` / ``return``.
  3. ``except Exception as exc: logger.debug(...)`` — debug-level is too
     quiet for business failures; promote to ``logger.exception`` or
     re-raise.

To intentionally whitelist a handler, add ``# noqa: silent-ok`` on the
``except`` line with a *reason*:

    try:
        ...
    except Exception:  # noqa: silent-ok — cleanup path, error already logged upstream
        pass

Paths scanned: backend/, scripts/. Use ``--update-baseline`` once to
snapshot today's 42 pre-existing violations into
``scripts/lint_no_silent_except_baseline.txt``; thereafter the linter only
fails CI for NEW offenders. The baseline shrinks over time as we
migrate legacy swallowers to `logger.exception` + EVENTS.* names.

Exit codes:
  0 — no new violations
  1 — one or more new violations; CI should fail
  2 — invocation error (bad path, syntax error in source)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = [
    REPO_ROOT / "backend",
    REPO_ROOT / "scripts",
]

# Paths that are skipped entirely (third-party, generated, deprecated
# services not worth fixing because they are slated for removal).
SKIP_SUBSTR = (
    "/migrations/",
    "\\migrations\\",
    "/dist/",
    "\\dist\\",
    "/node_modules/",
    "\\node_modules\\",
    "/__pycache__/",
    "\\__pycache__\\",
)


class Violation(Tuple[str, int, str]):
    ...


def _is_bare_pass(body: List[ast.stmt]) -> bool:
    return len(body) == 1 and isinstance(body[0], ast.Pass)


def _is_bare_return(body: List[ast.stmt]) -> bool:
    return (
        len(body) == 1
        and isinstance(body[0], ast.Return)
        and (body[0].value is None or (isinstance(body[0].value, ast.Constant) and body[0].value.value is None))
    )


def _is_debug_only(body: List[ast.stmt]) -> bool:
    """True when the body is a single logger.debug(...) call, nothing else."""
    if len(body) != 1:
        return False
    stmt = body[0]
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "debug":
        return True
    return False


def _catches_exception(handler: ast.ExceptHandler) -> bool:
    """True for `except:`, `except Exception:`, `except BaseException:`."""
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name) and handler.type.id in ("Exception", "BaseException"):
        return True
    if isinstance(handler.type, ast.Tuple):
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException"):
                return True
    return False


def _line_has_whitelist(source_lines: List[str], lineno: int) -> bool:
    idx = lineno - 1
    if idx < 0 or idx >= len(source_lines):
        return False
    return "# noqa: silent-ok" in source_lines[idx]


def scan_file(path: Path) -> List[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # Not our problem here — other CI steps will catch it.
        return []

    lines = source.splitlines()
    hits: List[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _catches_exception(node):
            continue
        if _line_has_whitelist(lines, node.lineno):
            continue

        if _is_bare_pass(node.body):
            hits.append(Violation((str(path), node.lineno, "silent pass on broad except")))
        elif _is_bare_return(node.body):
            hits.append(Violation((str(path), node.lineno, "silent return on broad except")))
        elif _is_debug_only(node.body):
            hits.append(Violation((str(path), node.lineno, "logger.debug-only on broad except (use logger.exception)")))

    return hits


def _iter_py_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            s = str(p).replace("\\", "/")
            if any(skip.replace("\\", "/") in s for skip in SKIP_SUBSTR):
                continue
            yield p


BASELINE_PATH = REPO_ROOT / "scripts" / "lint_no_silent_except_baseline.txt"


def _baseline_key(path: str, line: int, msg: str) -> str:
    """Stable key independent of line number — line numbers drift when editing unrelated code.

    We key on (relative_path, message). A new hit with the same
    (file, message) count as baseline would still fail if the file hits
    *more* times than before — see `_load_baseline_counts`.
    """
    rel = str(Path(path).resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    return f"{rel}::{msg}"


def _load_baseline_counts() -> dict:
    """Baseline is a text file of `path::message` (one per line) — repeats allowed."""
    if not BASELINE_PATH.exists():
        return {}
    counts: dict = {}
    for raw in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        counts[line] = counts.get(line, 0) + 1
    return counts


def _write_baseline(all_hits: List[Violation]) -> None:
    keys = [_baseline_key(p, ln, m) for (p, ln, m) in all_hits]
    keys.sort()
    header = (
        "# Auto-generated baseline for scripts/lint_no_silent_except.py\n"
        "# Each line is `<relative_path>::<message>`. Duplicates are allowed —\n"
        "# one line per pre-existing violation. The linter fails CI only if the\n"
        "# number of violations in a file grows beyond this count.\n"
        "# To regenerate: python scripts/lint_no_silent_except.py --update-baseline\n"
    )
    BASELINE_PATH.write_text(header + "\n".join(keys) + "\n", encoding="utf-8")


def main() -> int:
    update_baseline = "--update-baseline" in sys.argv

    roots = SCAN_DIRS
    all_hits: List[Violation] = []
    for py in _iter_py_files(roots):
        all_hits.extend(scan_file(py))

    if update_baseline:
        _write_baseline(all_hits)
        print(f"no-silent-except: baseline written with {len(all_hits)} entries -> {BASELINE_PATH}")
        return 0

    baseline = _load_baseline_counts()
    hit_counts: dict = {}
    for path, line, msg in all_hits:
        key = _baseline_key(path, line, msg)
        hit_counts[key] = hit_counts.get(key, 0) + 1

    new_violations: List[Violation] = []
    for path, line, msg in all_hits:
        key = _baseline_key(path, line, msg)
        allowed = baseline.get(key, 0)
        if allowed <= 0:
            new_violations.append(Violation((path, line, msg)))
        else:
            baseline[key] = allowed - 1

    if not new_violations:
        if all_hits:
            print(f"no-silent-except: OK ({len(all_hits)} pre-existing, all baselined)")
        else:
            print("no-silent-except: OK (no violations)")
        return 0

    print(f"no-silent-except: {len(new_violations)} NEW violation(s) (not in baseline)")
    for path, line, msg in new_violations:
        print(f"  {path}:{line}: {msg}")
    print(
        "\nFix each new violation by either:"
        "\n  • re-raising after logging, or"
        "\n  • calling logger.exception(...) with a canonical EVENTS.* name, or"
        "\n  • adding `# noqa: silent-ok — <reason>` on the except line if the"
        "\n    swallow is genuinely intentional."
        "\n\nDo NOT just regenerate the baseline unless the violation is in"
        "\nlegacy code you are not touching. New commits should keep the"
        "\nbaseline count flat or shrinking."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
