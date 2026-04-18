"""
Fix tenant 1 issues:
 1. Clean up duplicate Salla-generated user
 2. Re-enable integration (clear needs_reauth for fresh OAuth)
 3. Generate the Salla re-authorization URL
"""
import os, sys, psycopg2, psycopg2.extras, urllib.parse

sys.stdout.reconfigure(encoding='utf-8')

conn = psycopg2.connect(os.environ['DATABASE_URL'])
conn.autocommit = False
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 60)
print("TENANT 1 AUDIT")
print("=" * 60)

cur.execute("SELECT id, username, email, role FROM users WHERE tenant_id=1 ORDER BY id")
users = cur.fetchall()
print("\nUsers in tenant 1:")
for u in users:
    print(f"  id={u['id']} email={u['email']} role={u['role']}")

cur.execute("""
    SELECT id, enabled, external_store_id,
           config->>'needs_reauth' AS needs_reauth,
           (config->>'access_token') IS NOT NULL AS has_token
    FROM integrations WHERE tenant_id=1 AND provider='salla'
""")
integ = cur.fetchone()
print(f"\nSalla integration: {dict(integ) if integ else 'NOT FOUND'}")

# Fix 1: Remove the Salla auto-generated email user
salla_user = next((u for u in users if 'email.partners' in (u['email'] or '')), None)

if salla_user:
    uid = salla_user['id']
    print(f"\nREMOVING Salla-generated user id={uid} ({salla_user['email']})")
    safe = True
    for tbl, col in [
        ('conversations', 'id'),
        ('message_events', 'id'),
    ]:
        try:
            cur.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE {col}=%s", (uid,))
            count = cur.fetchone()['c']
            if count:
                print(f"  WARNING: {tbl} has {count} rows - skipping")
                safe = False
                break
        except Exception:
            pass

    if safe:
        cur.execute("DELETE FROM users WHERE id=%s AND tenant_id=1", (uid,))
        print(f"  Deleted user id={uid}")
    else:
        print("  Skipped deletion due to references")
else:
    print("\nNo Salla-generated user found")

# Fix 2: Prepare integration for re-authorization
if integ:
    print(f"\nResetting integration id={integ['id']} for re-authorization")
    cur.execute("""
        UPDATE integrations
        SET enabled = false,
            config  = config || '{"needs_reauth": false}'::jsonb
        WHERE id = %s AND tenant_id = 1
    """, (integ['id'],))
    print("  needs_reauth cleared (ready for new OAuth flow)")

conn.commit()
print("\nDB changes committed")

# Fix 3: Generate Salla re-authorization URL
client_id    = os.environ.get('SALLA_CLIENT_ID', '')
redirect_uri = os.environ.get('SALLA_REDIRECT_URI', 'https://api.nahlah.ai/oauth/salla/callback')

if client_id:
    params = urllib.parse.urlencode({
        'client_id':     client_id,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'offline_access',
        'state':         'tenant1_reauth',
    })
    auth_url = f"https://accounts.salla.sa/oauth2/auth?{params}"
    print("\n" + "=" * 60)
    print("SALLA RE-AUTH URL:")
    print("=" * 60)
    print(auth_url)
else:
    print("ERROR: SALLA_CLIENT_ID not set")

conn.close()
