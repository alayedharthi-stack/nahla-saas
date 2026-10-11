#!/usr/bin/env python3
"""Render the trusted pre-deploy command for the Shopify release gate, and its inputs.

Everything here runs on an operator checkout, never in the deployed container,
and never against the existing platform database. Subcommands:

``inventory --root <checkout>``
    Prints the runtime and full inventory counts and digests and the gate
    module's SHA-256 (JSON). The approval record binds to the runtime digest.

``render --root <checkout> --release-sha <40 hex> --phase <S0..S3>``
    Prints the one-line pre-deploy command. It pins, as literals: the gate
    module's SHA-256 (verified by the command before the gate runs), the
    release commit, the full inventory digest and count, the phase, the
    platform identity (project, environment, service) and the database
    target. Identity and target default to the existing platform target in the
    contract and may be overridden for a disposable rehearsal.

``snapshot --database-url <disposable DSN> --out <path> --source-sha <40 hex>``
    Writes the canonical schema expectations the gate compares the retained
    relations with. Read-only. Refuses the existing platform database host.

``approval-check --root <checkout> --phase <S0..S3>``
    Evaluates the approval record offline against the checkout's runtime
    inventory and prints the gate's verdict for it (no database).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.operators import shopify_release_gate as gate  # noqa: E402
from scripts.operators import shopify_release_gate_contract as k  # noqa: E402

PYTHON_IN_IMAGE = "python"


def inventory_report(root: Path) -> Dict[str, Any]:
    runtime_count, runtime_digest = gate.runtime_inventory(root)
    full_count, full_digest = gate.full_inventory(root)
    return {
        "gate_version": k.GATE_VERSION,
        "gate_sha256": gate.file_sha256(root / k.GATE_RELATIVE_PATH),
        "runtime_inventory_count": runtime_count,
        "runtime_inventory_digest": runtime_digest,
        "full_inventory_count": full_count,
        "full_inventory_digest": full_digest,
        "database_py_digest": gate.database_py_digest(root),
    }


def render_command(root: Path, *, release_sha: str, phase: str, project: str, environment: str, service: str,
                   db: str, python: str = PYTHON_IN_IMAGE, container_root: str = k.CONTAINER_ROOT) -> str:
    """The exact pre-deploy command: verify the gate file's digest, then exec the gate with every pin as a literal."""
    if k.phase(phase) is None:
        raise ValueError("unknown_phase")
    if not k.RELEASE_SHA_RE.match(release_sha):
        raise ValueError("release_sha_malformed")
    if k.parse_db_target(db) is None:
        raise ValueError("db_target_malformed")
    for value in (project, environment, service):
        if not k.UUID_RE.match(value):
            raise ValueError("identity_malformed")
    report = inventory_report(root)
    gate_path = f"{container_root}/{k.GATE_RELATIVE_PATH}"
    argv = [
        "--root", container_root, "--phase", phase, "--release-sha", release_sha,
        "--inventory-digest", report["full_inventory_digest"], "--inventory-count", str(report["full_inventory_count"]),
        "--expect-project", project, "--expect-environment", environment, "--expect-service", service, "--expect-db", db,
    ]
    for token in argv + [gate_path, python]:
        if "'" in token or '"' in token or "$" in token or "`" in token or "\\" in token or any(c.isspace() for c in token):
            raise ValueError("token_not_renderable")
    inner = (
        "import hashlib,os,sys;"
        f"p='{gate_path}';"
        "sys.exit(2) if not os.path.isfile(p) else None;"
        "h=hashlib.sha256(open(p,'rb').read()).hexdigest();"
        f"print('{k.LOG_PREFIX} trusted_command gate_version={k.GATE_VERSION} gate_sha256_match='+str(h=='{report['gate_sha256']}'),flush=True);"
        f"sys.exit(2) if h!='{report['gate_sha256']}' else os.execv(sys.executable,[sys.executable,'-I',p,{','.join(repr(a) for a in argv)}])"
    )
    return f'{python} -I -c "{inner}"'


def snapshot(database_url: str, *, source_sha: str, generated_at: Optional[str] = None, root: Optional[Path] = None) -> Dict[str, Any]:
    """Canonical schema expectations read from a disposable database built at ``source_sha`` (read-only)."""
    from sqlalchemy.engine import make_url

    parsed = make_url(database_url)
    if str(parsed.host or "").lower() == k.EXISTING_PLATFORM_TARGET["db"].split(":", 1)[0]:
        raise ValueError("refusing_existing_platform_database")
    engine = gate._engine(database_url)
    try:
        with engine.connect().execution_options(postgresql_readonly=True) as conn:
            observed = gate.observe_retained(conn)
            server_version = str(gate._scalar(conn, "SHOW server_version"))
            conn.rollback()
    finally:
        engine.dispose()
    missing = sorted(set(k.RETAINED_FULL_DEFINITION_TABLES) - set(observed["tables"]))
    if missing or observed["nonce"] is None or set(observed["pk_types"]) != set(k.PK_TYPE_PINS) \
            or len(observed["unique_definitions"]) != len(k.UNIQUE_DEFINITION_PINS):
        raise ValueError("database_is_not_a_complete_s0_build")
    observed = json.loads(json.dumps(observed))  # JSON shape (lists), exactly as the file will hold it
    return {
        "expectations_version": k.EXPECTATIONS_VERSION,
        "generated_from_source_sha": source_sha,
        "database_py_digest": gate.database_py_digest(root or _REPO_ROOT),
        "generated_at_utc": generated_at or _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "generator": "scripts/operators/shopify_release_gate_render.py snapshot",
        "server_version": server_version,
        "tables": observed["tables"],
        "nonce": observed["nonce"],
        "pk_types": observed["pk_types"],
        "unique_definitions": observed["unique_definitions"],
        "sequences": observed["sequences"],
    }


def approval_check(root: Path, phase: str, now: Optional[_dt.datetime] = None) -> Dict[str, Any]:
    runtime = gate.runtime_inventory(root)
    record = gate.read_json(root / k.APPROVAL_RELATIVE_PATH)
    status, reason, detail = k.evaluate_approval(record, phase, runtime_digest=runtime[1], runtime_count=runtime[0],
                                                 now=now or _dt.datetime.now(_dt.timezone.utc))
    return {"phase": phase, "status": status, "reason": reason, "runtime_inventory_count": runtime[0],
            "runtime_inventory_digest": runtime[1], "detail": detail}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="shopify_release_gate_render")
    sub = parser.add_subparsers(dest="command", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--root", default=str(_REPO_ROOT))
    ren = sub.add_parser("render")
    ren.add_argument("--root", default=str(_REPO_ROOT))
    ren.add_argument("--release-sha", required=True)
    ren.add_argument("--phase", required=True)
    ren.add_argument("--project", default=k.EXISTING_PLATFORM_TARGET["project"])
    ren.add_argument("--environment", default=k.EXISTING_PLATFORM_TARGET["environment"])
    ren.add_argument("--service", default=k.EXISTING_PLATFORM_TARGET["service"])
    ren.add_argument("--db", default=k.EXISTING_PLATFORM_TARGET["db"])
    ren.add_argument("--python", default=PYTHON_IN_IMAGE)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--database-url", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--source-sha", required=True)
    snap.add_argument("--root", default=str(_REPO_ROOT))
    chk = sub.add_parser("approval-check")
    chk.add_argument("--root", default=str(_REPO_ROOT))
    chk.add_argument("--phase", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "inventory":
            print(json.dumps(inventory_report(Path(args.root)), indent=2, sort_keys=True))
        elif args.command == "render":
            print(render_command(Path(args.root), release_sha=args.release_sha, phase=args.phase, project=args.project,
                                 environment=args.environment, service=args.service, db=args.db, python=args.python))
        elif args.command == "snapshot":
            if not k.RELEASE_SHA_RE.match(args.source_sha):
                raise ValueError("source_sha_malformed")
            payload = snapshot(args.database_url, source_sha=args.source_sha, root=Path(args.root))
            with open(args.out, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=1, sort_keys=True)
                handle.write("\n")
            print(json.dumps({k_: v for k_, v in payload.items() if k_ not in ("tables", "nonce", "sequences", "unique_definitions", "pk_types")}, indent=2, sort_keys=True))
        else:
            print(json.dumps(approval_check(Path(args.root), args.phase), indent=2, sort_keys=True))
    except (ValueError, FileNotFoundError) as exc:
        # The reason token or the exception type only: a DSN is never printed.
        print(json.dumps({"error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}))
        return k.EXIT_USAGE
    return k.EXIT_PASS


def quoted_for_shell(command: str) -> str:
    """The command as a single shell word, for operators who store it in a quoted field."""
    return shlex.quote(command)


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
