#!/usr/bin/env python3
"""Shopify release gate — read-only, phase-aware normal-deployment compatibility check.

Run by the rendered pre-deploy command (``shopify_release_gate_render.py``)
from the deployed image, after that command has verified this file's SHA-256
against the literal it carries::

    python -I /app/scripts/operators/shopify_release_gate.py --root /app --phase S0 \
        --release-sha <40 hex> --inventory-digest <64 hex> --inventory-count <n> \
        --expect-project <id> --expect-environment <id> --expect-service <id> \
        --expect-db host:port/database

It verifies, in order and fail-closed: the container root and its own
location; the deployed commit (``RAILWAY_GIT_COMMIT_SHA``) against the pinned
release; the byte inventory (count and digest) against the pinned values; the
library pins; the migration graph of the source it runs from (every expected
edge, the exact repository heads and the Shopify branch delta for the phase);
the platform identity; the database target through the existing pilot-job
validators (no query override, no inherited libpq variable, no loopback,
exact host, port and database); the owner's phase approval record bound to
the runtime inventory; that every Shopify flag is off. Only then does it open
one read-only connection with bounded timeouts, take an exclusive session
advisory lock (refusing when held), and check that the server is a writable
primary, the schema is ``public``, ``alembic_version`` holds exactly the
phase's rows, the Shopify footprint is exactly the phase's, the retained
relations still have their pinned canonical definitions, and that the lock is
released. It executes nothing that writes: no Alembic command, no subprocess,
no DDL, no DML.

Output is grep-able and secret-safe: closed reason tokens, known revision ids
only (``<unknown>`` otherwise), table and field names, expected platform ids.
Never the DSN, an environment value, a row value or an exception body.

Exit codes: 0 every check passed; 1 any check failed; 2 invocation error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.metadata
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

_OWN_FILE = Path(__file__).resolve()
_OWN_ROOT = _OWN_FILE.parents[2]
if str(_OWN_ROOT) not in sys.path:
    sys.path.insert(0, str(_OWN_ROOT))

from scripts.operators import shopify_release_gate_contract as k  # noqa: E402
from scripts.operators.commerce_runtime_pilot_migration import (  # noqa: E402
    explicit_url,
    parse_database_url,
    sanitized_environment,
)


class WallTimeout(Exception):
    """Raised by the alarm handler when the whole run exceeds its bound."""


class Check:
    def __init__(self, name: str, status: str, reason: Optional[str] = None, detail: Optional[Mapping[str, Any]] = None):
        self.name, self.status, self.reason, self.detail = name, status, reason, dict(detail or {})


def emit(message: str) -> None:
    print(f"{k.LOG_PREFIX} {message}", flush=True)


def report(check: Check) -> None:
    line = f"CHECK {check.name} {check.status}"
    if check.reason:
        line += f" reason={check.reason}"
    if check.detail:
        line += " " + json.dumps(check.detail, sort_keys=True, separators=(",", ":"), default=str)
    emit(line)


# ── Inventory ────────────────────────────────────────────────────────────────

def _database_py_paths(root: Path) -> List[str]:
    base = root / k.LEGACY_GLOB_ROOT
    found: List[str] = []
    for path in base.rglob(f"*{k.LEGACY_GLOB_SUFFIX}"):
        if path.is_file():
            found.append(path.relative_to(root).as_posix())
    return found


def inventory_paths(root: Path, *, full: bool) -> List[str]:
    """Sorted relative paths of the runtime inventory, or of the full inventory when ``full``."""
    paths = set(k.LEGACY_FIXED_FILES) | set(_database_py_paths(root)) | set(k.RUNTIME_EXTRA_FILES)
    if full:
        paths |= set(k.FULL_EXTRA_FILES)
    return sorted(paths)


def digest_paths(root: Path, paths: Sequence[str]) -> Tuple[int, str]:
    """``(count, sha256)`` over ``path NUL bytes NUL`` for the sorted paths. Missing file -> FileNotFoundError."""
    digest = hashlib.sha256()
    for relative in sorted(paths):
        with open(root / relative, "rb") as handle:
            data = handle.read()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return len(paths), digest.hexdigest()


def runtime_inventory(root: Path) -> Tuple[int, str]:
    return digest_paths(root, inventory_paths(root, full=False))


def full_inventory(root: Path) -> Tuple[int, str]:
    return digest_paths(root, inventory_paths(root, full=True))


def database_py_digest(root: Path) -> str:
    return digest_paths(root, sorted(_database_py_paths(root)))[1]


def file_sha256(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


# ── Source-side checks (no database) ─────────────────────────────────────────

def check_root(root: Path) -> Check:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return Check("root", k.FAIL, k.R_ROOT_MISSING)
    if _OWN_FILE != resolved / k.GATE_RELATIVE_PATH or not (resolved / "database").is_dir():
        return Check("root", k.FAIL, k.R_ROOT_MISSING)
    return Check("root", k.PASS, detail={"root": str(resolved)})


def check_release_sha(environ: Mapping[str, str], expected: str) -> Check:
    actual = str(environ.get(k.RELEASE_SHA_ENV) or "").strip().lower()
    if not actual:
        return Check("release_sha", k.FAIL, k.R_RELEASE_SHA_MISSING, {"expected": expected})
    if actual != expected:
        return Check("release_sha", k.FAIL, k.R_RELEASE_SHA_MISMATCH, {"expected": expected})
    return Check("release_sha", k.PASS, detail={"release": expected})


def check_inventory(root: Path, expected_count: int, expected_digest: str) -> Tuple[Check, Optional[Tuple[int, str]]]:
    try:
        runtime = runtime_inventory(root)
        count, digest = full_inventory(root)
    except FileNotFoundError:
        return Check("inventory", k.FAIL, k.R_INVENTORY_FILE_MISSING), None
    if count != expected_count:
        return Check("inventory", k.FAIL, k.R_INVENTORY_COUNT_MISMATCH, {"expected": expected_count, "observed": count}), None
    if digest != expected_digest:
        return Check("inventory", k.FAIL, k.R_INVENTORY_DIGEST_MISMATCH, {"expected": expected_digest}), None
    return Check("inventory", k.PASS, detail={"count": count, "digest": digest, "runtime_count": runtime[0], "runtime_digest": runtime[1]}), runtime


def check_libraries() -> Check:
    mismatched: List[str] = []
    for name, pinned in k.LIBRARY_PINS.items():
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed = None
        if installed != pinned:
            mismatched.append(name)
    if mismatched:
        return Check("libraries", k.FAIL, k.R_LIBRARY_PIN_MISMATCH, {"mismatched": sorted(mismatched)})
    return Check("libraries", k.PASS, detail={"pins": dict(k.LIBRARY_PINS)})


def load_graph(root: Path) -> Dict[str, Any]:
    """Known revisions, down-revisions and heads of the script directory under ``root`` (no database)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    database_dir = root / "database"
    for entry in (str(database_dir), str(root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    config = Config(str(database_dir / "alembic.ini"))
    config.set_main_option("script_location", str(database_dir / "migrations"))
    script = ScriptDirectory.from_config(config)
    down: Dict[str, Tuple[str, ...]] = {}
    for revision in script.walk_revisions():
        parents = revision.down_revision
        if parents is None:
            down[revision.revision] = ()
        elif isinstance(parents, str):
            down[revision.revision] = (parents,)
        else:
            down[revision.revision] = tuple(parents)
    return {"known": frozenset(down), "down": down, "heads": frozenset(script.get_heads())}


def branch_chain(down: Mapping[str, Tuple[str, ...]], start: str, stop: str) -> Optional[Tuple[str, ...]]:
    """Revisions from ``start`` down to (excluding) ``stop`` along single parents; None if the walk does not reach it."""
    chain: List[str] = []
    current = start
    for _ in range(len(down) + 1):
        if current == stop:
            return tuple(reversed(chain))
        parents = down.get(current)
        if not parents or len(parents) != 1:
            return None
        chain.append(current)
        current = parents[0]
    return None


def check_graph(root: Path, phase_def: Mapping[str, Any]) -> Tuple[Check, Optional[FrozenSet[str]]]:
    try:
        graph = load_graph(root)
    except Exception as exc:  # noqa: BLE001 - the type only; a graph that does not load is a refusal
        return Check("graph", k.FAIL, k.R_GRAPH_UNLOADABLE, {"error": type(exc).__name__}), None
    down = graph["down"]
    bad_edges = sorted(rev for rev, parent in k.EXPECTED_DOWN_REVISIONS.items() if down.get(rev) != (parent,))
    if bad_edges:
        return Check("graph", k.FAIL, k.R_GRAPH_EDGE_MISMATCH, {"revisions": bad_edges}), None
    if graph["heads"] != k.EXPECTED_REPOSITORY_HEADS:
        return Check("graph", k.FAIL, k.R_GRAPH_HEADS_MISMATCH, {"expected": sorted(k.EXPECTED_REPOSITORY_HEADS)}), None
    delta = branch_chain(down, phase_def["shopify_row"], k.BRANCH_FORK_REVISION)
    if delta != tuple(phase_def["branch_delta"]):
        return Check("graph", k.FAIL, k.R_GRAPH_BRANCH_DELTA_MISMATCH, {"expected": list(phase_def["branch_delta"])}), None
    # the retained rows are on distinct branches and neither is an ancestor of the other
    for row in sorted(k.RETAINED_ROWS):
        others = k.RETAINED_ROWS - {row}
        ancestors = set()
        frontier = [row]
        while frontier:
            current = frontier.pop()
            for parent in down.get(current, ()):
                if parent not in ancestors:
                    ancestors.add(parent)
                    frontier.append(parent)
        if ancestors & others or phase_def["shopify_row"] in ancestors or row in set(delta or ()):
            return Check("graph", k.FAIL, k.R_GRAPH_BRANCH_DELTA_MISMATCH, {"row": row}), None
    return Check("graph", k.PASS, detail={"heads": sorted(graph["heads"]), "branch_delta": list(delta or ())}), graph["known"]


def check_database_target(environ: Mapping[str, str], expected: Mapping[str, Any]) -> Tuple[Check, Optional[str]]:
    """The configured DATABASE_URL, validated by the pilot job's own parser, must be exactly the expected target."""
    url = str(environ.get("DATABASE_URL") or "").strip()
    target, refusal = parse_database_url(url, dict(environ))
    if target is None:
        return Check("database_target", k.FAIL, k.R_DB_TARGET_MISMATCH, {"refusal": refusal}), None
    if (target["host"], target["port"], target["database"]) != (expected["host"], expected["port"], expected["database"]):
        return Check("database_target", k.FAIL, k.R_DB_TARGET_MISMATCH, {"refusal": "effective_target_differs"}), None
    bound = explicit_url(url, target)
    if bound is None:
        return Check("database_target", k.FAIL, k.R_DB_TARGET_MISMATCH, {"refusal": "unbindable"}), None
    label = f"{expected['host']}:{expected['port']}/{expected['database']}"
    return Check("database_target", k.PASS, detail={"target": label}), bound


def read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def check_approval(root: Path, phase_name: str, runtime: Tuple[int, str], now: _dt.datetime) -> Check:
    record = read_json(root / k.APPROVAL_RELATIVE_PATH)
    if record is None:
        return Check("approval", k.FAIL, k.R_APPROVAL_UNREADABLE, {"phase": phase_name})
    status, reason, detail = k.evaluate_approval(record, phase_name, runtime_digest=runtime[1], runtime_count=runtime[0], now=now)
    return Check("approval", status, reason, detail)


def check_expectations(root: Path) -> Tuple[Check, Optional[Dict[str, Any]]]:
    expectations = read_json(root / k.EXPECTATIONS_RELATIVE_PATH)
    if not isinstance(expectations, dict) or expectations.get("expectations_version") != k.EXPECTATIONS_VERSION:
        return Check("expectations", k.FAIL, k.R_EXPECTATIONS_UNREADABLE), None
    try:
        current = database_py_digest(root)
    except FileNotFoundError:
        return Check("expectations", k.FAIL, k.R_EXPECTATIONS_UNREADABLE), None
    if expectations.get("database_py_digest") != current:
        return Check("expectations", k.FAIL, k.R_EXPECTATIONS_STALE, {"expected_for": str(expectations.get("generated_from_source_sha") or "")[:12]}), None
    return Check("expectations", k.PASS, detail={"generated_from_source_sha": expectations.get("generated_from_source_sha")}), expectations


# ── Database observation (read-only SQL only) ────────────────────────────────

_COLUMNS_SQL = (
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
    " pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated"
    " FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE a.attrelid = CAST(:tbl AS regclass) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum")
_CONSTRAINTS_SQL = (
    "SELECT conname, contype, pg_get_constraintdef(oid), condeferrable, condeferred, convalidated"
    " FROM pg_constraint WHERE conrelid = CAST(:tbl AS regclass) ORDER BY conname")
_INDEXES_SQL = (
    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = :tbl ORDER BY indexname")
_SEQUENCES_SQL = (
    "SELECT s.relname, t.relname, a.attname FROM pg_class s"
    " JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'"
    " JOIN pg_class t ON t.oid = d.refobjid"
    " JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid"
    " JOIN pg_namespace n ON n.oid = t.relnamespace"
    " WHERE s.relkind = 'S' AND n.nspname = 'public' AND t.relname = ANY(:tables) ORDER BY 1")
_PK_SQL = (
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_constraint c"
    " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)"
    " WHERE c.conrelid = CAST(:tbl AS regclass) AND c.contype = 'p' ORDER BY a.attnum")
_UNIQUE_SQL = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:tbl AS regclass) AND contype = 'u'")
_SHOPIFY_RELATIONS_SQL = (
    "SELECT c.relname, c.relkind, CASE WHEN c.relkind = 'i' THEN (SELECT t.relname FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid WHERE i.indexrelid = c.oid)"
    " WHEN c.relkind = 'S' THEN (SELECT t.relname FROM pg_depend d JOIN pg_class t ON t.oid = d.refobjid WHERE d.objid = c.oid AND d.deptype IN ('a', 'i') LIMIT 1)"
    " ELSE NULL END FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE n.nspname = 'public' AND c.relname LIKE :pattern ESCAPE '\\' ORDER BY c.relname")
_SHOPIFY_TYPES_SQL = (
    "SELECT t.typname, COALESCE(r.relname, (SELECT r2.relname FROM pg_type e JOIN pg_class r2 ON r2.oid = e.typrelid WHERE e.oid = t.typelem))"
    " FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace LEFT JOIN pg_class r ON r.oid = t.typrelid"
    " WHERE n.nspname = 'public' AND (t.typname LIKE :pattern ESCAPE '\\' OR t.typname LIKE :array_pattern ESCAPE '\\') ORDER BY t.typname")
_SHOPIFY_FUNCTIONS_SQL = (
    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE n.nspname = 'public' AND p.proname LIKE :pattern ESCAPE '\\' ORDER BY p.proname")
_SHOPIFY_CONSTRAINTS_SQL = (
    "SELECT c.conname, r.relname FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid JOIN pg_namespace n ON n.oid = r.relnamespace"
    " WHERE n.nspname = 'public' AND c.conname LIKE :contains ESCAPE '\\' ORDER BY c.conname")
_DB_ROLE_SETTINGS_SQL = (
    "SELECT unnest(setconfig) FROM pg_db_role_setting WHERE (setdatabase = 0 OR setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()))"
    " AND (setrole = 0 OR setrole = (SELECT oid FROM pg_roles WHERE rolname = current_user))")


def _quoted(table: str) -> str:
    return 'public."' + table.replace('"', '""') + '"'


def _rows(conn, sql: str, **params: Any) -> List[Tuple[Any, ...]]:
    from sqlalchemy import text

    return [tuple(row) for row in conn.execute(text(sql), params)]


def _scalar(conn, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    return conn.execute(text(sql), params).scalar()


def _table_exists(conn, table: str) -> bool:
    return _scalar(conn, "SELECT to_regclass(:name) IS NOT NULL", name=_quoted(table)) is True


def table_definition(conn, table: str) -> Dict[str, Any]:
    qualified = _quoted(table)
    return {
        "columns": _rows(conn, _COLUMNS_SQL, tbl=qualified),
        "constraints": _rows(conn, _CONSTRAINTS_SQL, tbl=qualified),
        "indexes": _rows(conn, _INDEXES_SQL, tbl=table),
    }


def _index_shape(unique: bool, indexdef: str) -> Tuple[bool, str]:
    marker = " USING "
    position = indexdef.find(marker)
    return (unique, indexdef[position + 1:] if position >= 0 else indexdef)


def nonce_definition(conn, table: str) -> Dict[str, Any]:
    qualified = _quoted(table)
    columns = [(name, type_, notnull) for name, type_, notnull, _d, _i, _g in _rows(conn, _COLUMNS_SQL, tbl=qualified)]
    constraints = sorted(definition for _n, _t, definition, _d1, _d2, _v in _rows(conn, _CONSTRAINTS_SQL, tbl=qualified))
    indexes = sorted(_index_shape(" UNIQUE INDEX " in definition, definition) for _n, definition in _rows(conn, _INDEXES_SQL, tbl=table))
    return {"columns": columns, "constraint_definitions": constraints, "index_shapes": indexes}


def observe_retained(conn) -> Dict[str, Any]:
    """Everything ``evaluate_retained`` compares, read from the live catalog."""
    observed: Dict[str, Any] = {"tables": {}, "nonce": None, "pk_types": {}, "unique_definitions": [], "sequences": []}
    for table in k.RETAINED_FULL_DEFINITION_TABLES:
        if _table_exists(conn, table):
            observed["tables"][table] = table_definition(conn, table)
    if _table_exists(conn, k.NONCE_TABLE):
        observed["nonce"] = nonce_definition(conn, k.NONCE_TABLE)
    for table in k.PK_TYPE_PINS:
        if _table_exists(conn, table):
            pk = _rows(conn, _PK_SQL, tbl=_quoted(table))
            if len(pk) == 1 and pk[0][0] == "id":
                observed["pk_types"][table] = pk[0][1]
    for table, definition in k.UNIQUE_DEFINITION_PINS:
        if _table_exists(conn, table) and any(row[0] == definition for row in _rows(conn, _UNIQUE_SQL, tbl=_quoted(table))):
            observed["unique_definitions"].append([table, definition])
    observed["sequences"] = [list(row) for row in _rows(conn, _SEQUENCES_SQL, tables=list(k.RETAINED_FULL_DEFINITION_TABLES))]
    return observed


def observe_footprint(conn, phase_def: Mapping[str, Any]) -> Dict[str, Any]:
    expected = set(phase_def["tables"])
    pattern = k.SHOPIFY_NAME_PREFIX.replace("_", "\\_") + "%"
    tables: List[str] = []
    other: List[str] = []
    for name, kind, owner in _rows(conn, _SHOPIFY_RELATIONS_SQL, pattern=pattern):
        if kind in ("r", "p"):
            tables.append(name)
        elif kind in ("S", "i") and owner in expected:
            continue
        else:
            other.append(name)
    types = [name for name, owner in _rows(conn, _SHOPIFY_TYPES_SQL, pattern=pattern, array_pattern="\\_" + pattern)
             if owner not in expected]
    functions = [name for (name,) in _rows(conn, _SHOPIFY_FUNCTIONS_SQL, pattern=pattern)]
    foreign = [name for name, table in _rows(conn, _SHOPIFY_CONSTRAINTS_SQL, contains="%shopify%") if table not in expected]
    key = False
    if "shopify_connections" in tables:
        key = any(row[0] == k.CONNECTION_KEY_CONSTRAINT for row in _rows(
            conn, "SELECT conname FROM pg_constraint WHERE conrelid = CAST(:tbl AS regclass) AND contype = 'u'", tbl=_quoted("shopify_connections")))
    return {"tables": sorted(tables), "other_relations": sorted(other), "types": sorted(types),
            "functions": sorted(functions), "foreign_constraints": sorted(foreign), "connection_key": key}


def read_version_rows(conn) -> Optional[List[str]]:
    if not _table_exists(conn, "alembic_version"):
        return None
    return [str(row[0]) for row in _rows(conn, "SELECT version_num FROM public.alembic_version")]


def _timeout_code(exc: BaseException) -> bool:
    original = getattr(exc, "orig", exc)
    return str(getattr(original, "pgcode", "") or "") in ("57014", "55P03")


def run_database_checks(url: str, phase_def: Mapping[str, Any], known: FrozenSet[str], expectations: Mapping[str, Any],
                        expected_database: str, *, engine_factory=None, lock_keys: Tuple[int, int] = k.ADVISORY_LOCK_KEYS) -> List[Check]:
    """One read-only connection, one exclusive advisory lock, every database check, explicit unlock.

    ``engine_factory`` exists for the DB-free contract tests; production uses the
    NullPool engine built here. Any exception inside the locked section still
    attempts the unlock, invalidates the connection and is reported by type only.
    """
    checks: List[Check] = []
    try:
        engine = engine_factory(url) if engine_factory else _engine(url)
    except Exception as exc:  # noqa: BLE001 - the type only; the URL is never printed
        return [Check("connect", k.FAIL, k.R_CONNECT_FAILED, {"error": type(exc).__name__})]
    conn = None
    locked = False
    try:
        try:
            conn = engine.connect().execution_options(postgresql_readonly=True)
        except Exception as exc:  # noqa: BLE001
            return [Check("connect", k.FAIL, k.R_CONNECT_FAILED, {"error": type(exc).__name__})]
        checks.append(Check("connect", k.PASS))
        locked = _scalar(conn, "SELECT pg_try_advisory_lock(:a, :b)", a=lock_keys[0], b=lock_keys[1]) is True
        if not locked:
            checks.append(Check("lock", k.FAIL, k.R_LOCK_DENIED))
            return checks
        checks.append(Check("lock", k.PASS))

        if _scalar(conn, "SELECT pg_is_in_recovery()") is not False:
            checks.append(Check("server", k.FAIL, k.R_SERVER_IN_RECOVERY))
        elif any(str(setting).replace(" ", "") == "default_transaction_read_only=on" for (setting,) in _rows(conn, _DB_ROLE_SETTINGS_SQL)):
            checks.append(Check("server", k.FAIL, k.R_SERVER_READ_ONLY))
        elif _scalar(conn, "SELECT current_schema()") != k.EXPECTED_SCHEMA:
            checks.append(Check("server", k.FAIL, k.R_SCHEMA_NOT_PUBLIC))
        elif _scalar(conn, "SELECT current_database()") != expected_database:
            checks.append(Check("server", k.FAIL, k.R_DATABASE_NAME_MISMATCH))
        else:
            checks.append(Check("server", k.PASS, detail={"schema": k.EXPECTED_SCHEMA}))

        status, reason, detail = k.evaluate_rows(read_version_rows(conn), phase_def["rows"], known)
        checks.append(Check("version_rows", status, reason, detail))

        status, reason, detail = k.evaluate_footprint(observe_footprint(conn, phase_def), phase_def)
        checks.append(Check("shopify_footprint", status, reason, detail))

        status, reason, detail = k.evaluate_retained(observe_retained(conn), expectations)
        checks.append(Check("retained_schema", status, reason, detail))
    except WallTimeout:
        checks.append(Check("wall", k.FAIL, k.R_WALL_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 - every failure is a refusal, reported by type only
        checks.append(Check("database", k.FAIL, k.R_TIMEOUT if _timeout_code(exc) else k.R_INTERNAL_ERROR, {"error": type(exc).__name__}))
    finally:
        if conn is not None:
            # A failed statement leaves the transaction aborted; roll it back first so
            # the session-level advisory lock (which survives rollback) can be released.
            try:
                conn.rollback()
            except Exception:  # noqa: silent-ok — read-only transaction teardown on a connection that may be gone
                pass
            if locked:
                released = False
                try:
                    released = _scalar(conn, "SELECT pg_advisory_unlock(:a, :b)", a=lock_keys[0], b=lock_keys[1]) is True
                except Exception as exc:  # noqa: BLE001 - reported below as an unlock failure
                    checks.append(Check("unlock", k.FAIL, k.R_UNLOCK_FAILED, {"error": type(exc).__name__}))
                else:
                    checks.append(Check("unlock", k.PASS) if released else Check("unlock", k.FAIL, k.R_UNLOCK_FAILED))
                if not released:
                    try:
                        conn.invalidate()
                    except Exception:  # noqa: silent-ok — the connection is being discarded; nothing else to do
                        pass
            try:
                conn.close()
            except Exception:  # noqa: silent-ok — closing a dead connection
                pass
        try:
            engine.dispose()
        except Exception:  # noqa: silent-ok — disposing an engine whose pool is already gone
            pass
    return checks


def _engine(url: str):
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool

    options = (f"-c statement_timeout={k.STATEMENT_TIMEOUT_MS} -c lock_timeout={k.LOCK_TIMEOUT_MS}"
               f" -c idle_in_transaction_session_timeout={k.IDLE_IN_TRANSACTION_TIMEOUT_MS}")
    return sa.create_engine(url, poolclass=NullPool, echo=False, hide_parameters=True, future=True,
                            connect_args={"connect_timeout": k.CONNECT_TIMEOUT_SEC, "application_name": k.APPLICATION_NAME,
                                          "options": options})


# ── Entry point ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shopify_release_gate", description=__doc__.splitlines()[0], add_help=True)
    parser.add_argument("--root", default=k.CONTAINER_ROOT)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--inventory-digest", required=True)
    parser.add_argument("--inventory-count", required=True, type=int)
    parser.add_argument("--expect-project", required=True)
    parser.add_argument("--expect-environment", required=True)
    parser.add_argument("--expect-service", required=True)
    parser.add_argument("--expect-db", required=True)
    return parser


def _alarm(_signum, _frame):
    raise WallTimeout()


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None,
         now: Optional[_dt.datetime] = None) -> int:
    env = dict(environ if environ is not None else os.environ)
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return k.EXIT_USAGE if exc.code else k.EXIT_PASS
    phase_def = k.phase(args.phase)
    expected_db = k.parse_db_target(args.expect_db)
    if phase_def is None or expected_db is None or not k.RELEASE_SHA_RE.match(args.release_sha) \
            or not k.DIGEST_RE.match(args.inventory_digest) or args.inventory_count <= 0 \
            or not all(k.UUID_RE.match(v) for v in (args.expect_project, args.expect_environment, args.expect_service)):
        emit("RESULT=USAGE reason=invalid_arguments")
        return k.EXIT_USAGE
    root = Path(args.root)
    emit(f"gate_version={k.GATE_VERSION} phase={args.phase} release={args.release_sha} root={args.root}")
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(k.WALL_TIMEOUT_SEC)
    checks: List[Check] = []
    try:
        checks.append(check_root(root))
        checks.append(check_release_sha(env, args.release_sha))
        inventory_check, runtime = check_inventory(root, args.inventory_count, args.inventory_digest)
        checks.append(inventory_check)
        checks.append(check_libraries())
        graph_check, known = check_graph(root, phase_def)
        checks.append(graph_check)
        status, reason, detail = k.evaluate_identity(env, {"project": args.expect_project, "environment": args.expect_environment, "service": args.expect_service})
        checks.append(Check("identity", status, reason, detail))
        target_check, bound_url = check_database_target(env, expected_db)
        checks.append(target_check)
        if runtime is not None:
            checks.append(check_approval(root, args.phase, runtime, now or _dt.datetime.now(_dt.timezone.utc)))
        else:
            checks.append(Check("approval", k.FAIL, k.R_APPROVAL_NOT_BOUND, {"phase": args.phase}))
        status, reason, detail = k.evaluate_flags(env)
        checks.append(Check("shopify_flags", status, reason, detail))
        expectations_check, expectations = check_expectations(root)
        checks.append(expectations_check)
        for check in checks:
            report(check)
        if all(c.status == k.PASS for c in checks) and bound_url and known is not None and expectations is not None:
            # The gate's own process keeps no inherited libpq indirection either: the
            # validated explicit URL is the only connection parameter source.
            if environ is None:
                os.environ.clear()
                os.environ.update(sanitized_environment(bound_url, env))
            database_checks = run_database_checks(bound_url, phase_def, known, expectations, expected_db["database"])
            for check in database_checks:
                report(check)
            checks.extend(database_checks)
        else:
            emit("database checks skipped: a source-side check failed")
    except WallTimeout:
        checks.append(Check("wall", k.FAIL, k.R_WALL_TIMEOUT))
        report(checks[-1])
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    failed = [c.name for c in checks if c.status != k.PASS]
    verdict = k.FAIL if failed or not checks else k.PASS
    emit(f"RESULT={verdict} phase={args.phase} release={args.release_sha} checks={len(checks)} failed={json.dumps(failed)}")
    return k.EXIT_PASS if verdict == k.PASS else k.EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
