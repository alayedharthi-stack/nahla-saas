"""
scripts/fix_salla_integration_tenant.py — v2
─────────────────────────────────────────────
المشكلة المكتشفة من الـ logs:
  - سلة لا ترسل email في introspect → يُشتق store-1979048767@salla-merchant.nahlah.ai
  - Integration بـ store_id=1979048767 مسجّل في tenant 43 لا tenant 1
  - النتيجة: كل دخول من سلة يذهب إلى tenant 43

الحل:
  1. حذف الـ integration من tenant 43 أولاً (لتحرير unique constraint)
  2. تحديث integration tenant 1 بـ store_id الصحيح + salla_owner_email
"""
import json
import os
import sys

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)

import psycopg2
conn = psycopg2.connect(DATABASE_URL)
cur  = conn.cursor()

TARGET_STORE_ID   = "1979048767"
CORRECT_TENANT_ID = 1
CORRECT_EMAIL     = "cgcaqkpx5wgewsyv@email.partners"
WRONG_TENANT_ID   = 43

print("=" * 60)
print("BEFORE — current state")
print("=" * 60)

cur.execute("""
    SELECT id, tenant_id, external_store_id, enabled,
           config->>'store_id'          AS cfg_store_id,
           config->>'salla_owner_email' AS cfg_owner_email,
           config->>'api_key'           AS has_key
    FROM integrations
    WHERE provider = 'salla'
    ORDER BY tenant_id
""")
for r in cur.fetchall():
    has_key = "YES" if r[6] else "NO"
    print(f"  id={r[0]}  tenant={r[1]}  ext_store_id={r[2]}  enabled={r[3]}  "
          f"cfg_store_id={r[4]}  owner_email={r[5]}  api_key={has_key}")

# ── جمع بيانات الـ tokens من tenant 43 ────────────────────────────────
cur.execute("""
    SELECT id, config FROM integrations
    WHERE provider = 'salla'
      AND tenant_id = %s
      AND (external_store_id = %s OR config->>'store_id' = %s)
""", (WRONG_TENANT_ID, TARGET_STORE_ID, TARGET_STORE_ID))
row43 = cur.fetchone()

cur.execute("""
    SELECT id, config FROM integrations
    WHERE provider = 'salla'
      AND tenant_id = %s
""", (CORRECT_TENANT_ID,))
row1 = cur.fetchone()

if not row43:
    print(f"\nNOTHING to fix in tenant {WRONG_TENANT_ID} — possibly already fixed.")
    cur.close(); conn.close(); sys.exit(0)

id43, cfg43 = row43
cfg43 = cfg43 or {}

print(f"\nFix plan:")
print(f"  - Integration id={id43} (tenant {WRONG_TENANT_ID}) will be deleted")
if row1:
    id1, cfg1 = row1
    print(f"  - Integration id={id1} (tenant {CORRECT_TENANT_ID}) will be updated with store_id + tokens")
else:
    print(f"  - tenant {CORRECT_TENANT_ID} has no Salla integration — will create one")

confirm = input("\nApply? (yes/no): ").strip().lower()
if confirm not in ("yes", "y"):
    print("Aborted.")
    cur.close(); conn.close(); sys.exit(0)

print("\n" + "=" * 60)
print("APPLYING FIX")
print("=" * 60)

# ── STEP 1: حذف الـ integration الخاطئ من tenant 43 ──────────────────
# يجب الحذف أولاً لتحرير unique constraint على config->>'store_id'
cur.execute("DELETE FROM integrations WHERE id = %s", (id43,))
print(f"  [1/3] Deleted integration id={id43} from tenant {WRONG_TENANT_ID}")

# ── STEP 2: تحديث أو إنشاء integration في tenant 1 ────────────────────
merged_cfg: dict = {}
if row1:
    id1, cfg1 = row1
    merged_cfg = dict(cfg1 or {})

# نأخذ الـ tokens من tenant 43 (الأحدث) ونضبط الـ owner صح
for key in ["api_key", "refresh_token", "token_type", "expires_in", "store_name", "merchant_id"]:
    if cfg43.get(key):
        merged_cfg[key] = cfg43[key]

merged_cfg["store_id"]          = TARGET_STORE_ID
merged_cfg["salla_owner_email"] = CORRECT_EMAIL

if row1:
    cur.execute("""
        UPDATE integrations
        SET config            = %s,
            external_store_id = %s,
            enabled           = TRUE
        WHERE id = %s
    """, (json.dumps(merged_cfg), TARGET_STORE_ID, id1))
    print(f"  [2/3] Updated integration id={id1} (tenant {CORRECT_TENANT_ID}): "
          f"store_id={TARGET_STORE_ID}  salla_owner_email={CORRECT_EMAIL}")
else:
    cur.execute("""
        INSERT INTO integrations
               (tenant_id, provider, external_store_id, config, enabled)
        VALUES (%s, 'salla', %s, %s, TRUE)
    """, (CORRECT_TENANT_ID, TARGET_STORE_ID, json.dumps(merged_cfg)))
    print(f"  [2/3] Created new integration for tenant {CORRECT_TENANT_ID}: store_id={TARGET_STORE_ID}")

conn.commit()
print("\n  COMMIT SUCCESS")

# ── STEP 3: check user ────────────────────────────────────────────────
cur.execute(
    "SELECT id FROM users WHERE email = %s AND tenant_id = %s",
    (CORRECT_EMAIL, CORRECT_TENANT_ID)
)
u = cur.fetchone()
if u:
    print(f"  [3/3] User '{CORRECT_EMAIL}' found in tenant {CORRECT_TENANT_ID} id={u[0]} OK")
else:
    print(f"  [3/3] WARNING: No user '{CORRECT_EMAIL}' in tenant {CORRECT_TENANT_ID}!")

# ── AFTER ─────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("AFTER -- final state")
print("=" * 60)

cur.execute("""
    SELECT id, tenant_id, external_store_id, enabled,
           config->>'store_id'          AS cfg_store_id,
           config->>'salla_owner_email' AS cfg_owner_email,
           CASE WHEN (config->>'api_key') IS NOT NULL THEN 'YES' ELSE 'NO' END AS has_key
    FROM integrations
    WHERE provider = 'salla'
    ORDER BY tenant_id
""")
for r in cur.fetchall():
    print(f"  id={r[0]}  tenant={r[1]}  ext_store_id={r[2]}  enabled={r[3]}  "
          f"cfg_store_id={r[4]}  owner_email={r[5]}  api_key={r[6]}")

cur.close()
conn.close()

print("\n" + "=" * 60)
print("DONE. Next Salla login should route to tenant 1.")
print("=" * 60)
