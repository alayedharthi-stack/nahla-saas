import os, psycopg2, psycopg2.extras, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=== Products (first 5) ===")
cur.execute("SELECT title, price, in_stock FROM products WHERE tenant_id=1 LIMIT 5")
for r in cur.fetchall():
    print(f"  {r['title']} | {r['price']} | in_stock={r['in_stock']}")

print("\n=== Active Coupons (first 5 of active) ===")
cur.execute("""
    SELECT code, discount_type, discount_value
    FROM coupons
    WHERE tenant_id=1
      AND (expires_at IS NULL OR expires_at > NOW())
    LIMIT 5
""")
for r in cur.fetchall():
    print(f"  {r['code']} | {r['discount_type']} {r['discount_value']}")

cur.execute("""
    SELECT COUNT(*) AS c FROM coupons WHERE tenant_id=1
      AND (expires_at IS NULL OR expires_at > NOW())
""")
print(f"\nTotal active coupons: {cur.fetchone()['c']}")

conn.close()
