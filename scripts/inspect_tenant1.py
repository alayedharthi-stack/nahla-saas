import os, psycopg2, psycopg2.extras
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute('SELECT id, name, domain FROM tenants WHERE id=1')
t = cur.fetchone()
print('TENANT 1:', dict(t) if t else 'NOT FOUND')

cur.execute("SELECT id, username, email, role FROM users WHERE tenant_id=1 ORDER BY id")
print('USERS:')
for r in cur.fetchall(): print(' ', dict(r))

cur.execute("""
    SELECT id, provider, external_store_id, enabled,
        config->>'store_id' AS store_id,
        config->>'needs_reauth' AS needs_reauth,
        (config->>'access_token') IS NOT NULL AS has_access_token,
        (config->>'refresh_token') IS NOT NULL AS has_refresh_token
    FROM integrations WHERE tenant_id=1 AND provider='salla'
""")
print('SALLA INTEGRATION:')
for r in cur.fetchall(): print(' ', dict(r))

for tbl in ['products', 'customers', 'orders', 'coupons']:
    cur.execute(f'SELECT COUNT(*) AS c FROM {tbl} WHERE tenant_id=1')
    print(f'{tbl}: {cur.fetchone()["c"]}')

# Last sync jobs
cur.execute("SELECT id, status, error_message, started_at, finished_at FROM store_sync_jobs WHERE tenant_id=1 ORDER BY started_at DESC LIMIT 3")
print('LAST SYNC JOBS:')
for r in cur.fetchall(): print(' ', dict(r))

conn.close()
