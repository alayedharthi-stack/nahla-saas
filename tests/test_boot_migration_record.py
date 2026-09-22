"""What the boot record says about a migration, and about nothing else.

Two defects this pins down, both found while trying to answer "has 0112 been
applied to production?" from the evidence available:

* every Step C log line named ``0089`` while the bootstrap contract had long
  since moved the pinned target to ``0093``, so an operator reading the log to
  judge the applied revision was told one that had not been requested;
* no line recorded *which database* the upgrade ran against, so with two live
  PostgreSQL services in the project the destination could not be established
  from the record at all.

A migration record that names the wrong revision, or no destination, is worse
than none: it invites a confident wrong answer.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in [str(REPO_ROOT), str(REPO_ROOT / "backend"), str(REPO_ROOT / "database")]:
    if p not in sys.path:
        sys.path.insert(0, p)

MAIN = REPO_ROOT / "backend" / "main.py"


def _masked_db_target():
    """The helper, exercised without importing the whole application."""
    source = MAIN.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_masked_db_target":
            namespace: dict = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(MAIN), "exec"),
                 {"os": __import__("os")}, namespace)
            return namespace["_masked_db_target"]
    raise AssertionError("backend/main.py no longer defines _masked_db_target")


def test_the_boot_log_names_the_revision_it_actually_runs() -> None:
    """The revision is read off the command, never written beside it.

    The drift was possible only because the number was typed into the log
    string. Taking it from ``_bootstrap_upgrade_cmd`` makes the two incapable
    of disagreeing.
    """
    source = MAIN.read_text()
    assert "_bootstrap_target = _bootstrap_upgrade_cmd[-1]" in source
    step_c = source[source.index("Step C: Apply the normal application migration target"):]
    step_c = step_c[:step_c.index("async def _bootstrap_db_schema_bg")]
    for line in re.findall(r'"\[BOOT/db\] Step C:[^"]*"', step_c):
        assert "0089" not in line, f"a hard-coded revision is back in the log: {line}"
        assert "0093" not in line, f"a hard-coded revision is back in the log: {line}"


def test_the_boot_log_names_the_database_the_migration_ran_against() -> None:
    source = MAIN.read_text()
    step_c = source[source.index("Step C: Apply the normal application migration target"):]
    step_c = step_c[:step_c.index("async def _bootstrap_db_schema_bg")]
    assert step_c.count("_masked_db_target()") >= 2, (
        "both the attempt and the success line should say which database was used"
    )


def test_the_target_is_host_port_and_database_and_never_a_credential() -> None:
    """Host, port and database name are what make a migration record mean
    something. The credential half is discarded before anything is formatted,
    so there is no path by which it reaches a log."""
    masked = _masked_db_target()
    secret = "sup3rs3cret-do-not-log"
    out = masked(f"postgresql://nahla:{secret}@postgres.railway.internal:5432/railway")
    assert out == "postgres.railway.internal:5432/railway"
    assert secret not in out and "nahla:" not in out

    other = masked("postgresql://u:p@nahla-postgres-prod.railway.internal:5432/nahla")
    assert other == "nahla-postgres-prod.railway.internal:5432/nahla"
    assert other != out, "the two candidate databases must be distinguishable in the record"


def test_an_absent_or_unreadable_url_says_so_rather_than_guessing() -> None:
    masked = _masked_db_target()
    assert masked("") in {"unset", "unparseable"} or masked("") == "unset"
    # A URL with no port still names host and database rather than inventing one.
    assert masked("postgresql://u:p@db.internal/nahla") == "db.internal/nahla"


def test_a_log_line_never_breaks_a_boot() -> None:
    """Whatever DATABASE_URL holds, the helper returns a string. A migration
    must not fail because its record could not be formatted."""
    masked = _masked_db_target()
    for hostile in ("", "://", "postgresql://", "not a url", "postgresql://u:p@host:notaport/db"):
        assert isinstance(masked(hostile), str)
