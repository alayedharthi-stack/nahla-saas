import os, psycopg2, psycopg2.extras, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT id, enabled, external_store_id,
           config->>'api_key'      AS ak,
           config->>'refresh_token' AS rt,
           config->>'needs_reauth' AS reauth,
           config->>'salla_owner_email' AS owner
    FROM integrations WHERE tenant_id=1 AND provider='salla'
""")
r = cur.fetchone()
if r:
    print(f"integration id  : {r['id']}")
    print(f"enabled         : {r['enabled']}")
    print(f"needs_reauth    : {r['reauth']}")
    print(f"owner email     : {r['owner']}")
    print(f"has api_key     : {bool(r['ak'])}  len={len(r['ak'] or '')}")
    print(f"has refresh_tok : {bool(r['rt'])}  len={len(r['rt'] or '')}")
else:
    print("No integration found!")

cur.execute("SELECT COUNT(*) AS c FROM products WHERE tenant_id=1")
print(f"\nproducts in DB  : {cur.fetchone()['c']}")
cur.execute("SELECT COUNT(*) AS c FROM coupons WHERE tenant_id=1")
print(f"coupons in DB   : {cur.fetchone()['c']}")
cur.execute("SELECT last_full_sync_at FROM store_knowledge_snapshots WHERE tenant_id=1")
snap = cur.fetchone()
print(f"last full sync  : {snap['last_full_sync_at'] if snap else 'no snapshot'}")
conn.close()
