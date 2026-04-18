"""
Get fresh Salla OAuth URL + show current token state for tenant 1.
Run: railway run --service nahla-saas python -X utf8 scripts/get_salla_reauth_url.py
"""
import os, sys, psycopg2, psycopg2.extras, urllib.parse
sys.stdout.reconfigure(encoding='utf-8')

conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

cur.execute("""
    SELECT id, tenant_id, enabled, external_store_id,
           config->>'access_token'      AS access_token,
           config->>'refresh_token'     AS refresh_token,
           config->>'salla_owner_email' AS owner_email,
           config->>'store_id'          AS store_id_cfg,
           config->>'needs_reauth'      AS needs_reauth_cfg
    FROM integrations WHERE tenant_id=1 AND provider='salla'
""")
row = cur.fetchone()
if not row:
    print("No Salla integration for tenant 1!")
    sys.exit(1)

print("=== Integration state ===")
print(f"  id           : {row['id']}")
print(f"  enabled      : {row['enabled']}")
print(f"  needs_reauth : {row['needs_reauth_cfg']}")
print(f"  store_id     : {row['external_store_id'] or row['store_id_cfg']}")
print(f"  owner_email  : {row['owner_email']}")
has_access  = bool(row['access_token'])
has_refresh = bool(row['refresh_token'])
print(f"  has_access_token  : {has_access}")
print(f"  has_refresh_token : {has_refresh}")

# Read client_id from env (set by Railway)
client_id    = os.getenv("SALLA_CLIENT_ID", "f0e12672-3682-4128-8846-2fa314cdd76b")
redirect_uri = "https://api.nahlah.ai/oauth/salla/callback"

params = urllib.parse.urlencode({
    "client_id":     client_id,
    "redirect_uri":  redirect_uri,
    "response_type": "code",
    "scope":         "offline_access",
    "state":         "tenant1_reauth",
})
url = f"https://accounts.salla.sa/oauth2/auth?{params}"
print("\n=== Re-authorization URL ===")
print(url)
print("\nOpen this URL in your browser, log in with your Salla account,")
print("approve permissions, and the new tokens will be saved automatically.")
conn.close()
