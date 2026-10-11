#!/usr/bin/env python3
"""Render the trusted pre-deploy bootstrap for the Shopify release gate, and its inputs.

Everything here runs on an operator checkout, never in the deployed container,
and never against the existing platform database. Subcommands:

``inventory --root <checkout>``
    Prints the inventory count and digest, the gate module's SHA-256 and the
    digest of the migration files (JSON). The approval record binds to the
    inventory digest and count.

``render --root <checkout> --release-sha <40 hex> --phase S0|S3 --approval-file <filled payload>``
    Validates the filled approval payload offline against the checkout, the
    release, the target and the phase, then prints the one-line pre-deploy
    command: ``python -I -c`` running a standard-library-only bootstrap
    (base64-embedded; ``--format source`` prints it readable) that carries,
    as literals, the container root, the release commit, the platform
    identity, the libpq variables to refuse, the fixed inventory files, the
    initializers that must be absent, the inventory digest and count, the gate
    module's SHA-256, the approval payload's SHA-256, the phase and the
    database and backup targets — verifies all of it **before importing
    anything from the repository**, then executes the verified gate bytes in
    the same process with source-only repository imports.

``snapshot --database-url <disposable S3 DSN> --out <path> --source-sha <40 hex>``
    Writes the canonical schema expectations (retained relations and the
    S3 Shopify relations) from a disposable database built at ``source-sha``
    with every Shopify revision applied. Read-only. Refuses the existing
    platform database host.

``approval-check --approval-file <payload> --root <checkout> --release-sha <40 hex> --phase S0|S3``
    Evaluates the payload offline and prints the gate's verdict for it.

Every failure is reported as a closed token (an internal ``RenderRefusal``)
or, for any other exception including a third-party ``ValueError``, the
exception type only; a DSN, a path argument or an exception body is never
printed.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.operators import shopify_release_gate as gate  # noqa: E402
from scripts.operators import shopify_release_gate_contract as k  # noqa: E402

PYTHON_IN_IMAGE = "python"


class RenderRefusal(Exception):
    """An internal refusal carrying a closed token; the only exception whose text is ever printed."""

    def __init__(self, token: str):
        super().__init__(token)
        self.token = token if k.TOKEN_RE.match(token or "") else "refusal"

# The bootstrap. Standard library only; every ``{name}`` is a JSON literal
# substituted by ``render_command``. It imports nothing from the repository
# until every pin has been verified, and then executes the verified gate bytes.
BOOTSTRAP_TEMPLATE = r'''import base64, hashlib, os, sys
import importlib.machinery
P = {prefix}
R = {root}
REL = {release}
IDS = {identity}
LIBPQ = {libpq}
FIXED = {fixed}
ABSENT = {absent}
N = {count}
DIG = {digest}
GSHA = {gate_sha}
ASHA = {approval_sha}
ARGV = {argv}


def fail(reason):
    print(P + " BOOTSTRAP FAIL reason=" + reason, flush=True)
    sys.exit(2)


if not os.path.isdir(os.path.join(R, "database")):
    fail("root_missing")
if os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip().lower() != REL:
    fail("release_sha_mismatch")
for name, value in IDS.items():
    if os.environ.get(name, "").strip() != value:
        fail("identity_mismatch")
if any(os.environ.get(name, "").strip() for name in LIBPQ):
    fail("libpq_environment_override")
for relative in ABSENT:
    if os.path.lexists(os.path.join(R, relative)):
        fail("initializer_unexpected")
paths = set(FIXED)
for dirpath, _dirnames, filenames in os.walk(os.path.join(R, "database")):
    for filename in filenames:
        if filename.endswith(".py"):
            paths.add(os.path.relpath(os.path.join(dirpath, filename), R).replace(os.sep, "/"))
if len(paths) != N:
    fail("inventory_count_mismatch")
digest = hashlib.sha256()
verified = {}
try:
    for relative in sorted(paths):
        absolute = os.path.abspath(os.path.join(R, relative))
        with open(absolute, "rb") as handle:
            data = handle.read()
        verified[absolute] = data
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
except OSError:
    fail("inventory_file_missing")
if digest.hexdigest() != DIG:
    fail("inventory_digest_mismatch")
gate_path = os.path.join(R, "scripts/operators/shopify_release_gate.py")
try:
    with open(gate_path, "rb") as handle:
        gate_sha = hashlib.sha256(handle.read()).hexdigest()
except OSError:
    fail("inventory_file_missing")
if gate_sha != GSHA:
    fail("gate_digest_mismatch")
payload = os.environ.get("NAHLA_SHOPIFY_RELEASE_APPROVAL_B64", "").strip()
if not payload:
    fail("approval_payload_missing")
try:
    raw = base64.b64decode(payload, validate=True)
except (ValueError, TypeError):
    fail("approval_payload_malformed")
if hashlib.sha256(raw).hexdigest() != ASHA:
    fail("approval_payload_pin_mismatch")
print(P + " BOOTSTRAP PASS gate_version=2 inventory_count=" + str(N), flush=True)

# Keep the verified bytes in this process. A source-only loader prevents a
# valid-looking, unpinned __pycache__ entry from replacing a verified .py file,
# including Alembic's direct SourceFileLoader use. The finder refuses other
# repository import origins (scripts.py, scripts.pyc, native extensions, etc.)
# before their loader runs. Installed dependencies retain their normal loader.
root = os.path.abspath(R)
directories = set([root])
for absolute in verified:
    parent = os.path.dirname(absolute)
    while parent != root and os.path.commonpath([root, parent]) == root:
        directories.add(parent)
        parent = os.path.dirname(parent)


def inside_repository(path):
    try:
        return os.path.commonpath([root, os.path.abspath(path)]) == root
    except ValueError:
        return False


original_get_code = importlib.machinery.SourceFileLoader.get_code


def verified_get_code(loader, fullname):
    absolute = os.path.abspath(loader.path)
    if inside_repository(absolute):
        data = verified.get(absolute)
        if data is None or not absolute.endswith(".py"):
            fail("repository_import_unverified")
        return compile(data, absolute, "exec", dont_inherit=True)
    return original_get_code(loader, fullname)


class RepositoryImportGate:
    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None:
            return None
        owned_package = fullname.split(".", 1)[0] in ("scripts", "database")
        if owned_package and spec.origin and not inside_repository(spec.origin):
            fail("repository_import_unverified")
        if spec.origin and inside_repository(spec.origin):
            absolute = os.path.abspath(spec.origin)
            if absolute not in verified or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                fail("repository_import_unverified")
            return spec
        for location in spec.submodule_search_locations or ():
            if owned_package and not inside_repository(location):
                fail("repository_import_unverified")
            if inside_repository(location) and os.path.abspath(location) not in directories:
                fail("repository_import_unverified")
        return spec if spec.origin is None else None


importlib.machinery.SourceFileLoader.get_code = verified_get_code
sys.meta_path.insert(0, RepositoryImportGate())
sys.path.insert(0, root)
sys.argv = [gate_path] + ARGV
exec(compile(verified[os.path.abspath(gate_path)], gate_path, "exec"),
     {"__name__": "__main__", "__file__": gate_path})
'''


def inventory_report(root: Path) -> Dict[str, Any]:
    count, digest = gate.inventory(root)
    return {
        "gate_version": k.GATE_VERSION,
        "gate_sha256": gate.file_sha256(root / k.GATE_RELATIVE_PATH),
        "inventory_count": count,
        "inventory_digest": digest,
        "database_py_digest": gate.database_py_digest(root),
        "unexpected_initializers": gate.unexpected_initializers(root),
    }


def load_approval(path: Path) -> Dict[str, Any]:
    """The filled payload bytes and record. Errors are closed tokens."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise RenderRefusal("approval_file_unreadable")
    try:
        record = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RenderRefusal("approval_file_malformed")
    return {"raw": raw, "record": record, "sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "b64": base64.b64encode(raw).decode("ascii")}


def approval_verdict(root: Path, *, approval_file: Path, release_sha: str, phase: str, identity: Mapping[str, str], db: str,
                     backup_service: str, backup_volume: str, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    count, digest = gate.inventory(root)
    loaded = load_approval(approval_file)
    target = dict(k.parse_db_target(db) or {})
    if not target:
        raise RenderRefusal("db_target_malformed")
    bound_target = {"project": identity["project"], "environment": identity["environment"], "service": identity["service"],
                    "db": k.target_label(target)}
    status, reason, detail = k.evaluate_approval(
        loaded["record"], phase_name=phase, release_sha=release_sha, runtime_digest=digest, runtime_count=count,
        target=bound_target, backup_service=backup_service, backup_volume=backup_volume,
        now=now or _dt.datetime.now(_dt.timezone.utc))
    return {"phase": phase, "status": status, "reason": reason, "inventory_count": count, "inventory_digest": digest,
            "approval_sha256": loaded["sha256"], "approval_b64": loaded["b64"], "detail": detail}


def _check_tokens(*values: str) -> None:
    for value in values:
        if not isinstance(value, str) or not value or any(c in value for c in "'\"$`\\") or any(c.isspace() for c in value):
            raise RenderRefusal("token_not_renderable")


def render_source(root: Path, *, release_sha: str, phase: str, approval_sha256: str, project: str, environment: str, service: str,
                  db: str, backup_service: str, backup_volume: str, container_root: str = k.CONTAINER_ROOT) -> str:
    """The bootstrap program with every literal substituted (readable form)."""
    if k.phase(phase) is None:
        raise RenderRefusal("phase_not_deployable" if phase in k.INTERMEDIATE_STATES else "unknown_phase")
    if not k.RELEASE_SHA_RE.match(release_sha):
        raise RenderRefusal("release_sha_malformed")
    if not k.DIGEST_RE.match(approval_sha256):
        raise RenderRefusal("approval_sha_malformed")
    if k.parse_db_target(db) is None:
        raise RenderRefusal("db_target_malformed")
    for value in (project, environment, service, backup_service, backup_volume):
        if not k.UUID_RE.match(value):
            raise RenderRefusal("identity_malformed")
    _check_tokens(container_root, db)
    report = inventory_report(root)
    if report["unexpected_initializers"]:
        raise RenderRefusal("initializer_unexpected")
    argv = [
        "--root", container_root, "--phase", phase, "--release-sha", release_sha,
        "--inventory-digest", report["inventory_digest"], "--inventory-count", str(report["inventory_count"]),
        "--approval-sha256", approval_sha256,
        "--expect-project", project, "--expect-environment", environment, "--expect-service", service, "--expect-db", db,
        "--expect-backup-service", backup_service, "--expect-backup-volume", backup_volume,
    ]
    literals = {
        "prefix": k.LOG_PREFIX, "root": container_root, "release": release_sha,
        "identity": {k.IDENTITY_ENVS["project"]: project, k.IDENTITY_ENVS["environment"]: environment, k.IDENTITY_ENVS["service"]: service},
        "libpq": list(k.LIBPQ_TARGET_ENV_VARS), "fixed": sorted(set(k.LEGACY_FIXED_FILES) | set(k.RUNTIME_EXTRA_FILES)),
        "absent": list(k.MUST_BE_ABSENT), "count": report["inventory_count"], "digest": report["inventory_digest"],
        "gate_sha": report["gate_sha256"], "approval_sha": approval_sha256, "argv": argv,
    }
    source = BOOTSTRAP_TEMPLATE
    for name, value in literals.items():
        source = source.replace("{" + name + "}", json.dumps(value, sort_keys=True))
    return source


def render_command(root: Path, *, python: str = PYTHON_IN_IMAGE, **kwargs: Any) -> str:
    """The exact one-line pre-deploy command: the bootstrap source, base64-embedded, run by ``python -I``."""
    _check_tokens(python)
    source = render_source(root, **kwargs)
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return f'{python} -I -c "import base64,sys;exec(compile(base64.b64decode(\'{encoded}\'),\'release-gate-bootstrap\',\'exec\'))"'


def decode_command(command: str) -> str:
    """The bootstrap source embedded in a rendered command (for review)."""
    start = command.index("b64decode('") + len("b64decode('")
    end = command.index("'", start)
    return base64.b64decode(command[start:end]).decode("utf-8")


def snapshot(database_url: str, *, source_sha: str, generated_at: Optional[str] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    """Canonical schema expectations read from a disposable S3 database built at ``source_sha`` (read-only)."""
    # The authority alone is not the driver's target. Refuse inherited libpq
    # indirection before parsing, then use the existing strict driver-aware
    # validator and a fully explicit URL. Neither step opens a connection.
    import os

    environment = dict(os.environ)
    if any(str(environment.get(name) or "").strip() for name in k.LIBPQ_TARGET_ENV_VARS):
        raise RenderRefusal("snapshot_environment_override")
    target, _refusal = gate.parse_database_url(database_url, environment)
    if target is None:
        raise RenderRefusal("database_url_malformed")
    # libpq interprets an authority host list as failover destinations. A
    # disposable snapshot must name one host, never a list with a hidden
    # existing-platform member. Refuse before constructing the read engine.
    if "," in target["host"]:
        raise RenderRefusal("snapshot_multiple_hosts")
    if target["host"].rstrip(".") == k.EXISTING_PLATFORM_TARGET["db"].split(":", 1)[0]:
        raise RenderRefusal("refusing_existing_platform_database")
    bound_url = gate.explicit_url(database_url, target)
    if bound_url is None:
        raise RenderRefusal("database_url_malformed")
    try:
        engine = gate._engine(bound_url)
        try:
            with engine.connect().execution_options(postgresql_readonly=True) as conn:
                retained = gate.observe_retained(conn)
                footprint = gate.observe_footprint(conn, k.DEPLOY_PHASES["S3"])
                server_version = str(gate._scalar(conn, "SHOW server_version"))
                conn.rollback()
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - the exception type only, never its body
        raise RenderRefusal("snapshot_" + "".join(c for c in type(exc).__name__.lower() if c.isalnum() or c == "_")) from exc
    missing_retained = sorted(set(k.RETAINED_FULL_DEFINITION_TABLES) - set(retained["tables"]))
    missing_shopify = sorted(set(k.SHOPIFY_TABLES) - set(footprint["definitions"]))
    # every sequence observed must be a public sequence owned by an expected public table (the tables that
    # carry a serial key; a table keyed otherwise owns none)
    sequences_ok = bool(footprint["sequences"]) and all(
        name.startswith("public.") and owner and owner.startswith("public.") and owner.split(".", 1)[1] in k.SHOPIFY_TABLES and column
        for name, owner, column in footprint["sequences"])
    if (missing_retained or retained["nonce"] is None or set(retained["pk_types"]) != set(k.PK_TYPE_PINS)
            or len(retained["unique_definitions"]) != len(k.UNIQUE_DEFINITION_PINS) or missing_shopify or footprint["stray_count"]
            or not sequences_ok):
        raise RenderRefusal("database_is_not_a_complete_s3_build")
    return k.normalize({
        "expectations_version": k.EXPECTATIONS_VERSION,
        "generated_from_source_sha": source_sha,
        "database_py_digest": gate.database_py_digest(root or _REPO_ROOT),
        "generated_at_utc": generated_at or _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "generator": "scripts/operators/shopify_release_gate_render.py snapshot",
        "server_version": server_version,
        "retained": retained,
        "shopify_s3": {"tables": footprint["definitions"], "sequences": footprint["sequences"]},
    })


def _add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=k.EXISTING_PLATFORM_TARGET["project"])
    parser.add_argument("--environment", default=k.EXISTING_PLATFORM_TARGET["environment"])
    parser.add_argument("--service", default=k.EXISTING_PLATFORM_TARGET["service"])
    parser.add_argument("--db", default=k.EXISTING_PLATFORM_TARGET["db"])
    parser.add_argument("--backup-service", default=k.EXISTING_PLATFORM_TARGET["backup_service"])
    parser.add_argument("--backup-volume", default=k.EXISTING_PLATFORM_TARGET["backup_volume"])


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401 - argparse hook; never echoes an argument value
        print(json.dumps({"error": "invalid_arguments"}))
        raise SystemExit(k.EXIT_USAGE)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _Parser(prog="shopify_release_gate_render", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory", add_help=False)
    inv.add_argument("--root", default=str(_REPO_ROOT))
    ren = sub.add_parser("render", add_help=False)
    ren.add_argument("--root", default=str(_REPO_ROOT))
    ren.add_argument("--release-sha", required=True)
    ren.add_argument("--phase", required=True)
    ren.add_argument("--approval-file", required=True)
    ren.add_argument("--python", default=PYTHON_IN_IMAGE)
    ren.add_argument("--container-root", default=k.CONTAINER_ROOT)
    ren.add_argument("--format", choices=("command", "source"), default="command")
    _add_target_arguments(ren)
    snap = sub.add_parser("snapshot", add_help=False)
    snap.add_argument("--database-url", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--source-sha", required=True)
    snap.add_argument("--root", default=str(_REPO_ROOT))
    chk = sub.add_parser("approval-check", add_help=False)
    chk.add_argument("--root", default=str(_REPO_ROOT))
    chk.add_argument("--approval-file", required=True)
    chk.add_argument("--release-sha", required=True)
    chk.add_argument("--phase", required=True)
    _add_target_arguments(chk)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if args.command == "inventory":
            print(json.dumps(inventory_report(Path(args.root)), indent=2, sort_keys=True))
        elif args.command in ("render", "approval-check"):
            if not k.RELEASE_SHA_RE.match(args.release_sha):
                raise RenderRefusal("release_sha_malformed")
            identity = {"project": args.project, "environment": args.environment, "service": args.service}
            verdict = approval_verdict(Path(args.root), approval_file=Path(args.approval_file), release_sha=args.release_sha, phase=args.phase,
                                       identity=identity, db=args.db, backup_service=args.backup_service, backup_volume=args.backup_volume)
            if args.command == "approval-check":
                print(json.dumps({key: value for key, value in verdict.items() if key != "approval_b64"}, indent=2, sort_keys=True))
            else:
                if verdict["status"] != k.PASS:
                    raise RenderRefusal("approval_" + str(verdict["reason"]))
                kwargs = dict(release_sha=args.release_sha, phase=args.phase, approval_sha256=verdict["approval_sha256"], project=args.project,
                              environment=args.environment, service=args.service, db=args.db, backup_service=args.backup_service,
                              backup_volume=args.backup_volume, container_root=args.container_root)
                if args.format == "source":
                    print(render_source(Path(args.root), **kwargs))
                else:
                    print(render_command(Path(args.root), python=args.python, **kwargs))
        elif args.command == "snapshot":
            if not k.RELEASE_SHA_RE.match(args.source_sha):
                raise RenderRefusal("source_sha_malformed")
            payload = snapshot(args.database_url, source_sha=args.source_sha, root=Path(args.root))
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1, sort_keys=True)
                handle.write("\n")
            print(json.dumps({key: value for key, value in payload.items() if key not in ("retained", "shopify_s3")}, indent=2, sort_keys=True))
    except RenderRefusal as refusal:
        print(json.dumps({"error": refusal.token}))
        return k.EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - any other exception, third-party ValueError included: the type only, never a body
        print(json.dumps({"error": type(exc).__name__}))
        return k.EXIT_USAGE
    return k.EXIT_PASS


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
