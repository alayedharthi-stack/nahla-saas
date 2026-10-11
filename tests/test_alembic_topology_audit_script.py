"""DB-free contract of scripts/audit_alembic_topology_readonly.py: exact rows, fail closed, read-only, secret-safe.

The script's comparison is a pure function over the rows it read; these cases
pin that it accepts exactly the expected multiset and nothing else (count,
duplicates, unknown ids, missing or extra rows, absent table), that the
expectation itself must be well-formed and cannot repeat an id, that a row the
repository does not know is counted but never echoed (so a corrupted field
cannot leak a credential or control text), and that the module never imports
a migration executor. The PostgreSQL exercise of the same script lives in the
Shopify connection chain proof.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "audit_alembic_topology_readonly.py"

spec = importlib.util.spec_from_file_location("audit_alembic_topology_readonly", _SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

KNOWN = {"0093", "0111", "0113", "0114", "0115", "0118", "0120", "0121", "0122", "0123"}
HEADS = {"0092", "0111", "0114", "0115", "0123"}
S0 = ["0111", "0113", "0120"]
SECRET_ROW = "postgresql://svc:p-not-a-real-secret@db.internal:5432/prod"
CONTROL_ROW = "\x1b[2J\x1b[31mDROP TABLE tenants; --"


def _verdict(rows, expected=S0):
    return mod.evaluate(rows, expected, KNOWN, HEADS)


def test_exact_rows_pass_and_order_does_not_matter():
    for rows in (["0111", "0113", "0120"], ["0120", "0111", "0113"]):
        report = _verdict(rows)
        assert report["verdict"] == mod.PASS and report["row_count"] == 3 and report["unknown_row_count"] == 0
        assert [c for c in report["checks"] if c[0] == mod.FAIL] == []
    # database rows are interior revisions / applied branches, not the repository heads
    report = _verdict(S0)
    assert report["repository_heads"] == sorted(HEADS)
    assert [d["is_repository_head"] for d in report["row_detail"]] == [True, False, False]
    assert all(d["known_to_repository"] for d in report["row_detail"])


@pytest.mark.parametrize(
    "rows, failing",
    [
        (["0111", "0113"], "alembic_version.missing_expected:0120"),
        (["0111", "0113", "0120", "0120"], "alembic_version.duplicate_rows:0120"),
        (["0111", "0113", "0120", "0121"], "alembic_version.unexpected_row:0121"),
        (["0111", "0113", "0121"], "alembic_version.missing_expected:0120"),
        (["0111", "0113", "0120", "9999"], "alembic_version.unknown_revision_count:1"),
        (["0093"], "alembic_version.missing_expected:0111,0113,0120"),
        ([], "alembic_version.missing_expected:0111,0113,0120"),
    ],
)
def test_any_deviation_fails_closed(rows, failing):
    report = _verdict(rows)
    assert report["verdict"] == mod.FAIL
    assert (mod.FAIL, failing) in report["checks"]
    assert (mod.FAIL, "alembic_version.rows_differ_from_expectation") in report["checks"]
    assert report["row_count"] == len(rows)


def test_missing_table_fails_closed_without_rows():
    report = _verdict(None)
    assert report["verdict"] == mod.FAIL and report["rows"] is None
    assert (mod.FAIL, "alembic_version.table_missing") in report["checks"]


def test_unknown_rows_are_counted_never_echoed():
    rows = ["0111", "0113", "0120", SECRET_ROW, CONTROL_ROW, SECRET_ROW]
    report = _verdict(rows)
    dumped = json.dumps(report)
    assert report["verdict"] == mod.FAIL
    assert report["row_count"] == 6 and report["unknown_row_count"] == 3
    assert report["rows"] == ["0111", "0113", "0120", mod.UNKNOWN, mod.UNKNOWN, mod.UNKNOWN]
    assert (mod.FAIL, "alembic_version.unknown_revision_count:3") in report["checks"]
    assert (mod.FAIL, "alembic_version.rows_differ_from_expectation") in report["checks"]
    for leak in ("p-not-a-real-secret", "db.internal", "DROP TABLE", "\x1b", SECRET_ROW, CONTROL_ROW):
        assert leak not in dumped
    # a repeated unknown value is still never named, only counted
    assert not any("duplicate" in c[1] for c in report["checks"])


def test_cli_text_and_json_output_never_echo_unknown_rows(monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p-not-a-real-secret@127.0.0.1:1/none")
    monkeypatch.setattr(mod, "repository_graph", lambda: {"known": KNOWN, "heads": HEADS})
    monkeypatch.setattr(mod, "read_rows", lambda url: ["0111", "0113", "0120", SECRET_ROW, CONTROL_ROW])
    assert mod.main(["--expect", "0111,0113,0120"]) == 1
    text_out = capsys.readouterr()
    assert mod.main(["--expect", "0111,0113,0120", "--json"]) == 1
    json_out = capsys.readouterr()
    for out in (text_out.out + text_out.err, json_out.out + json_out.err):
        assert "unknown_revision_count:2" in out and mod.UNKNOWN in out
        for leak in ("p-not-a-real-secret", "db.internal", "DROP TABLE", "\x1b", "127.0.0.1:1/none"):
            assert leak not in out
    monkeypatch.setattr(mod, "read_rows", lambda url: ["0111", "0113", "0120"])
    assert mod.main(["--expect", "0111,0113,0120"]) == 0
    assert "VERDICT PASS" in capsys.readouterr().out


def test_expectation_is_a_set_of_well_formed_ids_and_never_repeats():
    assert mod.parse_expect("0120,0113,0111") == ["0111", "0113", "0120"]
    assert mod.parse_expect(" 0111 , 0113 ,0123 ") == ["0111", "0113", "0123"]
    with pytest.raises(ValueError):
        mod.parse_expect("0111,0111,0120")
    with pytest.raises(ValueError):
        mod.parse_expect(" , ")
    with pytest.raises(ValueError):
        mod.parse_expect("0111,postgresql://x")


def test_cli_refuses_without_dsn_and_never_echoes_one(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    proc = subprocess.run([sys.executable, str(_SCRIPT), "--expect", "0111,0113,0120"], capture_output=True, text=True,
                          cwd=str(_REPO), timeout=120)
    assert proc.returncode == 2 and "DATABASE_URL is not set" in proc.stderr
    proc = subprocess.run([sys.executable, str(_SCRIPT), "--expect", "0111,0111"], capture_output=True, text=True,
                          cwd=str(_REPO), timeout=120)
    assert proc.returncode == 2 and "repeats" in proc.stderr
    secret = "postgresql://u:p-not-a-real-secret@127.0.0.1:1/none"
    proc = subprocess.run([sys.executable, str(_SCRIPT), "--expect", "0111,0113,0120"], capture_output=True, text=True,
                          cwd=str(_REPO), timeout=120, env={"DATABASE_URL": secret, "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 2 and secret not in proc.stdout + proc.stderr and "p-not-a-real-secret" not in proc.stdout + proc.stderr


def test_module_is_read_only_and_runs_no_migration():
    src = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    for forbidden in ("upgrade", "downgrade", "stamp", "merge", "command", "execute_migration", "purge", "DELETE", "delete"):
        assert forbidden not in names, forbidden
    assert "readonly=True" in src and "rollback()" in src
    sql = [n.value.upper() for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert all(not any(w in text for w in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER ", "TRUNCATE")) for text in sql)
