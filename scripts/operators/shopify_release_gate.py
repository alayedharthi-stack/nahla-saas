#!/usr/bin/env python3
"""Shopify release gate — read-only, phase-aware normal-deployment compatibility check.

Executed only by the rendered standard-library bootstrap
(``shopify_release_gate_render.py render``), which has already verified — from
literals it carries and before importing anything from the repository — the
container root, the deployed commit, the platform identity, the absence of
inherited libpq target variables, the complete byte inventory (count and
digest), the absence of forbidden package initializers, this module's own
digest and the SHA-256 of the external approval payload. The gate re-verifies
all of that fail-fast and then performs the database checks::

    python -I /app/scripts/operators/shopify_release_gate.py --root /app --phase S0|S3 \
        --release-sha <40 hex> --inventory-digest <64 hex> --inventory-count <n> \
        --approval-sha256 <64 hex> --expect-project <id> --expect-environment <id> \
        --expect-service <id> --expect-db host:port/database \
        --expect-backup-service <id> --expect-backup-volume <id>

Order, each a refusal that stops the run: root; release commit; inventory
(count, digest, forbidden initializers); library pins; AST preflight of every
migration file (unique string revision ids, single string or null parent, no
``depends_on`` or ``branch_labels``, parents known) before the script
directory is loaded; migration graph (every expected edge, exact repository
heads, the phase's Shopify branch delta, retained rows on distinct branches);
platform identity; database target through the pilot job's own validators
(no query override, no inherited libpq variable, no loopback, exact host,
port and database); the external approval payload (pin, release, inventory,
target, phase, approval window, rollback snapshot, backup identity, size,
completion, restorability, timestamp order and freshness, restore
verification for a schema-changing phase); every Shopify flag off; the schema
expectations bound to the migration files present. Only then: one read-only
connection with bounded timeouts and the legacy fixed search path, the legacy
exclusive advisory key (refused when held by anyone), a writable primary on
``public``, exactly the phase's ``alembic_version`` rows, exactly the phase's
Shopify footprint by canonical definition across every user schema (tables,
the exact pinned sequence set with ownership, no other Shopify-named or
Shopify-owned object), the retained relations by canonical definition, the same relation identities at
the end of the run as at its start, and a verified unlock. It executes nothing
that writes: no Alembic command, no subprocess, no DDL, no DML.

Output is grep-able and secret-safe: closed reason tokens, allow-listed table
and field names, counts for anything else, known revision ids only
(``<unknown>`` otherwise), expected platform ids. Never the DSN, an
environment value, an argument value, a row value, an object name the database
chose, or an exception body.

Exit codes: 0 every check passed; 1 any check failed; 2 invocation error.
"""
from __future__ import annotations

import argparse
import ast
import base64
import datetime as _dt
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
import signal
import sys
import sysconfig
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

logging.disable(logging.CRITICAL)

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

    @property
    def ok(self) -> bool:
        return self.status == k.PASS


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
    """Every ``database/**/*.py`` as a posix relative path (``os.walk``, symlinked directories not followed)."""
    base = root / k.LEGACY_GLOB_ROOT
    found: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(base):
        for name in filenames:
            if name.endswith(k.LEGACY_GLOB_SUFFIX):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
    return found


def inventory_paths(root: Path) -> List[str]:
    return sorted(set(k.LEGACY_FIXED_FILES) | set(_database_py_paths(root)) | set(k.RUNTIME_EXTRA_FILES))


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


def inventory(root: Path) -> Tuple[int, str]:
    return digest_paths(root, inventory_paths(root))


def database_py_digest(root: Path) -> str:
    return digest_paths(root, sorted(_database_py_paths(root)))[1]


def file_sha256(path: Path) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def unexpected_initializers(root: Path) -> List[str]:
    return [relative for relative in k.MUST_BE_ABSENT if (root / relative).exists() or (root / relative).is_symlink()]


# ── Source-side checks (no database) ─────────────────────────────────────────

def check_root(root: Path) -> Check:
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return Check("root", k.FAIL, k.R_ROOT_MISSING)
    if _OWN_FILE != resolved / k.GATE_RELATIVE_PATH or not (resolved / "database").is_dir():
        return Check("root", k.FAIL, k.R_ROOT_MISSING)
    return Check("root", k.PASS, detail={"root": "container" if str(resolved) == k.CONTAINER_ROOT else "non-default"})


def check_release_sha(environ: Mapping[str, str], expected: str) -> Check:
    actual = str(environ.get(k.RELEASE_SHA_ENV) or "").strip().lower()
    if not actual:
        return Check("release_sha", k.FAIL, k.R_RELEASE_SHA_MISSING, {"expected": expected})
    if actual != expected:
        return Check("release_sha", k.FAIL, k.R_RELEASE_SHA_MISMATCH, {"expected": expected})
    return Check("release_sha", k.PASS, detail={"release": expected})


def check_inventory(root: Path, expected_count: int, expected_digest: str) -> Tuple[Check, Optional[Tuple[int, str]]]:
    planted = unexpected_initializers(root)
    if planted:
        return Check("inventory", k.FAIL, k.R_INITIALIZER_UNEXPECTED, {"paths": planted}), None
    try:
        count, digest = inventory(root)
    except FileNotFoundError:
        return Check("inventory", k.FAIL, k.R_INVENTORY_FILE_MISSING), None
    if count != expected_count:
        return Check("inventory", k.FAIL, k.R_INVENTORY_COUNT_MISMATCH, {"expected": expected_count, "observed": count}), None
    if digest != expected_digest:
        return Check("inventory", k.FAIL, k.R_INVENTORY_DIGEST_MISMATCH, {"expected": expected_digest}), None
    return Check("inventory", k.PASS, detail={"count": count, "digest": digest}), (count, digest)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def check_libraries(root: Optional[Path] = None) -> Check:
    """Bind pins to installed distributions and imported origins, never repository metadata."""
    repository = root or _OWN_ROOT
    configured = sysconfig.get_paths()
    trusted = sorted({str(Path(configured[key]).resolve()) for key in ("purelib", "platlib")
                      if configured.get(key) and not _within(Path(configured[key]), repository)})
    modules = {"alembic": "alembic", "sqlalchemy": "sqlalchemy", "psycopg2-binary": "psycopg2"}
    mismatched: List[str] = []
    for name, pinned in k.LIBRARY_PINS.items():
        try:
            distributions = list(importlib.metadata.Distribution.discover(name=name, path=trusted))
            if len(distributions) != 1 or distributions[0].version != pinned:
                mismatched.append(name)
                continue
            distribution = distributions[0]
            dependency_root = Path(distribution.locate_file("")).resolve()
            if str(dependency_root) not in trusted:
                mismatched.append(name)
                continue
            spec = importlib.util.find_spec(modules[name])
            origin = Path(spec.origin).resolve() if spec and spec.origin else None
            recorded = {str(p).replace("\\", "/") for p in distribution.files or ()}
            if (origin is None or _within(origin, repository) or not _within(origin, dependency_root)
                    or origin.relative_to(dependency_root).as_posix() not in recorded):
                mismatched.append(name)
                continue
            imported = importlib.import_module(modules[name])
            # psycopg2 appends build capabilities after the version number.
            actual_version = str(getattr(imported, "__version__", "")).split(" ", 1)[0]
            if Path(imported.__file__).resolve() != origin or actual_version != pinned:
                mismatched.append(name)
        except Exception:  # noqa: BLE001 - refuse discovery/import failures by allow-listed package name only
            mismatched.append(name)
    if mismatched:
        return Check("libraries", k.FAIL, k.R_LIBRARY_PIN_MISMATCH, {"mismatched": sorted(mismatched)})
    return Check("libraries", k.PASS, detail={"pins": dict(k.LIBRARY_PINS)})


_REVISION_FIELDS = ("revision", "down_revision", "depends_on", "branch_labels")
_REVISION_ID_RE = k.TOKEN_RE


def _module_literals(tree: ast.Module) -> Dict[str, Tuple[int, Any]]:
    """Module-level ``name = <literal>`` / ``name: T = <literal>`` for the revision fields: ``{name: (count, value)}``."""
    found: Dict[str, Tuple[int, Any]] = {}
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target not in _REVISION_FIELDS:
            continue
        try:
            literal = ast.literal_eval(value) if value is not None else None
        except (ValueError, TypeError, SyntaxError):
            literal = ("<non-literal>",)
        previous = found.get(target, (0, None))
        found[target] = (previous[0] + 1, literal)
    return found


def preflight_migration_files(root: Path) -> Check:
    """Refuse duplicate, malformed, merge-style or dependency-carrying revisions before any migration module runs."""
    versions = root / "database" / "migrations" / "versions"
    files = sorted(p for p in versions.iterdir() if p.is_file() and p.suffix == ".py" and p.name != "__init__.py")
    ids: Dict[str, int] = {}
    parents: Dict[str, Optional[str]] = {}
    malformed = 0
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, UnicodeDecodeError):
            malformed += 1
            continue
        literals = _module_literals(tree)
        revision = literals.get("revision")
        parent = literals.get("down_revision", (1, None))
        depends = literals.get("depends_on", (1, None))
        labels = literals.get("branch_labels", (1, None))
        if (revision is None or revision[0] != 1 or not isinstance(revision[1], str) or not _REVISION_ID_RE.match(revision[1])
                or parent[0] != 1 or not (parent[1] is None or (isinstance(parent[1], str) and _REVISION_ID_RE.match(parent[1])))
                or depends[0] != 1 or depends[1] is not None or labels[0] != 1 or labels[1] is not None):
            malformed += 1
            continue
        ids[revision[1]] = ids.get(revision[1], 0) + 1
        parents[revision[1]] = parent[1]
    duplicates = sorted(r for r, n in ids.items() if n > 1)
    unknown_parents = sum(1 for parent in parents.values() if parent is not None and parent not in ids)
    detail = {"files": len(files), "malformed_count": malformed, "duplicate_revisions": [d for d in duplicates if d in k.EXPECTED_DOWN_REVISIONS or d in k.EXPECTED_REPOSITORY_HEADS],
              "duplicate_count": len(duplicates), "unknown_parent_count": unknown_parents}
    if malformed or duplicates or unknown_parents or not files:
        return Check("migration_preflight", k.FAIL, k.R_GRAPH_UNLOADABLE, detail)
    return Check("migration_preflight", k.PASS, detail={"files": len(files), "revisions": len(ids)})


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
    delta = branch_chain(down, phase_def.get("database_tip",phase_def["shopify_row"]), k.BRANCH_FORK_REVISION)
    if delta != tuple(phase_def["branch_delta"]):
        return Check("graph", k.FAIL, k.R_GRAPH_BRANCH_DELTA_MISMATCH, {"expected": list(phase_def["branch_delta"])}), None
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
    inherited = sorted(name for name in k.LIBPQ_TARGET_ENV_VARS if str(environ.get(name) or "").strip())
    if inherited:
        return Check("database_target", k.FAIL, k.R_LIBPQ_OVERRIDE, {"variables": inherited}), None
    url = str(environ.get("DATABASE_URL") or "").strip()
    target, refusal = parse_database_url(url, dict(environ))
    if target is None:
        return Check("database_target", k.FAIL, k.R_DB_TARGET_MISMATCH, {"refusal": refusal}), None
    if (target["host"], target["port"], target["database"]) != (expected["host"], expected["port"], expected["database"]):
        return Check("database_target", k.FAIL, k.R_DB_TARGET_MISMATCH, {"refusal": "effective_target_differs"}), None
    bound = explicit_url(url, target)
    if bound is None:
        return Check("database_target", k.FAIL, k.R_DB_TARGET_MISMATCH, {"refusal": "unbindable"}), None
    return Check("database_target", k.PASS, detail={"target": k.target_label(expected)}), bound


def read_approval_payload(environ: Mapping[str, str], pin: str) -> Tuple[Optional[Any], Optional[str]]:
    """The external approval record: base64 in one variable, SHA-256 pinned by the command."""
    raw = str(environ.get(k.APPROVAL_PAYLOAD_ENV) or "").strip()
    if not raw:
        return None, k.R_APPROVAL_PAYLOAD_MISSING
    try:
        payload = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return None, k.R_APPROVAL_PAYLOAD_MALFORMED
    if hashlib.sha256(payload).hexdigest() != pin:
        return None, k.R_APPROVAL_PAYLOAD_PIN_MISMATCH
    try:
        return json.loads(payload.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError):
        return None, k.R_APPROVAL_UNREADABLE


def check_approval(environ: Mapping[str, str], args: argparse.Namespace, runtime: Tuple[int, str], expected_db: Mapping[str, Any],
                   now: _dt.datetime) -> Check:
    record, reason = read_approval_payload(environ, args.approval_sha256)
    if reason:
        return Check("approval", k.FAIL, reason, {"phase": args.phase})
    target = {"project": args.expect_project, "environment": args.expect_environment, "service": args.expect_service,
              "db": k.target_label(expected_db)}
    status, reason, detail = k.evaluate_approval(
        record, phase_name=args.phase, release_sha=args.release_sha, runtime_digest=runtime[1], runtime_count=runtime[0],
        target=target, backup_service=args.expect_backup_service, backup_volume=args.expect_backup_volume, now=now,
        rollback_bindings=schema_rollback_bindings(Path(args.root),args.phase) if args.phase in k.POST_INSTALL_PHASES else None)
    return Check("approval", status, reason, detail)


def read_json(path: Path) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def schema_rollback_bindings(root: Path, phase_name: str = "S4_DORMANT") -> Optional[Dict[str, Any]]:
    """Source-pinned artifact/definition bindings used by renderer and runtime."""
    check, _expectations = check_expectations(root, "S4_DORMANT")
    if check.status != k.PASS:
        return None
    artifact_path = k.COUPON_ROLLBACK_RELATIVE_PATH if phase_name in k.COUPON_POST_PHASES else k.INSTALL_ROLLBACK_RELATIVE_PATH
    artifact = read_json(root / artifact_path)
    if not isinstance(artifact, dict):
        return None
    try:
        return {"artifact": artifact, "artifact_sha256": file_sha256(root / artifact_path),
                "coupon_schema_sha256": file_sha256(root / k.COUPON_EXPECTATIONS_RELATIVE_PATH),
                "schema_definitions_sha256": hashlib.sha256(json.dumps(
                    {name:_expectations[name] for name in ("retained","shopify_s4")},
                    sort_keys=True,separators=(",",":")).encode()).hexdigest(),
                "post_schema_observation_sha256": hashlib.sha256(json.dumps(
                    read_json(root/k.COUPON_EXPECTATIONS_RELATIVE_PATH).get("post"),
                    sort_keys=True,separators=(",",":")).encode()).hexdigest(),
                "schema_expectations_sha256": file_sha256(root / k.INSTALL_EXPECTATIONS_RELATIVE_PATH)}
    except OSError:
        return None


def check_expectations(root: Path, phase_name: str = "S3") -> Tuple[Check, Optional[Dict[str, Any]]]:
    post_install = phase_name in k.POST_INSTALL_PHASES
    path = k.INSTALL_EXPECTATIONS_RELATIVE_PATH if post_install else k.EXPECTATIONS_RELATIVE_PATH
    key = "shopify_s4" if post_install else "shopify_s3"
    expectations = read_json(root / path)
    if (not isinstance(expectations, dict) or expectations.get("expectations_version") != k.EXPECTATIONS_VERSION
            or not isinstance(expectations.get("retained"), dict) or not isinstance(expectations.get(key), dict)
            or not isinstance(expectations[key].get("sequences"), list) or not isinstance(expectations[key].get("tables"), dict)):
        return Check("expectations", k.FAIL, k.R_EXPECTATIONS_UNREADABLE), None
    try:
        current = database_py_digest(root)
    except FileNotFoundError:
        return Check("expectations", k.FAIL, k.R_EXPECTATIONS_UNREADABLE), None
    if expectations.get("database_py_digest") != current:
        return Check("expectations", k.FAIL, k.R_EXPECTATIONS_STALE), None
    if post_install:
        baseline = read_json(root / k.EXPECTATIONS_RELATIVE_PATH)
        if (set(expectations[key]["tables"]) != set(k.INSTALL_TABLES)
                or not isinstance(baseline, dict) or expectations["retained"] != baseline.get("retained")
                or any(expectations[key]["tables"].get(name) != definition
                       for name, definition in baseline.get("shopify_s3", {}).get("tables", {}).items())):
            return Check("expectations", k.FAIL, k.R_EXPECTATIONS_STALE), None
    coupon = read_json(root / k.COUPON_EXPECTATIONS_RELATIVE_PATH)
    profile = k.phase(phase_name).get("coupon_schema","pre")
    if (not isinstance(coupon,dict) or coupon.get("database_py_digest") != current
            or profile not in coupon or not isinstance(coupon[profile],dict)):
        return Check("expectations",k.FAIL,k.R_EXPECTATIONS_STALE),None
    expectations["coupon_retirement"] = coupon[profile]
    return Check("expectations", k.PASS, detail={"generated_from_source_sha": str(expectations.get("generated_from_source_sha") or "")[:12]}), expectations


# ── Database observation (read-only SQL only) ────────────────────────────────

_COLUMNS_SQL = (
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
    " pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated"
    " FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum"
    " WHERE a.attrelid = CAST(:tbl AS regclass) AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum")
# NOT NULL is compared through attnotnull; PostgreSQL 18 additionally records
# it as contype 'n' rows, which are redundant and excluded so both servers
# produce the same canonical form.
_CONSTRAINTS_SQL = (
    "SELECT conname, contype, pg_get_constraintdef(oid), condeferrable, condeferred, convalidated"
    " FROM pg_constraint WHERE conrelid = CAST(:tbl AS regclass) AND contype <> 'n' ORDER BY conname")
_INDEXES_SQL = (
    "SELECT ic.relname, pg_get_indexdef(ic.oid), i.indisvalid, i.indisready, i.indislive"
    " FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid"
    " WHERE i.indrelid = CAST(:tbl AS regclass) ORDER BY ic.relname")
_SEQUENCES_SQL = (
    "SELECT s.relname, t.relname, a.attname FROM pg_class s"
    " JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')"
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
_USER_SCHEMA = (" n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_toast%'"
                " AND n.nspname NOT LIKE 'pg\\_temp%'")
_SHOPIFY_RELATIONS_SQL = (
    "SELECT n.nspname, c.relname, c.relkind,"
    " CASE WHEN c.relkind = 'i' THEN (SELECT t.relname FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid"
    " JOIN pg_namespace tn ON tn.oid = t.relnamespace WHERE i.indexrelid = c.oid AND tn.nspname = 'public')"
    " ELSE NULL END FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
    " WHERE" + _USER_SCHEMA + " AND c.relkind <> 'S' AND c.relname LIKE :pattern ESCAPE '\\' ORDER BY n.nspname, c.relname")
# Every sequence that is Shopify-named or owned by an expected public table, in
# any user schema, with its owning table and column (null when unowned).
_SHOPIFY_SEQUENCES_SQL = (
    "SELECT n.nspname || '.' || s.relname, tn.nspname || '.' || t.relname, a.attname"
    " FROM pg_class s JOIN pg_namespace n ON n.oid = s.relnamespace"
    " LEFT JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')"
    " LEFT JOIN pg_class t ON t.oid = d.refobjid LEFT JOIN pg_namespace tn ON tn.oid = t.relnamespace"
    " LEFT JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid"
    " WHERE s.relkind = 'S' AND" + _USER_SCHEMA +
    " AND (s.relname LIKE :pattern ESCAPE '\\' OR (tn.nspname = 'public' AND t.relname = ANY(:tables))) ORDER BY 1, 2, 3")
_SHOPIFY_TYPES_SQL = (
    "SELECT t.typname, COALESCE(r.relname, (SELECT r2.relname FROM pg_type e JOIN pg_class r2 ON r2.oid = e.typrelid WHERE e.oid = t.typelem)),"
    " n.nspname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace LEFT JOIN pg_class r ON r.oid = t.typrelid"
    " WHERE" + _USER_SCHEMA + " AND (t.typname LIKE :pattern ESCAPE '\\' OR t.typname LIKE :array_pattern ESCAPE '\\') ORDER BY t.typname")
_SHOPIFY_FUNCTIONS_SQL = (
    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
    " WHERE" + _USER_SCHEMA + " AND p.proname LIKE :pattern ESCAPE '\\' ORDER BY p.proname")
_SHOPIFY_CONSTRAINTS_SQL = (
    "SELECT c.conname, r.relname, n.nspname FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid JOIN pg_namespace n ON n.oid = r.relnamespace"
    " WHERE" + _USER_SCHEMA + " AND c.conname LIKE :contains ESCAPE '\\' ORDER BY c.conname")
_OID_SQL = "SELECT CAST(to_regclass(:name) AS oid)"


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
        "indexes": _rows(conn, _INDEXES_SQL, tbl=qualified),
    }


def _index_shape(indexdef: str) -> str:
    marker = " USING "
    position = indexdef.find(marker)
    return indexdef[position + 1:] if position >= 0 else indexdef


def nonce_definition(conn, table: str) -> Dict[str, Any]:
    """The full definition with only constraint and index names abstracted."""
    qualified = _quoted(table)
    columns = _rows(conn, _COLUMNS_SQL, tbl=qualified)
    constraints = sorted((contype, definition, deferrable, deferred, validated)
                         for _name, contype, definition, deferrable, deferred, validated in _rows(conn, _CONSTRAINTS_SQL, tbl=qualified))
    indexes = sorted((" UNIQUE INDEX " in definition, _index_shape(definition), valid, ready, live)
                     for _name, definition, valid, ready, live in _rows(conn, _INDEXES_SQL, tbl=qualified))
    return {"columns": columns, "constraints": constraints, "indexes": indexes}


def observe_retained(conn) -> Dict[str, Any]:
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
    """Every Shopify-named object in every user schema; definitions of the expected public tables; the rest counted."""
    expected = set(phase_def["tables"])
    pattern = k.SHOPIFY_NAME_PREFIX.replace("_", "\\_") + "%"
    tables: List[str] = []
    stray = 0
    for schema, name, kind, owner in _rows(conn, _SHOPIFY_RELATIONS_SQL, pattern=pattern):
        if kind in ("r", "p"):
            tables.append(f"{schema}.{name}")
        elif kind == "i" and owner in expected and schema == "public":
            continue
        else:
            stray += 1
    sequences = [list(row) for row in _rows(conn, _SHOPIFY_SEQUENCES_SQL, pattern=pattern, tables=list(expected))]
    stray += sum(1 for _name, owner, _schema in _rows(conn, _SHOPIFY_TYPES_SQL, pattern=pattern, array_pattern="\\_" + pattern)
                 if owner not in expected)
    stray += len(_rows(conn, _SHOPIFY_FUNCTIONS_SQL, pattern=pattern))
    stray += sum(1 for _name, table, schema in _rows(conn, _SHOPIFY_CONSTRAINTS_SQL, contains="%shopify%")
                 if not (schema == "public" and table in expected))
    definitions = {table: table_definition(conn, table) for table in phase_def["tables"] if f"public.{table}" in tables}
    return {"tables": sorted(tables), "stray_count": stray, "definitions": definitions, "sequences": sequences}


def relation_oids(conn, names: Sequence[str]) -> Dict[str, Any]:
    return {name: _scalar(conn, _OID_SQL, name=_quoted(name)) for name in names}


def read_version_rows(conn) -> Optional[List[str]]:
    if not _table_exists(conn, "alembic_version"):
        return None
    return [str(row[0]) for row in _rows(conn, "SELECT version_num FROM public.alembic_version")]


def _timeout_code(exc: BaseException) -> bool:
    original = getattr(exc, "orig", exc)
    return str(getattr(original, "pgcode", "") or "") in ("57014", "55P03")


def run_database_checks(url: str, phase_def: Mapping[str, Any], known: FrozenSet[str], expectations: Mapping[str, Any],
                        expected_database: str, *, engine_factory=None, lock_key: int = k.ADVISORY_LOCK_KEY) -> List[Check]:
    """One read-only connection, the legacy exclusive advisory key, every database check, explicit verified unlock.

    ``engine_factory`` exists for the DB-free contract tests; production uses the
    NullPool engine built here. Any exception inside the locked section still
    rolls back, attempts the unlock, invalidates the connection on an unlock
    failure and is reported by type only.
    """
    checks: List[Check] = []
    try:
        engine = engine_factory(url) if engine_factory else _engine(url)
    except Exception as exc:  # noqa: BLE001 - the type only; the URL is never printed
        return [Check("connect", k.FAIL, k.R_CONNECT_FAILED, {"error": type(exc).__name__})]
    conn = None
    locked = False
    identity_names = list(k.IDENTITY_TABLES) + list(phase_def["tables"])
    if phase_def.get("coupon_schema") == "post":
        identity_names.append("coupon_identity_retirements")
    try:
        try:
            conn = engine.connect().execution_options(postgresql_readonly=True)
        except Exception as exc:  # noqa: BLE001
            return [Check("connect", k.FAIL, k.R_CONNECT_FAILED, {"error": type(exc).__name__})]
        checks.append(Check("connect", k.PASS))
        locked = _scalar(conn, "SELECT pg_try_advisory_lock(:key)", key=lock_key) is True
        if not locked:
            checks.append(Check("lock", k.FAIL, k.R_LOCK_DENIED))
            return checks
        checks.append(Check("lock", k.PASS))

        search_path = str(_scalar(conn, "SHOW search_path") or "").replace(" ", "")
        if _scalar(conn, "SELECT pg_is_in_recovery()") is not False:
            checks.append(Check("server", k.FAIL, k.R_SERVER_IN_RECOVERY))
        # Inspect the effective default, including server-wide configuration.
        # transaction_read_only is deliberately ON for this observation and
        # is not evidence that the application's ordinary transactions work.
        elif str(_scalar(conn, "SHOW default_transaction_read_only") or "").strip().lower() != "off":
            checks.append(Check("server", k.FAIL, k.R_SERVER_READ_ONLY))
        elif search_path != k.SEARCH_PATH:
            checks.append(Check("server", k.FAIL, k.R_SEARCH_PATH_MISMATCH))
        elif _scalar(conn, "SELECT current_schema()") != k.EXPECTED_SCHEMA:
            checks.append(Check("server", k.FAIL, k.R_SCHEMA_NOT_PUBLIC))
        elif _scalar(conn, "SELECT current_database()") != expected_database:
            checks.append(Check("server", k.FAIL, k.R_DATABASE_NAME_MISMATCH))
        else:
            checks.append(Check("server", k.PASS, detail={"schema": k.EXPECTED_SCHEMA, "search_path": k.SEARCH_PATH}))

        before = relation_oids(conn, identity_names)

        status, reason, detail = k.evaluate_rows(read_version_rows(conn), phase_def["rows"], known)
        checks.append(Check("version_rows", status, reason, detail))

        status, reason, detail = k.evaluate_footprint(observe_footprint(conn, phase_def), phase_def,
                                                   expectations.get(phase_def.get("expectations_key", "shopify_s3")) or {})
        checks.append(Check("shopify_footprint", status, reason, detail))

        status, reason, detail = k.evaluate_retained(observe_retained(conn), expectations.get("retained") or {})
        checks.append(Check("retained_schema", status, reason, detail))

        from scripts.operators import coupon_retirement_schema as coupon_schema
        actual = coupon_schema.observe(conn,table_definition)
        wanted = expectations.get("coupon_retirement",coupon_schema.EMPTY)
        valid = coupon_schema.matches(actual,wanted)
        checks.append(Check("coupon_retirement_schema",k.PASS if valid else k.FAIL,
                            None if valid else "coupon_retirement_schema_drift"))

        # A fresh transaction: the relations checked must still be the same objects.
        conn.rollback()
        after = relation_oids(conn, identity_names)
        if not coupon_schema.matches(coupon_schema.observe(conn,table_definition),wanted):
            checks.append(Check("coupon_retirement_recheck",k.FAIL,"coupon_retirement_schema_drift"))
        status, reason, detail = k.evaluate_relation_identity(before, after)
        checks.append(Check("relation_identity", status, reason, detail))
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
                    released = _scalar(conn, "SELECT pg_advisory_unlock(:key)", key=lock_key) is True
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
               f" -c idle_in_transaction_session_timeout={k.IDLE_IN_TRANSACTION_TIMEOUT_MS} -c search_path={k.SEARCH_PATH}")
    return sa.create_engine(url, poolclass=NullPool, echo=False, hide_parameters=True, future=True,
                            connect_args={"connect_timeout": k.CONNECT_TIMEOUT_SEC, "application_name": k.APPLICATION_NAME,
                                          "options": options})


# ── Entry point ──────────────────────────────────────────────────────────────

class _Parser(argparse.ArgumentParser):
    """Never echoes an argument value: a bad invocation is reported by a closed token only."""

    def error(self, message: str) -> None:  # noqa: D401 - argparse hook
        emit("RESULT=USAGE reason=invalid_arguments")
        raise SystemExit(k.EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="shopify_release_gate", add_help=False)
    parser.add_argument("--root", default=k.CONTAINER_ROOT)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--inventory-digest", required=True)
    parser.add_argument("--inventory-count", required=True)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--expect-project", required=True)
    parser.add_argument("--expect-environment", required=True)
    parser.add_argument("--expect-service", required=True)
    parser.add_argument("--expect-db", required=True)
    parser.add_argument("--expect-backup-service", required=True)
    parser.add_argument("--expect-backup-volume", required=True)
    return parser


def _alarm(_signum, _frame):
    raise WallTimeout()


def _valid_arguments(args: argparse.Namespace) -> bool:
    try:
        args.inventory_count = int(args.inventory_count)
    except (TypeError, ValueError):
        return False
    return (k.phase(args.phase) is not None and k.parse_db_target(args.expect_db) is not None
            and bool(k.RELEASE_SHA_RE.match(args.release_sha)) and bool(k.DIGEST_RE.match(args.inventory_digest))
            and bool(k.DIGEST_RE.match(args.approval_sha256)) and args.inventory_count > 0
            and all(k.UUID_RE.match(v) for v in (args.expect_project, args.expect_environment, args.expect_service,
                                                 args.expect_backup_service, args.expect_backup_volume)))


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None,
         now: Optional[_dt.datetime] = None) -> int:
    env = dict(environ if environ is not None else os.environ)
    try:
        args = build_parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 0)
    if str(args.phase) in k.INTERMEDIATE_STATES:
        emit(f"RESULT=USAGE reason={k.R_PHASE_NOT_DEPLOYABLE}")
        return k.EXIT_USAGE
    if not _valid_arguments(args):
        emit("RESULT=USAGE reason=invalid_arguments")
        return k.EXIT_USAGE
    phase_def = k.phase(args.phase)
    expected_db = k.parse_db_target(args.expect_db)
    root = Path(args.root)
    emit(f"gate_version={k.GATE_VERSION} phase={args.phase} release={args.release_sha}")
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(k.WALL_TIMEOUT_SEC)
    checks: List[Check] = []

    def stop(check: Check) -> bool:
        checks.append(check)
        report(check)
        return not check.ok

    try:
        while True:  # a single pass; ``break`` on the first refusal
            if stop(check_root(root)) or stop(check_release_sha(env, args.release_sha)):
                break
            inventory_check, runtime = check_inventory(root, args.inventory_count, args.inventory_digest)
            if stop(inventory_check) or stop(check_libraries(root)) or stop(preflight_migration_files(root)):
                break
            graph_check, known = check_graph(root, phase_def)
            if stop(graph_check):
                break
            status, reason, detail = k.evaluate_identity(env, {"project": args.expect_project, "environment": args.expect_environment,
                                                               "service": args.expect_service})
            if stop(Check("identity", status, reason, detail)):
                break
            target_check, bound_url = check_database_target(env, expected_db)
            if stop(target_check) or stop(check_approval(env, args, runtime, expected_db, now or _dt.datetime.now(_dt.timezone.utc))):
                break
            status, reason, detail = k.evaluate_flags(env, args.phase)
            if stop(Check("shopify_flags", status, reason, detail)):
                break
            expectations_check, expectations = check_expectations(root, args.phase)
            if stop(expectations_check):
                break
            if environ is None:
                # The gate's own process keeps no inherited libpq indirection either: the
                # validated explicit URL is the only connection parameter source.
                os.environ.clear()
                os.environ.update(sanitized_environment(bound_url, env))
            for check in run_database_checks(bound_url, phase_def, known, expectations, expected_db["database"]):
                checks.append(check)
                report(check)
            break
    except WallTimeout:
        checks.append(Check("wall", k.FAIL, k.R_WALL_TIMEOUT))
        report(checks[-1])
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    failed = [c.name for c in checks if not c.ok]
    complete = {"connect", "lock", "server", "version_rows", "shopify_footprint", "retained_schema", "relation_identity", "unlock"} <= {c.name for c in checks}
    verdict = k.PASS if checks and not failed and complete else k.FAIL
    if "connect" not in {c.name for c in checks}:
        emit("database checks skipped: a source-side check failed")
    emit(f"RESULT={verdict} phase={args.phase} release={args.release_sha} checks={len(checks)} failed={json.dumps(failed)}")
    return k.EXIT_PASS if verdict == k.PASS else k.EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover - entry point
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _exc:  # noqa: BLE001 - the type only, never a body
        emit(f"RESULT=FAIL reason={k.R_INTERNAL_ERROR} error={type(_exc).__name__}")
        sys.exit(k.EXIT_FAIL)
