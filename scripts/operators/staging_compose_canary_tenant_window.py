"""Bounded staging operator for conditional-coupon compose canary tenant window.

Read/apply/restore closed canary ``ai_settings`` on an explicit tenant id.
Does not enable env master flags or send provider messages.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [
    REPO,
    os.path.join(REPO, "backend"),
    os.path.join(REPO, "database"),
    "/app",
    "/app/backend",
    "/app/database",
]

from sqlalchemy import text  # noqa: E402
from database.session import SessionLocal  # noqa: E402
from scripts.operators.customer_conditional_coupon_consumer_verify_contract import (  # noqa: E402
    FIXTURE_TENANT_ID,
    eligible_compose_canary_ai_settings,
)


def _read_ai_settings(db: Any, tenant_id: int) -> dict[str, Any]:
    row = db.execute(
        text("SELECT ai_settings FROM tenant_settings WHERE tenant_id = :tenant_id"),
        {"tenant_id": int(tenant_id)},
    ).fetchone()
    if not row or not isinstance(row[0], dict):
        return {}
    return copy.deepcopy(row[0])


def _write_ai_settings(db: Any, tenant_id: int, settings: dict[str, Any]) -> None:
    db.execute(
        text(
            """
            UPDATE tenant_settings
            SET ai_settings = CAST(:settings AS jsonb), updated_at = NOW()
            WHERE tenant_id = :tenant_id
            """
        ),
        {"tenant_id": int(tenant_id), "settings": json.dumps(settings, ensure_ascii=False)},
    )


def cmd_read(tenant_id: int) -> int:
    db = SessionLocal()
    try:
        payload = {
            "operator": "staging_compose_canary_tenant_window",
            "action": "read",
            "tenant_id": int(tenant_id),
            "ai_settings": _read_ai_settings(db, tenant_id),
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        return 0
    finally:
        db.close()


def cmd_apply(tenant_id: int, phone: str) -> int:
    db = SessionLocal()
    try:
        original = _read_ai_settings(db, tenant_id)
        merged = dict(original)
        merged.update(eligible_compose_canary_ai_settings(tenant_id=tenant_id, phone=phone))
        _write_ai_settings(db, tenant_id, merged)
        db.commit()
        payload = {
            "operator": "staging_compose_canary_tenant_window",
            "action": "apply",
            "tenant_id": int(tenant_id),
            "ai_settings": merged,
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        return 0
    finally:
        db.close()


def cmd_restore(tenant_id: int, snapshot_file: str) -> int:
    snapshot = json.loads(open(snapshot_file, encoding="utf-8").read())
    settings = snapshot.get("ai_settings")
    if not isinstance(settings, dict):
        raise SystemExit("snapshot_ai_settings_invalid")
    db = SessionLocal()
    try:
        _write_ai_settings(db, tenant_id, settings)
        db.commit()
        payload = {
            "operator": "staging_compose_canary_tenant_window",
            "action": "restore",
            "tenant_id": int(tenant_id),
            "ai_settings": settings,
        }
        sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        return 0
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    read_p = sub.add_parser("read")
    read_p.add_argument("--tenant-id", type=int, default=FIXTURE_TENANT_ID)
    apply_p = sub.add_parser("apply")
    apply_p.add_argument("--tenant-id", type=int, default=FIXTURE_TENANT_ID)
    apply_p.add_argument("--phone", default=None)
    restore_p = sub.add_parser("restore")
    restore_p.add_argument("--tenant-id", type=int, default=FIXTURE_TENANT_ID)
    restore_p.add_argument("--snapshot-file", required=True)
    args = parser.parse_args()
    if args.command == "read":
        return cmd_read(args.tenant_id)
    if args.command == "apply":
        from scripts.operators.customer_conditional_coupon_consumer_verify_contract import (  # noqa: PLC0415
            FIXTURE_CUSTOMER_PHONE,
        )

        phone = str(args.phone or FIXTURE_CUSTOMER_PHONE)
        return cmd_apply(args.tenant_id, phone)
    if args.command == "restore":
        return cmd_restore(args.tenant_id, args.snapshot_file)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
