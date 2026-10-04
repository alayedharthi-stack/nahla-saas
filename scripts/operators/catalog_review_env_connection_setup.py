#!/usr/bin/env python3
"""
scripts/operators/catalog_review_env_connection_setup.py
────────────────────────────────────────────────────────
Review-environment-only operator: configure the WhatsApp **catalog connection**
of one tenant in the catalog_management review database so the Nahla → Meta
publish path can run against an empty test catalog with a pre-provisioned
system-user token.

What it writes (review database only, one tenant):
  whatsapp_connections: provider=meta, connection_type (default ``embedded``),
  status=connected, catalog_enabled=true, meta_catalog_id=<test catalog>,
  optional business_manager_id / whatsapp_business_account_id, and
  access_token **encrypted at rest** through the existing
  ``services.whatsapp_platform.wa_connection_secrets.store_access_token``
  path (``core.wa_token_crypto``). Nothing else is touched.

Hard stops (the script exits non-zero and writes nothing):
  * ``NAHLA_CATALOG_REVIEW_ENV`` unset, or any isolation check of
    ``core.review_environment`` fails — identity, database host allowlist,
    ``nahla.environment`` database marker, dashboard URL. Production and
    ``postgres-staging`` bindings are therefore refused by construction.
  * The tenant does not exist in the review database.
  * Another tenant's connection already carries the catalog id
    (same rule as the runtime claim guard).
  * ``--write`` without the confirmation variable.

Secrets: the token is read from an environment variable (default
``NAHLA_CATALOG_REVIEW_WA_TOKEN``) or from stdin (``--token-stdin``); it is
never printed, never written to a file, never echoed in the manifest (only
``token_provided`` and the stored ciphertext prefix are reported). The DSN is
never printed.

Dry-run is the default. Usage (inside the review service container only)::

    python scripts/operators/catalog_review_env_connection_setup.py \
        --tenant-id 1 --catalog-id 1234567890 --business-id 248365378024448

    export NAHLA_CATALOG_REVIEW_CONNECTION_WRITE_CONFIRM=RUN_CATALOG_REVIEW_CONNECTION_WRITE
    python scripts/operators/catalog_review_env_connection_setup.py \
        --tenant-id 1 --catalog-id 1234567890 --business-id 248365378024448 --write

This script is **not** executed by CI, by the application, or by any
scheduler. Running it is an owner-approved step of the review plan.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(ROOT), str(ROOT / "backend"), str(ROOT / "database")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

CONFIRMATION_ENV = "NAHLA_CATALOG_REVIEW_CONNECTION_WRITE_CONFIRM"
CONFIRMATION_TOKEN = "RUN_CATALOG_REVIEW_CONNECTION_WRITE"
DEFAULT_TOKEN_ENV = "NAHLA_CATALOG_REVIEW_WA_TOKEN"
_ALLOWED_CONNECTION_TYPES = ("embedded", "direct", "coexistence")


class OperatorStop(RuntimeError):
    def __init__(self, stage: str, code: str):
        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code


def _manifest(stage: str, status: str, **fields: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"operator": "catalog_review_env_connection_setup", "stage": stage, "status": status}
    out.update(fields)
    return out


def _emit(payload: Mapping[str, Any]) -> None:
    print(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))


def read_token(args: argparse.Namespace, env: Mapping[str, str], stdin_reader=None) -> str:
    """Token from stdin or an env var. Never logged."""
    if args.token_stdin:
        reader = stdin_reader or (lambda: sys.stdin.read())
        token = str(reader() or "").strip()
    else:
        token = str(env.get(args.token_env) or "").strip()
    if not token:
        raise OperatorStop("token", "token_missing")
    if "\n" in token or " " in token:
        raise OperatorStop("token", "token_malformed")
    return token


def guard_isolation(env: Mapping[str, str], *, marker_reader=None) -> Dict[str, Any]:
    """Stop unless the review environment isolation is fully proven (incl. DB marker)."""
    from core.review_environment import evaluate_review_environment  # noqa: PLC0415

    check = evaluate_review_environment(env, with_database_marker=True, marker_reader=marker_reader)
    if not check.enabled:
        raise OperatorStop("isolation", "review_env_flag_unset")
    if not check.ok:
        raise OperatorStop("isolation", ",".join(check.failures))
    return check.as_dict()


def plan_connection(
    session: Any,
    *,
    tenant_id: int,
    catalog_id: str,
    business_id: Optional[str],
    waba_id: Optional[str],
    phone_number_id: Optional[str],
    connection_type: str,
) -> Dict[str, Any]:
    """Validate preconditions and describe the write. Read-only."""
    from models import Tenant, WhatsAppConnection  # noqa: PLC0415

    if connection_type not in _ALLOWED_CONNECTION_TYPES:
        raise OperatorStop("plan", "connection_type_rejected")
    cid = str(catalog_id or "").strip()
    if not cid.isdigit():
        raise OperatorStop("plan", "catalog_id_rejected")
    tenant = session.query(Tenant).filter(Tenant.id == int(tenant_id)).first()
    if tenant is None:
        raise OperatorStop("plan", "tenant_not_found")
    others = (
        session.query(WhatsAppConnection.tenant_id)
        .filter(WhatsAppConnection.meta_catalog_id == cid, WhatsAppConnection.tenant_id != int(tenant_id))
        .all()
    )
    if others:
        raise OperatorStop("plan", "catalog_claimed_by_other_tenant")
    existing = session.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == int(tenant_id)).first()
    return {
        "tenant_id": int(tenant_id),
        "connection_exists": existing is not None,
        "action": "update" if existing is not None else "insert",
        "meta_catalog_id": cid,
        "catalog_enabled": True,
        "connection_type": connection_type,
        "business_manager_id": business_id or None,
        "whatsapp_business_account_id": waba_id or None,
        "phone_number_id": phone_number_id or None,
        "token_storage": "encrypted_at_rest(enc1)",
    }


def apply_connection(session: Any, plan: Mapping[str, Any], token: str) -> Dict[str, Any]:
    """Perform the write described by *plan*. Token encrypted via the existing path."""
    from datetime import datetime, timezone  # noqa: PLC0415

    from models import WhatsAppConnection  # noqa: PLC0415
    from services.whatsapp_platform.wa_connection_secrets import store_access_token  # noqa: PLC0415

    tenant_id = int(plan["tenant_id"])
    conn = session.query(WhatsAppConnection).filter(WhatsAppConnection.tenant_id == tenant_id).first()
    if conn is None:
        conn = WhatsAppConnection(tenant_id=tenant_id)
        session.add(conn)
    conn.provider = "meta"
    conn.connection_type = str(plan["connection_type"])
    conn.status = "connected"
    conn.catalog_enabled = True
    conn.meta_catalog_id = str(plan["meta_catalog_id"])
    if plan.get("business_manager_id"):
        conn.business_manager_id = str(plan["business_manager_id"])
    if plan.get("whatsapp_business_account_id"):
        conn.whatsapp_business_account_id = str(plan["whatsapp_business_account_id"])
    if plan.get("phone_number_id"):
        conn.phone_number_id = str(plan["phone_number_id"])
    conn.token_type = "system_user"
    conn.connected_at = conn.connected_at or datetime.now(timezone.utc).replace(tzinfo=None)
    store_access_token(conn, token)
    session.commit()
    stored = str(conn.access_token or "")
    return {
        "tenant_id": tenant_id,
        "meta_catalog_id": conn.meta_catalog_id,
        "catalog_enabled": bool(conn.catalog_enabled),
        "token_stored_prefix": stored[:5] if stored else None,  # "enc1:" proves encryption at rest
        "token_encrypted": stored.startswith("enc1:"),
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--tenant-id", type=int, required=True)
    p.add_argument("--catalog-id", required=True, help="Empty test catalog owned by the app owner's portfolio")
    p.add_argument("--business-id", default=None, help="Business portfolio id that owns the test catalog")
    p.add_argument("--waba-id", default=None)
    p.add_argument("--phone-number-id", default=None)
    p.add_argument("--connection-type", default="embedded", choices=list(_ALLOWED_CONNECTION_TYPES))
    p.add_argument("--token-env", default=DEFAULT_TOKEN_ENV, help="Env var holding the system-user token (never printed)")
    p.add_argument("--token-stdin", action="store_true", help="Read the token from stdin instead of an env var")
    p.add_argument("--write", action="store_true", help=f"Apply the write; requires {CONFIRMATION_ENV}={CONFIRMATION_TOKEN}")
    return p


def run(argv: Optional[list] = None, *, env: Optional[Mapping[str, str]] = None, session_factory=None, marker_reader=None, stdin_reader=None) -> int:
    env = env if env is not None else os.environ
    args = build_parser().parse_args(argv)

    try:
        isolation = guard_isolation(env, marker_reader=marker_reader)
    except OperatorStop as stop:
        _emit(_manifest("isolation", "refused", error=stop.code))
        return 2

    if args.write and (env.get(CONFIRMATION_ENV) or "").strip() != CONFIRMATION_TOKEN:
        _emit(_manifest("confirmation", "refused", error="dangerous_action_not_confirmed", isolation=isolation))
        return 3

    try:
        token = read_token(args, env, stdin_reader=stdin_reader)
    except OperatorStop as stop:
        _emit(_manifest("token", "refused", error=stop.code, isolation=isolation))
        return 4

    if session_factory is None:
        from sqlalchemy import create_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415
        from sqlalchemy.pool import NullPool  # noqa: PLC0415

        engine = create_engine(env["DATABASE_URL"], poolclass=NullPool, future=True)
        session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    session = session_factory()
    try:
        try:
            plan = plan_connection(
                session,
                tenant_id=args.tenant_id,
                catalog_id=args.catalog_id,
                business_id=args.business_id,
                waba_id=args.waba_id,
                phone_number_id=args.phone_number_id,
                connection_type=args.connection_type,
            )
        except OperatorStop as stop:
            _emit(_manifest("plan", "refused", error=stop.code, isolation=isolation))
            return 5

        if not args.write:
            _emit(_manifest("plan", "dry_run", plan=plan, token_provided=True, isolation=isolation))
            return 0

        result = apply_connection(session, plan, token)
        _emit(_manifest("apply", "written", result=result, isolation=isolation))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(run())
