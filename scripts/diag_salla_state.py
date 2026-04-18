"""
Quick diagnostic: dump Salla integration state for all tenants.
Run with: python scripts/diag_salla_state.py
"""
import os
import sys
import json
import psycopg2
import psycopg2.extras

DB_URL = os.environ.get("DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
if not DB_URL:
    print("Pass DATABASE_URL env var or as argv[1]")
    sys.exit(1)

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("\n========== Salla Integrations ==========")
cur.execute("""
    SELECT i.id, i.tenant_id, t.name AS tenant_name, i.enabled,
           i.external_store_id, i.config
    FROM integrations i
    LEFT JOIN tenants t ON t.id = i.tenant_id
    WHERE i.provider = 'salla'
    ORDER BY i.tenant_id
""")
rows = cur.fetchall()
for r in rows:
    cfg = r["config"] or {}
    print(f"\n--- tenant_id={r['tenant_id']} ({r['tenant_name']}) ---")
    print(f"  integration_id   : {r['id']}")
    print(f"  enabled          : {r['enabled']}")
    print(f"  external_store_id: {r['external_store_id']}")
    print(f"  store_id (cfg)   : {cfg.get('store_id')}")
    print(f"  store_name       : {cfg.get('store_name')}")
    print(f"  has_api_key      : {bool(cfg.get('api_key'))}")
    print(f"  has_refresh      : {bool(cfg.get('refresh_token'))}")
    print(f"  needs_reauth     : {cfg.get('needs_reauth')}")
    print(f"  reauth_reason    : {cfg.get('reauth_reason')}")
    print(f"  app_type         : {cfg.get('app_type', 'production')}")
    print(f"  redirect_uri     : {cfg.get('redirect_uri')}")
    print(f"  connected_at     : {cfg.get('connected_at')}")

print("\n========== Tenant 1 Users ==========")
cur.execute("SELECT id, email, role, is_active, tenant_id FROM users WHERE tenant_id = 1")
for r in cur.fetchall():
    print(f"  user_id={r['id']} email={r['email']} role={r['role']} active={r['is_active']}")

cur.close()
conn.close()
