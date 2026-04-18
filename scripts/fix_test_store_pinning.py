"""
Fix script: Pin Salla test store (22825873) permanently to tenant_id=1
and clean up orphan duplicate tenants 49, 50, 51.

Each step commits independently so partial fixes are preserved.
"""
import os
import sys
import psycopg2
import psycopg2.extras

TEST_STORE_ID   = "22825873"
CANONICAL_TENANT = 1
ORPHAN_TENANTS  = (49, 50, 51)

DB_URL = os.environ.get("DATABASE_URL")
if not DB_URL:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)

def connect():
    c = psycopg2.connect(DB_URL)
    c.autocommit = False
    return c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Pin canonical integration — external_store_id + clear needs_reauth
# ─────────────────────────────────────────────────────────────────────────────
print("=" * 60)
print("STEP 1 — Pin integration tenant=1 <-> store=22825873")
print("=" * 60)
conn, cur = connect()
try:
    cur.execute("""
        UPDATE integrations
        SET
            external_store_id = %s,
            config = jsonb_strip_nulls(
                config
                || '{"needs_reauth": false}'::jsonb
                || '{"reauth_reason": null}'::jsonb
            )
        WHERE provider  = 'salla'
          AND tenant_id = %s
          AND (config->>'store_id') = %s
        RETURNING id, tenant_id, external_store_id, enabled
    """, (TEST_STORE_ID, CANONICAL_TENANT, TEST_STORE_ID))
    rows = cur.fetchall()
    if rows:
        for r in rows:
            print(f"  UPDATED integration id={r['id']} tenant={r['tenant_id']} "
                  f"ext_store={r['external_store_id']} enabled={r['enabled']}")
    else:
        print("  WARNING: No integration matched — manual check needed")
    conn.commit()
    print("  Committed.")
except Exception as e:
    conn.rollback()
    print(f"  FAILED: {e}")
finally:
    cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Delete orphan integrations (no real tokens)
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("STEP 2 — Remove orphan integrations for tenants 49/50/51")
print("=" * 60)
conn, cur = connect()
try:
    cur.execute("""
        DELETE FROM integrations
        WHERE provider  = 'salla'
          AND tenant_id = ANY(%s)
        RETURNING id, tenant_id
    """, (list(ORPHAN_TENANTS),))
    deleted = cur.fetchall()
    for r in deleted:
        print(f"  DELETED integration id={r['id']} tenant={r['tenant_id']}")
    if not deleted:
        print("  Nothing to delete.")
    conn.commit()
    print("  Committed.")
except Exception as e:
    conn.rollback()
    print(f"  FAILED: {e}")
finally:
    cur.close(); conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Cascade-delete each orphan tenant with all dependencies
# Tables that ref tenants: users, tenant_settings, whatsapp_connections,
#   whatsapp_usage, campaigns, conversations, orders, products, etc.
# Safest: delete each dependency table row by row so FK errors are surfaced.
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("STEP 3 — Cascade-delete orphan tenants 49/50/51")
print("=" * 60)

DEPENDENT_TABLES = [
    "merchant_addons",
    "merchant_widgets",
    "whatsapp_connections",
    "whatsapp_usage",
    "tenant_settings",
    "automation_events",
    "webhook_events",
    "campaigns",
    "conversations",
    "coupon_events",
    "coupons",
    "orders",
    "customers",
    "products",
    "product_interests",
    "automation_rules",
    "conversation_traces",
    "integrity_events",
    "webhook_guardian_log",
    "users",
]

for tid in ORPHAN_TENANTS:
    conn, cur = connect()
    try:
        print(f"\n  -- Tenant {tid} --")
        # Get tenant name first
        cur.execute("SELECT name FROM tenants WHERE id=%s", (tid,))
        row = cur.fetchone()
        if not row:
            print(f"  Tenant {tid} not found — already gone.")
            conn.commit()
            cur.close(); conn.close()
            continue
        tenant_name = row["name"]

        for tbl in DEPENDENT_TABLES:
            try:
                cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s RETURNING id", (tid,))
                n = len(cur.fetchall())
                if n:
                    print(f"    Deleted {n} rows from {tbl}")
            except psycopg2.errors.UndefinedTable:
                conn.rollback()
                conn, cur = connect()  # reset connection after error
            except Exception as dep_e:
                print(f"    WARNING on {tbl}: {dep_e}")
                conn.rollback()
                conn, cur = connect()

        cur.execute("DELETE FROM tenants WHERE id=%s RETURNING id, name", (tid,))
        deleted = cur.fetchone()
        if deleted:
            print(f"  DELETED tenant id={tid} name={deleted['name']}")
        conn.commit()
        print(f"  Committed tenant {tid}.")
    except Exception as e:
        conn.rollback()
        print(f"  FAILED tenant {tid}: {e}")
    finally:
        try: cur.close()
        except: pass
        try: conn.close()
        except: pass


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Final verification
# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 60)
print("STEP 4 — Final verification")
print("=" * 60)
conn, cur = connect()
try:
    cur.execute("""
        SELECT i.id, i.tenant_id, i.enabled, i.external_store_id,
               i.config->>'store_id'     AS store_id_cfg,
               i.config->>'needs_reauth' AS needs_reauth,
               (i.config ? 'api_key' AND i.config->>'api_key' != '') AS has_token
        FROM integrations i
        WHERE i.provider = 'salla'
        ORDER BY i.tenant_id
    """)
    for r in cur.fetchall():
        status = "OK" if (r["external_store_id"] and r["enabled"] is not None) else "WARN"
        print(f"  [{status}] tenant={r['tenant_id']} ext={r['external_store_id']} "
              f"cfg={r['store_id_cfg']} enabled={r['enabled']} "
              f"needs_reauth={r['needs_reauth']} has_token={r['has_token']}")

    print()
    cur.execute("SELECT id, name FROM tenants WHERE id IN (49,50,51)")
    remaining = cur.fetchall()
    if remaining:
        print(f"  WARNING: Orphan tenants still exist: {remaining}")
    else:
        print("  Orphan tenants 49/50/51: all removed.")
    conn.commit()
except Exception as e:
    print(f"  Verification error: {e}")
finally:
    cur.close(); conn.close()

print()
print("Done.")
