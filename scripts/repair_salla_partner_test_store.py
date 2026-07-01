"""
scripts/repair_salla_partner_test_store.py
──────────────────────────────────────────
Production data repair for the Salla Partners test store tenant mapping.

Run ONLY after the platform code fix is deployed:
  railway ssh --service nahla-saas python3 scripts/repair_salla_partner_test_store.py

Strategy:
  Move the row that already owns external_store_id=22825873 to tenant 1.
  Never blind-UPDATE a different row to that external_store_id (UNIQUE conflict).
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

CANONICAL_STORE_ID  = "22825873"
ALT_MERCHANT_ID     = "1979048767"
CANONICAL_TENANT_ID = 1
OWNER_EMAIL         = "cgcaqkpx5wgewsyv@email.partners"
DERIVED_EMAIL       = "store-22825873@salla-merchant.nahlah.ai"
STORE_NAME          = "Nahlah Ai honey"
USER_OWNER_ID       = 15
USER_DERIVED_ID     = 16

LOOKUP_STORE_IDS = (CANONICAL_STORE_ID, ALT_MERCHANT_ID)
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
DISABLED_AT = datetime.now(timezone.utc).isoformat()


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
    print(f"  snapshot -> {path}")
    return path


def _fetch_related_integrations(cur) -> list:
    cur.execute(
        """
        SELECT i.id, i.tenant_id, t.name AS tenant_name, i.enabled,
               i.external_store_id, i.config
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
            OR i.tenant_id = %s
          )
        ORDER BY i.enabled DESC, i.tenant_id, i.id
        """,
        (
            list(LOOKUP_STORE_IDS),
            list(LOOKUP_STORE_IDS),
            list(LOOKUP_STORE_IDS),
            list(LOOKUP_STORE_IDS),
            OWNER_EMAIL.lower(),
            "%nahlah ai honey%",
            CANONICAL_TENANT_ID,
        ),
    )
    return [dict(r) for r in cur.fetchall()]


def _fetch_target_users(cur) -> list:
    cur.execute(
        """
        SELECT id, email, tenant_id, role, created_at
        FROM users
        WHERE id IN (%s, %s)
           OR email IN (%s, %s)
        ORDER BY id
        """,
        (USER_OWNER_ID, USER_DERIVED_ID, OWNER_EMAIL, DERIVED_EMAIL),
    )
    return [dict(r) for r in cur.fetchall()]


def _find_canonical_integration(cur) -> dict | None:
    cur.execute(
        """
        SELECT id, tenant_id, enabled, external_store_id, config
        FROM integrations
        WHERE provider = 'salla'
          AND external_store_id = %s
        ORDER BY enabled DESC, id
        LIMIT 1
        """,
        (CANONICAL_STORE_ID,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _merge_required_config(cfg: dict) -> dict:
    merged = dict(cfg or {})
    merged["store_id"] = CANONICAL_STORE_ID
    merged["salla_merchant_id_alt"] = ALT_MERCHANT_ID
    merged["merchant_id"] = ALT_MERCHANT_ID
    merged["salla_owner_email"] = OWNER_EMAIL
    if not (merged.get("store_name") or "").strip():
        merged["store_name"] = STORE_NAME
    return merged


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
        if key not in source or not source.get(key):
            continue
        if key in ("api_key", "refresh_token"):
            if not target.get(key) or _token_score(source) > _token_score(target):
                target[key] = source[key]
        elif not target.get(key):
            target[key] = source[key]


def _disable_integration(cur, integration_id: int, reason: str) -> dict | None:
    cur.execute(
        """
        UPDATE integrations
        SET enabled = FALSE,
            config = config || jsonb_build_object(
                'disabled_reason', %s,
                'disabled_at', %s
            )
        WHERE id = %s
          AND enabled IS DISTINCT FROM FALSE
        RETURNING id, tenant_id, external_store_id, enabled
        """,
        (reason, DISABLED_AT, integration_id),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _print_final_verification(cur, canonical_id: int) -> None:
    print("\n" + "=" * 70)
    print("FINAL VERIFICATION")
    print("=" * 70)

    cur.execute(
        """
        SELECT id, tenant_id, enabled, external_store_id,
               config->>'store_id' AS store_id,
               config->>'store_name' AS store_name,
               config->>'salla_owner_email' AS owner_email,
               config->>'salla_merchant_id_alt' AS alt_id,
               config->>'merchant_id' AS merchant_id
        FROM integrations
        WHERE id = %s
        """,
        (canonical_id,),
    )
    print("\n--- canonical integration ---")
    row = cur.fetchone()
    print(dict(row) if row else "MISSING")

    cur.execute(
        """
        SELECT id, tenant_id, enabled, external_store_id,
               config->>'store_id' AS store_id,
               config->>'disabled_reason' AS disabled_reason
        FROM integrations
        WHERE provider = 'salla' AND tenant_id = %s
        ORDER BY enabled DESC, id
        """,
        (CANONICAL_TENANT_ID,),
    )
    print("\n--- all Salla integrations on tenant 1 ---")
    for r in cur.fetchall():
        print(dict(r))

    cur.execute(
        """
        SELECT id, email, tenant_id, role, created_at
        FROM users
        WHERE id IN (%s, %s)
        ORDER BY id
        """,
        (USER_OWNER_ID, USER_DERIVED_ID),
    )
    print("\n--- users 15 and 16 ---")
    for r in cur.fetchall():
        print(dict(r))


def main() -> None:
    print("=" * 70)
    print("Salla partner test-store repair (v2 — move canonical row)")
    print("=" * 70)

    conn, cur = _connect()
    try:
        integrations = _fetch_related_integrations(cur)
        users = _fetch_target_users(cur)

        print(f"\nFound {len(integrations)} related integration(s), "
              f"{len(users)} target user(s)")

        _write_snapshot("integrations_before", integrations)
        _write_snapshot("users_before", users)

        canonical = _find_canonical_integration(cur)
        if not canonical:
            print("\nERROR: No integration with external_store_id="
                  f"{CANONICAL_STORE_ID}. Manual investigation required.")
            conn.rollback()
            sys.exit(1)

        canonical_id = canonical["id"]
        print(f"\n[1] Canonical integration id={canonical_id} "
              f"tenant={canonical['tenant_id']} "
              f"ext={canonical['external_store_id']} enabled={canonical['enabled']}")

        merged_cfg = _merge_required_config(dict(canonical.get("config") or {}))
        for row in integrations:
            if row["id"] != canonical_id:
                _merge_tokens(merged_cfg, dict(row.get("config") or {}))

        if canonical["tenant_id"] != CANONICAL_TENANT_ID:
            print(f"    Moving integration id={canonical_id} "
                  f"tenant {canonical['tenant_id']} -> {CANONICAL_TENANT_ID}")
        else:
            print(f"    Integration id={canonical_id} already on tenant "
                  f"{CANONICAL_TENANT_ID}")

        cur.execute(
            """
            UPDATE integrations
            SET tenant_id = %s,
                external_store_id = %s,
                enabled = TRUE,
                config = %s::jsonb
            WHERE id = %s
            RETURNING id, tenant_id, external_store_id, enabled
            """,
            (
                CANONICAL_TENANT_ID,
                CANONICAL_STORE_ID,
                json.dumps(merged_cfg),
                canonical_id,
            ),
        )
        updated = cur.fetchone()
        print(f"    OK id={updated['id']} tenant={updated['tenant_id']} "
              f"ext={updated['external_store_id']} enabled={updated['enabled']}")

        cur.execute(
            """
            SELECT id, tenant_id, external_store_id, enabled
            FROM integrations
            WHERE provider = 'salla'
              AND tenant_id = %s
              AND id != %s
            """,
            (CANONICAL_TENANT_ID, canonical_id),
        )
        tenant1_orphans = cur.fetchall()
        if tenant1_orphans:
            print(f"\n[2] Disabling {len(tenant1_orphans)} orphan Salla "
                  f"integration(s) on tenant {CANONICAL_TENANT_ID}:")
            for orphan in tenant1_orphans:
                result = _disable_integration(
                    cur, orphan["id"], "partner_test_store_repair_orphan",
                )
                if result:
                    print(f"      disabled id={result['id']} "
                          f"ext={result['external_store_id']}")
                else:
                    print(f"      already disabled id={orphan['id']}")
        else:
            print(f"\n[2] No orphan Salla integrations on tenant "
                  f"{CANONICAL_TENANT_ID}")

        cur.execute(
            """
            SELECT id, tenant_id, external_store_id, enabled
            FROM integrations
            WHERE provider = 'salla'
              AND id != %s
              AND (
                external_store_id = %s
                OR config->>'store_id' = %s
                OR config->>'salla_merchant_id_alt' = %s
                OR config->>'merchant_id' = %s
              )
            """,
            (
                canonical_id,
                ALT_MERCHANT_ID,
                ALT_MERCHANT_ID,
                ALT_MERCHANT_ID,
                ALT_MERCHANT_ID,
            ),
        )
        alt_dupes = cur.fetchall()
        if alt_dupes:
            print(f"\n[3] Disabling {len(alt_dupes)} duplicate row(s) for "
                  f"alt id {ALT_MERCHANT_ID}:")
            for dup in alt_dupes:
                result = _disable_integration(
                    cur, dup["id"], "partner_test_store_repair_alt_duplicate",
                )
                if result:
                    print(f"      disabled id={result['id']} "
                          f"tenant={result['tenant_id']} "
                          f"ext={result['external_store_id']}")
                else:
                    print(f"      already disabled id={dup['id']}")
        else:
            print(f"\n[3] No duplicate integrations for alt id "
                  f"{ALT_MERCHANT_ID}")

        for label, user_id, email in (
            ("owner", USER_OWNER_ID, OWNER_EMAIL),
            ("derived", USER_DERIVED_ID, DERIVED_EMAIL),
        ):
            cur.execute(
                "SELECT id, tenant_id, email FROM users WHERE id = %s",
                (user_id,),
            )
            user = cur.fetchone()
            if not user:
                print(f"\n[4] WARNING: {label} user id={user_id} not found")
                continue
            if user["tenant_id"] == CANONICAL_TENANT_ID:
                print(f"\n[4] {label.capitalize()} user id={user_id} "
                      f"({user['email']}) already on tenant "
                      f"{CANONICAL_TENANT_ID}")
            else:
                cur.execute(
                    """
                    UPDATE users
                    SET tenant_id = %s
                    WHERE id = %s
                    RETURNING id, email, tenant_id
                    """,
                    (CANONICAL_TENANT_ID, user_id),
                )
                moved = cur.fetchone()
                print(f"\n[4] Moved {label} user id={moved['id']} "
                      f"({moved['email']}) -> tenant {moved['tenant_id']}")

        conn.commit()
        print("\nCOMMIT OK")

        _print_final_verification(cur, canonical_id)
        _write_snapshot("integrations_after", _fetch_related_integrations(cur))
        _write_snapshot("users_after", _fetch_target_users(cur))

        print("\nTenant deletion is NOT performed by this script.")

    except Exception as exc:
        conn.rollback()
        print(f"\nFAILED — rolled back: {exc}")
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
