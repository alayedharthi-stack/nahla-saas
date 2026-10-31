#!/usr/bin/env python3
"""One bounded 0124 -> 0125 pre-deploy action for S4_COMM_DORMANT.

The normal byte-pinned bootstrap enters this module. All normal gate source,
target, approval and flag checks run before a database write. The existing
read-only 0124 gate checks the old schema. A retry on exact 0125 never runs
Alembic again; it must pass the complete post-migration gate. A failed
post-migration check leaves 0125 in place and returns nonzero so Railway keeps
the old application serving. The operator must inspect the printed heads and
repair/retry with a fresh, explicit approval; this command never downgrades.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k

BEFORE = frozenset({"0111", "0113", "0124"})
AFTER = frozenset({"0111", "0113", "0125"})


def _stop(check: gate.Check) -> bool:
    gate.report(check)
    return not check.ok


def _source_preflight(args, env, now):
    """Reuse every existing source-side check, in the gate's order."""
    if args.phase != "S4_COMM_DORMANT" or not gate._valid_arguments(args):
        gate.emit("RESULT=FAIL reason=invalid_arguments")
        return None
    root = Path(args.root)
    expected_db = k.parse_db_target(args.expect_db)
    if _stop(gate.check_root(root)) or _stop(gate.check_release_sha(env, args.release_sha)):
        return None
    inventory_check, runtime = gate.check_inventory(root, args.inventory_count, args.inventory_digest)
    if (_stop(inventory_check) or _stop(gate.check_libraries(root))
            or _stop(gate.preflight_migration_files(root))):
        return None
    graph_check, known = gate.check_graph(root, k.phase(args.phase))
    if _stop(graph_check):
        return None
    status, reason, detail = k.evaluate_identity(
        env, {"project": args.expect_project, "environment": args.expect_environment,
              "service": args.expect_service})
    if _stop(gate.Check("identity", status, reason, detail)):
        return None
    target_check, bound_url = gate.check_database_target(env, expected_db)
    if (_stop(target_check) or _stop(gate.check_approval(env, args, runtime, expected_db, now))):
        return None
    status, reason, detail = k.evaluate_flags(env, args.phase)
    if _stop(gate.Check("shopify_flags", status, reason, detail)):
        return None
    if _stop(gate.check_expectations(root, args.phase)[0]):
        return None
    prior_check, prior_expectations = gate.check_expectations(root, "S4_DORMANT")
    if _stop(prior_check):
        return None
    return root, expected_db, bound_url, known, prior_expectations


def _safe_heads(rows):
    """Never print unexpected database values."""
    if rows is not None and len(rows) == 3 and set(rows) in (BEFORE, AFTER):
        return ",".join(sorted(rows))
    return "<unexpected>"


def _migrate_under_lock(root: Path, bound_url: str) -> bool:
    """Recheck exact heads under the legacy advisory lock before Alembic."""
    engine = gate._engine(bound_url)
    conn = None
    locked = False
    outcome = False
    try:
        conn = engine.connect().execution_options(postgresql_readonly=True)
        locked = gate._scalar(conn, "SELECT pg_try_advisory_lock(:key)", key=k.ADVISORY_LOCK_KEY) is True
        if not locked:
            gate.emit("MIGRATION FAIL reason=lock_denied")
            return False
        heads = gate.read_version_rows(conn)
        gate.emit("MIGRATION heads_before=" + _safe_heads(heads))
        if heads is None or len(heads) != 3 or set(heads) != BEFORE:
            gate.emit("MIGRATION FAIL reason=heads_changed_before_write")
            return False
        conn.rollback()
        # The validated explicit URL is the only connection target for Alembic.
        # This is the sole write operation in the command.
        from alembic import command
        from alembic.config import Config

        original_cwd = Path.cwd()
        try:
            os.chdir(root / "database")
            cfg = Config(str(root / "database/alembic.ini"))
            cfg.set_main_option("script_location", str(root / "database/migrations"))
            command.upgrade(cfg, "0125")
        finally:
            os.chdir(original_cwd)
        conn.rollback()
        after = gate.read_version_rows(conn)
        gate.emit("MIGRATION heads_after=" + _safe_heads(after))
        outcome = after is not None and len(after) == 3 and set(after) == AFTER
        if not outcome:
            gate.emit("MIGRATION FAIL reason=post_heads_mismatch")
    except BaseException as exc:  # only a type, never exception text/SQL/URL
        gate.emit("MIGRATION FAIL reason=exception type=" + type(exc).__name__)
        if conn is not None:
            try:
                conn.rollback()
                gate.emit("MIGRATION heads_after=" + _safe_heads(gate.read_version_rows(conn)))
            except BaseException:
                gate.emit("MIGRATION heads_after=<unavailable>")
    finally:
        if conn is not None:
            try:
                conn.rollback()
                if locked and gate._scalar(conn, "SELECT pg_advisory_unlock(:key)", key=k.ADVISORY_LOCK_KEY) is not True:
                    outcome = False
                    gate.emit("MIGRATION FAIL reason=unlock_failed")
            except BaseException:
                outcome = False
                gate.emit("MIGRATION FAIL reason=unlock_failed")
            finally:
                conn.close()
        engine.dispose()
    return outcome


def main(argv=None, environ=None, now=None) -> int:
    env = dict(os.environ if environ is None else environ)
    try:
        args = gate.build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 0)
    checked = _source_preflight(args, env, now or dt.datetime.now(dt.timezone.utc))
    if checked is None:
        return k.EXIT_FAIL
    root, expected_db, bound_url, known, prior_expectations = checked
    if environ is None:
        os.environ.clear()
        os.environ.update(gate.sanitized_environment(bound_url, env))
    # This read never writes and prints only one of two allow-listed head sets.
    engine = gate._engine(bound_url)
    try:
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            heads = gate.read_version_rows(conn)
            conn.rollback()
    except BaseException as exc:
        gate.emit("RESULT=FAIL reason=heads_read_failed type=" + type(exc).__name__)
        return k.EXIT_FAIL
    finally:
        engine.dispose()
    gate.emit("MIGRATION heads_before=" + _safe_heads(heads))
    if heads is None or len(heads) != 3:
        gate.emit("RESULT=FAIL reason=unexpected_heads")
        return k.EXIT_FAIL
    if set(heads) == AFTER:
        gate.emit("MIGRATION SKIP reason=already_0125")
    elif set(heads) == BEFORE:
        checks = gate.run_database_checks(bound_url, k.phase("S4_DORMANT"), known,
                                          prior_expectations, expected_db["database"])
        for check in checks:
            gate.report(check)
        required = {"connect", "lock", "server", "version_rows", "shopify_footprint",
                    "retained_schema", "relation_identity", "unlock"}
        if any(not check.ok for check in checks) or not required <= {check.name for check in checks}:
            gate.emit("RESULT=FAIL reason=prior_schema_preflight")
            return k.EXIT_FAIL
        if not _migrate_under_lock(root, bound_url):
            gate.emit("RESULT=FAIL reason=migration_incomplete")
            return k.EXIT_FAIL
    else:
        gate.emit("RESULT=FAIL reason=unexpected_heads")
        return k.EXIT_FAIL
    # The unchanged full gate verifies approval again (including expiry), exact
    # 0125 heads, full schema, relation identity, target and all flags.
    return gate.main(argv, os.environ if environ is None else env, now)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as exc:
        gate.emit("RESULT=FAIL reason=internal_error type=" + type(exc).__name__)
        sys.exit(k.EXIT_FAIL)

