#!/usr/bin/env python3
"""
scripts/enable_partner_testing_override.py
────────────────────────────────────────────
Idempotent production enablement for Salla partner testing override (tenant 1).

  railway ssh --service nahla-saas python3 scripts/enable_partner_testing_override.py
  railway ssh --service nahla-saas python3 scripts/enable_partner_testing_override.py --verify-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND), "/app", "/app/backend"):
    if p and p not in sys.path and Path(p).exists():
        sys.path.insert(0, p)

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
TENANT_ID = 1
CONTROL_TENANT_ID = 2
DEFAULT_DAYS = 30


def _connect():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _override_blob(*, granted_by: str, days: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "enabled": True,
        "reason": "salla_partner_testing",
        "plan_slug": "scale",
        "expires_at": (now + timedelta(days=days)).replace(microsecond=0).isoformat(),
        "granted_at": now.replace(microsecond=0).isoformat(),
        "granted_by": granted_by,
    }


def upsert_override(cur, *, granted_by: str = "ops", days: int = DEFAULT_DAYS) -> str:
    """Insert or update tenant_settings for tenant 1. Returns 'inserted' or 'updated'."""
    cur.execute(
        "SELECT id, metadata FROM tenant_settings WHERE tenant_id = %s",
        (TENANT_ID,),
    )
    row = cur.fetchone()
    blob = _override_blob(granted_by=granted_by, days=days)

    if row:
        meta = dict(row["metadata"] or {})
        billing = dict(meta.get("billing") or {})
        billing["partner_testing_override"] = blob
        meta["billing"] = billing
        cur.execute(
            """
            UPDATE tenant_settings
            SET metadata = %s::jsonb,
                updated_at = NOW()
            WHERE tenant_id = %s
            RETURNING id
            """,
            (json.dumps(meta), TENANT_ID),
        )
        action = "updated"
    else:
        meta = {"billing": {"partner_testing_override": blob}}
        cur.execute(
            """
            INSERT INTO tenant_settings
                (tenant_id, show_nahla_branding, branding_text, metadata, created_at, updated_at)
            VALUES (%s, TRUE, %s, %s::jsonb, NOW(), NOW())
            RETURNING id
            """,
            (TENANT_ID, "🐝 Powered by Nahla", json.dumps(meta)),
        )
        action = "inserted"

    cur.fetchone()
    return action


def fetch_override_json(cur) -> dict | None:
    cur.execute(
        """
        SELECT metadata->'billing'->'partner_testing_override' AS override
        FROM tenant_settings
        WHERE tenant_id = %s
        """,
        (TENANT_ID,),
    )
    row = cur.fetchone()
    if not row:
        return None
    val = row["override"]
    if isinstance(val, str):
        return json.loads(val)
    return val


def verify_sql(cur) -> None:
    """DB-level verification — works before billing_override code is deployed."""
    print("\n" + "=" * 70)
    print("BILLING VERIFICATION (SQL / metadata)")
    print("=" * 70)

    def _override_active(tid: int) -> bool:
        if tid != TENANT_ID:
            return False
        blob = fetch_override_json(cur)
        if not blob or not blob.get("enabled"):
            return False
        exp = blob.get("expires_at", "")
        if exp:
            try:
                dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt <= datetime.now(timezone.utc):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _has_active_sub(tid: int) -> bool:
        cur.execute(
            """
            SELECT 1 FROM billing_subscriptions
            WHERE tenant_id = %s AND status = 'active'
              AND (ends_at IS NULL OR ends_at > NOW())
            LIMIT 1
            """,
            (tid,),
        )
        return cur.fetchone() is not None

    def _trial_active(tid: int) -> bool:
        cur.execute(
            "SELECT trial_ends_at FROM tenants WHERE id = %s",
            (tid,),
        )
        row = cur.fetchone()
        if not row or not row.get("trial_ends_at"):
            return False
        te = row["trial_ends_at"]
        if te.tzinfo is None:
            te = te.replace(tzinfo=timezone.utc)
        return te > datetime.now(timezone.utc)

    for tid, label in (
        (TENANT_ID, "tenant 1 (partner)"),
        (CONTROL_TENANT_ID, "tenant 2 (control)"),
    ):
        oa = _override_active(tid)
        sub = _has_active_sub(tid)
        trial = _trial_active(tid)
        access = sub or trial or oa
        print(f"\n--- {label} ---")
        print(f"  metadata override active:  {oa}")
        print(f"  active billing_sub:        {sub}")
        print(f"  trial window active:       {trial}")
        print(f"  effective_access (SQL):    {access}")

    oa1 = _override_active(TENANT_ID)
    print("\n--- tenant 1 expected /billing/status fields (after code deploy) ---")
    print(f"  partner_testing_override_active:       {oa1}")
    print(f"  partner_testing_override_headline_ar:  {'وضع اختبار سلة مفعل لهذا المتجر' if oa1 else None}")
    print(f"  partner_testing_override_plan_slug:    {'scale' if oa1 else None}")
    print(f"  ai_auto_replies_allowed:               {oa1}")
    print(f"  entitlements.plan_slug:                {'scale' if oa1 else '(normal)'}")


def verify_with_app(cur) -> None:
    """Use deployed billing modules when available."""
    print("\n" + "=" * 70)
    print("BILLING VERIFICATION (application layer)")
    print("=" * 70)

    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(DATABASE_URL)
        Session = sessionmaker(bind=engine)
        db = Session()

        from core.billing import has_billing_access
        from core.billing_override import (
            is_partner_testing_override_active,
        )
        from core.plan_entitlements import get_entitlements
        from core.trial_lifecycle import build_billing_status_payload

        try:
            from models import Tenant
        except ImportError:
            from database.models import Tenant  # type: ignore

        for tid, label in ((TENANT_ID, "tenant 1 (partner)"), (CONTROL_TENANT_ID, "tenant 2 (control)")):
            tenant = db.query(Tenant).filter(Tenant.id == tid).first()
            if not tenant:
                print(f"\n--- {label}: tenant row missing ---")
                continue

            access = has_billing_access(db, tid)
            ent = get_entitlements(db, tid)
            override_active = is_partner_testing_override_active(db, tid)

            print(f"\n--- {label} ---")
            print(f"  has_billing_access:              {access}")
            print(f"  partner_testing_override_active: {override_active}")
            print(f"  entitlements.plan_slug:          {ent.plan_slug}")
            print(f"  entitlements.is_active:          {ent.is_active}")
            print(f"  entitlements.is_blocked:         {ent.is_blocked}")

        tenant1 = db.query(Tenant).filter(Tenant.id == TENANT_ID).one()
        try:
            from core.wa_usage import get_current_period_usage

            usage = get_current_period_usage(db, TENANT_ID)
        except Exception as exc:
            print(f"\n  usage lookup failed (non-fatal): {exc}")
            usage = {
                "conversations_used": 0,
                "conversations_limit": -1,
                "usage_pct": 0,
                "exceeded": False,
            }

        from core.billing import get_tenant_subscription, INTEGRATION_FEE_SAR

        status = build_billing_status_payload(
            db,
            TENANT_ID,
            tenant1,
            active_sub=get_tenant_subscription(db, TENANT_ID),
            conversations_used=usage.get("conversations_used", 0),
            usage_data=usage,
            integration_fee_sar=INTEGRATION_FEE_SAR,
        )

        print("\n--- tenant 1 /billing/status key fields ---")
        keys = (
            "ai_auto_replies_allowed",
            "campaigns_automations_allowed",
            "partner_testing_override_active",
            "partner_testing_override_headline_ar",
            "partner_testing_override_plan_slug",
            "partner_testing_override_reason",
            "partner_testing_override_expires_at",
            "lifecycle_status",
            "subscription_expired",
            "plan",
        )
        for key in keys:
            val = status.get(key)
            if key == "plan" and isinstance(val, dict):
                print(f"  {key}.slug: {val.get('slug')}")
            else:
                print(f"  {key}: {val}")

        db.close()
        engine.dispose()
    except ImportError as exc:
        print(f"  SKIP: billing override modules not deployed yet ({exc})")
        print("  Metadata was written; deploy billing_override code then re-run --verify-only")
    except Exception as exc:
        print(f"  VERIFY ERROR: {exc}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Enable partner testing billing override")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--granted-by", default="ops")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()

    conn, cur = _connect()
    try:
        if not args.verify_only:
            print("=" * 70)
            print("ENABLE partner_testing_override (tenant_id=1)")
            print("=" * 70)
            action = upsert_override(cur, granted_by=args.granted_by, days=args.days)
            conn.commit()
            print(f"\nrows {action}: 1 (tenant_settings tenant_id={TENANT_ID})")

        override = fetch_override_json(cur)
        print("\n--- final metadata.billing.partner_testing_override ---")
        print(json.dumps(override, indent=2, ensure_ascii=False, default=str))

        verify_sql(cur)
        verify_with_app(cur)
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
