#!/usr/bin/env python3
"""
scripts/audit_alembic_topology_readonly.py
──────────────────────────────────────────
Read-only preflight for a multi-head Alembic topology.

Reads every row of ``alembic_version`` in one ``READ ONLY`` transaction and
compares the exact row multiset with the expected set given on the command
line. It prints **known repository revision ids only** — a row whose value
is not a revision the repository knows is reported as ``<unknown>`` and
counted, never echoed, so a corrupted field cannot leak a credential or
control text into logs. It never prints a DSN, a token, a config value or any
other row, and it creates, stamps, merges, deletes or migrates nothing.

Why exact rows: a malformed ``alembic_version`` table can carry a duplicate
or an unknown id while the *set* of ids still looks right. The check therefore
fails closed on anything but exactly the expected rows: a missing table, a
different row count, a duplicate, an id the repository does not know, or an
unexpected id. It never coerces, repairs or relabels.

Repository heads versus database rows: the repository graph has its own heads
(``alembic heads``), which are not what a database must record. A database
records one row per *applied* branch, which may be an interior revision of
the repository (for example ``0113`` recorded while ``0114`` exists) and
need not include every repository head (``0092`` is not recorded in the
observed topology). The report prints both and marks, per database row,
whether the repository knows it and whether it is a repository head.

Exit codes: 0 rows equal the expectation exactly, 1 any mismatch (including a
missing table), 2 invocation / connection error.

Usage
─────
  DATABASE_URL="postgresql://..." python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120
  python scripts/audit_alembic_topology_readonly.py --expect 0111,0113,0120 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Sequence

PASS, FAIL = "PASS", "FAIL"
UNKNOWN = "<unknown>"
_DATABASE_DIR = Path(__file__).resolve().parents[1] / "database"
_REVISION_ID = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def parse_expect(raw: str) -> List[str]:
    """Comma-separated revision ids -> sorted list; malformed ids or duplicates in the expectation are an error."""
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items:
        raise ValueError("--expect needs at least one revision id")
    bad = [x for x in items if not _REVISION_ID.match(x)]
    if bad:
        raise ValueError(f"--expect carries {len(bad)} malformed revision id(s)")
    if len(set(items)) != len(items):
        raise ValueError("--expect repeats a revision id")
    return sorted(items)


def repository_graph() -> Dict[str, Any]:
    """Known revisions and heads of the repository migration graph (no database)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # revision modules import helpers relative to database/ (as env.py arranges when alembic runs from there)
    for entry in (str(_DATABASE_DIR), str(_DATABASE_DIR.parent)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    cfg = Config(str(_DATABASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_DATABASE_DIR / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    return {"known": {r.revision for r in script.walk_revisions()}, "heads": set(script.get_heads())}


def evaluate(rows: Sequence[str] | None, expected: Sequence[str], known: set, heads: set) -> Dict[str, Any]:
    """Pure comparison of the exact row multiset against the expected set. ``rows`` is None when the table
    is absent. Only ids the repository knows appear in the report; anything else is ``<unknown>`` and counted."""
    expected_sorted = sorted(expected)
    report: Dict[str, Any] = {"expected": expected_sorted, "repository_heads": sorted(heads), "checks": []}
    if rows is None:
        report["rows"] = None
        report["checks"].append((FAIL, "alembic_version.table_missing"))
        report["verdict"] = FAIL
        return report
    known_rows = sorted(r for r in rows if r in known)
    unknown_count = len(rows) - len(known_rows)
    counts = Counter(known_rows)
    report["rows"] = known_rows + [UNKNOWN] * unknown_count
    report["row_count"] = len(rows)
    report["unknown_row_count"] = unknown_count
    report["row_detail"] = [{"revision": r, "known_to_repository": True, "is_repository_head": r in heads} for r in known_rows] + [
        {"revision": UNKNOWN, "known_to_repository": False, "is_repository_head": False}] * unknown_count
    dup = sorted(r for r, n in counts.items() if n > 1)
    missing = sorted(set(expected_sorted) - set(known_rows))
    unexpected = sorted(set(known_rows) - set(expected_sorted))
    if dup:
        report["checks"].append((FAIL, "alembic_version.duplicate_rows:" + ",".join(dup)))
    if unknown_count:
        report["checks"].append((FAIL, f"alembic_version.unknown_revision_count:{unknown_count}"))
    if missing:
        report["checks"].append((FAIL, "alembic_version.missing_expected:" + ",".join(missing)))
    if unexpected:
        report["checks"].append((FAIL, "alembic_version.unexpected_row:" + ",".join(unexpected)))
    if sorted(rows) != expected_sorted:
        report["checks"].append((FAIL, "alembic_version.rows_differ_from_expectation"))
    if not report["checks"]:
        report["checks"].append((PASS, "alembic_version.rows_exactly_expected"))
    report["verdict"] = FAIL if any(c[0] == FAIL for c in report["checks"]) else PASS
    return report


def read_rows(url: str) -> List[str] | None:
    """All rows of alembic_version (with duplicates), or None if the table does not exist. Read only."""
    import psycopg2

    # accept the SQLAlchemy driver spelling used by the repository's tooling; the DSN is never printed
    if url.startswith("postgresql+psycopg2://"):
        url = "postgresql://" + url[len("postgresql+psycopg2://"):]
    conn = psycopg2.connect(url)
    try:
        conn.set_session(readonly=True, autocommit=False)
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('alembic_version') IS NOT NULL")
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT version_num FROM alembic_version")
        return [str(row[0]) for row in cur.fetchall()]
    finally:
        conn.rollback()
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--expect", required=True, help="comma-separated exact revision ids the database must record")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    try:
        expected = parse_expect(args.expect)
    except ValueError as exc:
        print(f"invocation error: {exc}", file=sys.stderr)
        return 2
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        print("invocation error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        graph = repository_graph()
        rows = read_rows(url)
    except ImportError as exc:  # a missing module is safe to name; never the DSN
        print(f"repository error: {type(exc).__name__}: {exc.name}", file=sys.stderr)
        return 2
    except Exception as exc:  # connection / graph errors: never print the DSN
        print(f"connection or repository error: {type(exc).__name__}", file=sys.stderr)
        return 2
    report = evaluate(rows, expected, graph["known"], graph["heads"])
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(f"alembic_version rows: {report['rows']}")
        print(f"expected:             {report['expected']}")
        print(f"repository heads:     {report['repository_heads']} (not what the database must record)")
        for level, name in report["checks"]:
            print(f"{level} {name}")
        print(f"VERDICT {report['verdict']}")
    return 0 if report["verdict"] == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
