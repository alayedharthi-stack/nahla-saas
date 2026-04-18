"""
Fix user-tenant binding for tenant 1:
 - cgcaqkpx5wgewsyv@email.partners  = correct Salla user (keep in tenant 1)
 - eng.khaled03@gmail.com           = unrelated account (detach from tenant 1)

Run: railway run --service nahla-saas python -X utf8 scripts/fix_user_tenant_binding.py
"""
import os, sys, psycopg2, psycopg2.extras
sys.stdout.reconfigure(encoding='utf-8')

conn = psycopg2.connect(os.environ['DATABASE_URL'])
conn.autocommit = False
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("=" * 60)
print("USER-TENANT AUDIT — tenant 1")
print("=" * 60)

cur.execute("SELECT id, username, email, role FROM users WHERE tenant_id=1 ORDER BY id")
users = cur.fetchall()
for u in users:
    print(f"  id={u['id']}  email={u['email']}  role={u['role']}")

REAL_SALLA_EMAIL  = 'cgcaqkpx5wgewsyv@email.partners'
FRIEND_EMAIL      = 'eng.khaled03@gmail.com'

friend_user = next((u for u in users if u['email'] == FRIEND_EMAIL), None)
salla_user  = next((u for u in users if u['email'] == REAL_SALLA_EMAIL), None)

print(f"\nSalla user  : {salla_user['id'] if salla_user else 'NOT FOUND'}")
print(f"Friend user : {friend_user['id'] if friend_user else 'NOT FOUND'}")

if not friend_user:
    print("\nFriend user not found in tenant 1 — nothing to do.")
    conn.close()
    sys.exit(0)

fid = friend_user['id']

# Check if the friend user has any data in tenant 1
print(f"\nChecking data for friend user id={fid}...")
refs = {}
tables = {
    'conversations':     ('id',         None),
    'message_events':    ('id',         None),
    'orders':            ('tenant_id',  fid),
    'customers':         ('tenant_id',  fid),
}
# Check auth-related tables only
for tbl, (col, val) in tables.items():
    try:
        if val is None:
            cur.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE {col}=%s", (fid,))
        else:
            cur.execute(f"SELECT COUNT(*) AS c FROM {tbl} WHERE {col}=%s", (val,))
        refs[tbl] = cur.fetchone()['c']
    except Exception:
        refs[tbl] = 'N/A'

for tbl, cnt in refs.items():
    print(f"  {tbl}: {cnt} rows")

# The safest approach: move the friend user to a brand-new dedicated tenant
# so they can register properly later. We create a placeholder tenant for them.
print(f"\nCreating placeholder tenant for friend account ({FRIEND_EMAIL})...")

cur.execute("""
    INSERT INTO tenants (name, domain, is_active, is_platform_tenant)
    VALUES ('placeholder-eng-khaled', 'placeholder-khaled.nahla.sa', false, false)
    RETURNING id
""")
new_tenant_id = cur.fetchone()['id']
print(f"  Placeholder tenant created: id={new_tenant_id}")

cur.execute("""
    UPDATE users SET tenant_id = %s WHERE id = %s
""", (new_tenant_id, fid))
print(f"  User id={fid} moved to tenant {new_tenant_id}")

# Insert minimal tenant_settings for the new placeholder
cur.execute("""
    INSERT INTO tenant_settings (tenant_id, show_nahla_branding, branding_text)
    VALUES (%s, true, '')
    ON CONFLICT DO NOTHING
""", (new_tenant_id,))

# Mark Salla integration to record the correct owner email
cur.execute("""
    UPDATE integrations
    SET config = config || jsonb_build_object('salla_owner_email', %s)
    WHERE tenant_id = 1 AND provider = 'salla'
""", (REAL_SALLA_EMAIL,))
print(f"  Integration updated: salla_owner_email = {REAL_SALLA_EMAIL}")

conn.commit()

print("\n" + "=" * 60)
print("RESULT")
print("=" * 60)
cur.execute("SELECT id, email, tenant_id, role FROM users WHERE id IN (%s, %s)", (fid, salla_user['id'] if salla_user else 0))
for u in cur.fetchall():
    print(f"  id={u['id']}  email={u['email']}  tenant_id={u['tenant_id']}  role={u['role']}")

print("\nDone. eng.khaled03@gmail.com is now in placeholder tenant and can")
print("re-register independently when they create their own store.")
conn.close()
