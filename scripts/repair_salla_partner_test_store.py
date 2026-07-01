"""
scripts/repair_salla_partner_test_store.py
──────────────────────────────────────────
Production data repair for the Salla Partners test store tenant mapping.

Run ONLY after the platform code fix is deployed:
  railway run --service nahla-saas python -X utf8 scripts/repair_salla_partner_test_store.py

Steps:
  1. Snapshot affected integrations / users / tenants to JSON files
  2. Merge newest valid Salla tokens into canonical integration (tenant 1)
  3. Set external_store_id=22825873, salla_merchant_id_alt=1979048767
  4. Repair owner user cgcaqkpx5wgewsyv@email.partners → tenant 1
  5. Disable duplicate integrations (after snapshot)
  6. Report orphan tenants — does NOT auto-delete tenants
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)

# ── Canonical target for the partner test store ─────────────────────────────

CANONICAL_STORE_ID   = "22825873"
ALT_MERCHANT_ID      = "1979048767"
CANONICAL_TENANT_ID  = 1
OWNER_EMAIL          = "cgcaqkpx5wgewsyv@email.partners"
STORE_NAME_HINT      = "nahlah ai honey"
LOOKUP_STORE_IDS     = (CANONICAL_STORE_ID, ALT_MERCHANT_ID)

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _write_snapshot(name: str, rows: list) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{_now_tag()}_{name}.json"
    path.write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    print(f"  snapshot → {path}")
    return path


def _fetch_integrations(cur) -> list:
    cur.execute(
        """
        SELECT i.*, t.name AS tenant_name
        FROM integrations i
        LEFT JOIN tenants t ON t.id = i.tenant_id
        WHERE i.provider = 'salla'
          AND (
            i.external_store_id = ANY(%s)
            OR i.config->>'store_id' = ANY(%s)
            OR i.config->>'salla_merchant_id_alt' = ANY(%s)
            OR i.config->>'merchant_id' = ANY(%s)
            OR lower(i.config->>'salla_owner_email') = %s
            OR lower(i.config->>'store_name') LIKE %s
          )
        ORDER BY i.tenant_id, i.id
        """,
        (
            list(LOOKUP_STORE_IDS),
            list(LOOKUP_STORE_IDS),
            list(LOOKUP_STORE_IDS),
            list(LOOKUP_STORE_IDS),
            OWNER_EMAIL.lower(),
            f"%{STORE_NAME_HINT}%",
        ),
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_users(cur, tenant_ids: list[int]) -> list:
    if not tenant_ids:
        return []
    cur.execute(
        """
        SELECT id, username, email, role, tenant_id, is_active
        FROM users
        WHERE tenant_id = ANY(%s) OR email = %s
        ORDER BY tenant_id, id
        """,
        (tenant_ids, OWNER_EMAIL),
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_tenants(cur, tenant_ids: list[int]) -> list:
    if not tenant_ids:
        return []
    cur.execute(
        "SELECT id, name, domain, is_active FROM tenants WHERE id = ANY(%s) ORDER BY id",
        (tenant_ids,),
    )
    return [dict(r) for r in cur.fetchall()]


def _token_score(cfg: dict) -> int:
    score = 0
    if cfg.get("refresh_token"):
        score += 100
    if cfg.get("api_key"):
        score += 10
    if cfg.get("connected_at") or cfg.get("last_seen"):
        score += 1
    return score


def _merge_tokens(target: dict, source: dict) -> None:
    for key in (
        "api_key", "refresh_token", "token_type", "expires_in",
        "api_key_source", "api_key_received_at", "connected_at",
        "token_source", "easy_mode", "api_sync_enabled", "api_canonical",
    ):
        if source.get(key) and _token_score({key: source[key]}) >= 0:
            if key in ("api_key", "refresh_token"):
                if source.get(key) and (
                    not target.get(key) or _token_score(source) > _token_score(target)
                ):
                    target[key] = source[key]
            elif not target.get(key):
                target[key] = source[key]


def main() -> None:
    print("=" * 70)
    print("Salla partner test-store repair")
    print("=" * 70)

    conn, cur = _connect()
    try:
        integrations = _fetch_integrations(cur)
        tenant_ids = sorted({r["tenant_id"] for r in integrations})
        users = _fetch_users(cur, tenant_ids)
        tenants = _fetch_tenants(cur, tenant_ids)

        print(f"\nFound {len(integrations)} integration(s), {len(users)} user(s), "
              f"{len(tenants)} tenant(s)")

        _write_snapshot("integrations_before", integrations)
        _write_snapshot("users_before", users)
        _write_snapshot("tenants_before", tenants)

        if not integrations:
            print("\nNothing to repair — no matching integrations found.")
            conn.rollback()
            return

        # Pick best token source across all duplicate rows
        best_cfg: dict = {}
        for row in integrations:
            cfg = dict(row.get("config") or {})
            if _token_score(cfg) > _token_score(best_cfg):
                best_cfg = cfg
        for row in integrations:
            _merge_tokens(best_cfg, dict(row.get("config") or {}))

        best_cfg["store_id"] = CANONICAL_STORE_ID
        best_cfg["salla_merchant_id_alt"] = ALT_MERCHANT_ID
        best_cfg["merchant_id"] = ALT_MERCHANT_ID
        best_cfg["salla_owner_email"] = OWNER_EMAIL
        if not best_cfg.get("store_name"):
            best_cfg["store_name"] = "Nahlah Ai honey"

        # Canonical integration on tenant 1
        cur.execute(
            """
            SELECT id, tenant_id, config, enabled
            FROM integrations
            WHERE provider = 'salla' AND tenant_id = %s
            ORDER BY id
            LIMIT 1
            """,
            (CANONICAL_TENANT_ID,),
        )
        canonical_row = cur.fetchone()

        if canonical_row:
            cur.execute(
                """
                UPDATE integrations
                SET external_store_id = %s,
                    enabled = TRUE,
                    config = %s::jsonb
                WHERE id = %s
                RETURNING id, tenant_id, external_store_id, enabled
                """,
                (CANONICAL_STORE_ID, json.dumps(best_cfg), canonical_row["id"]),
            )
            updated = cur.fetchone()
            print(f"\n[1] Updated canonical integration id={updated['id']} "
                  f"tenant={updated['tenant_id']} ext={updated['external_store_id']}")
        else:
            cur.execute(
                """
                INSERT INTO integrations
                    (tenant_id, provider, external_store_id, config, enabled)
                VALUES (%s, 'salla', %s, %s::jsonb, TRUE)
                RETURNING id, tenant_id, external_store_id, enabled
                """,
                (CANONICAL_TENANT_ID, CANONICAL_STORE_ID, json.dumps(best_cfg)),
            )
            updated = cur.fetchone()
            print(f"\n[1] Created canonical integration id={updated['id']} "
                  f"tenant={updated['tenant_id']}")

        # Owner user → tenant 1
        cur.execute(
            "SELECT id, tenant_id FROM users WHERE email = %s",
            (OWNER_EMAIL,),
        )
        owner = cur.fetchone()
        if owner and owner["tenant_id"] != CANONICAL_TENANT_ID:
            cur.execute(
                "UPDATE users SET tenant_id = %s WHERE id = %s RETURNING id, tenant_id",
                (CANONICAL_TENANT_ID, owner["id"]),
            )
            moved = cur.fetchone()
            print(f"[2] Moved owner user id={moved['id']} → tenant {moved['tenant_id']}")
        elif owner:
            print(f"[2] Owner user id={owner['id']} already on tenant {CANONICAL_TENANT_ID}")
        else:
            print(f"[2] WARNING: owner user {OWNER_EMAIL} not found — create manually if needed")

        # Disable duplicate integrations (not tenant 1 canonical row)
        canonical_id = updated["id"]
        dup_ids = [
            r["id"] for r in integrations
            if r["id"] != canonical_id
        ]
        if dup_ids:
            cur.execute(
                """
                UPDATE integrations
                SET enabled = FALSE,
                    config = config || jsonb_build_object(
                        'revoked_reason', %s,
                        'disabled_reason', 'partner_test_store_repair',
                        'disabled_at', %s
                    )
                WHERE id = ANY(%s)
                RETURNING id, tenant_id, external_store_id
                """,
                (
                    f"merged into integration {canonical_id} on tenant {CANONICAL_TENANT_ID}",
                    datetime.now(timezone.utc).isoformat(),
                    dup_ids,
                ),
            )
            disabled = cur.fetchall()
            print(f"[3] Disabled {len(disabled)} duplicate integration(s):")
            for d in disabled:
                print(f"      id={d['id']} tenant={d['tenant_id']} ext={d['external_store_id']}")
        else:
            print("[3] No duplicate integrations to disable")

        conn.commit()
        print("\nCOMMIT OK")

        # Post-repair verification (read-only)
        after_integrations = _fetch_integrations(cur)
        _write_snapshot("integrations_after", after_integrations)

        orphan_tenant_ids = sorted({
            r["tenant_id"] for r in after_integrations
            if r["tenant_id"] != CANONICAL_TENANT_ID and r.get("enabled")
        })
        if orphan_tenant_ids:
            print("\n⚠️  Orphan tenants still have ENABLED integrations:")
            for tid in orphan_tenant_ids:
                print(f"    tenant_id={tid} — review before deletion")
        else:
            print("\n✓ No enabled integrations remain outside tenant 1 for this store")

        print("\nTenant deletion is NOT performed by this script.")
        print("Review orphan tenants manually for real merchant data before deleting.")

    except Exception as exc:
        conn.rollback()
        print(f"\nFAILED — rolled back: {exc}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
