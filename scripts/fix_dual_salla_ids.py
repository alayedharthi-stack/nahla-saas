"""
scripts/fix_dual_salla_ids.py
──────────────────────────────
المشكلة الجذرية:
  سلة تُعيد أحياناً merchant_id=22825873 وأحياناً merchant_id=1979048767
  كلاهما يشيران لنفس المتجر التجريبي (tenant 1)
  لكن النظام يعاملهما كمتجرين منفصلين فيُنشئ tenant 43 و 47

الحل:
  1. نضبط external_store_id=22825873 (المعرّف الرئيسي الحالي)
  2. نحفظ 1979048767 كـ salla_merchant_id_alt في config
  3. نحذف integration tenant 47 (المُنشأة بالخطأ)
  4. نُبقي tenant 43 فارغة أو نحذفها
"""
import json
import os
import sys

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set"); sys.exit(1)

import psycopg2
conn = psycopg2.connect(DATABASE_URL)
cur  = conn.cursor()

PRIMARY_STORE_ID   = "22825873"    # ما تُعيده سلة عبر embedded token الآن
ALT_MERCHANT_ID    = "1979048767"  # ما كانت تُعيده سابقاً (merchant account ID)
CORRECT_TENANT_ID  = 1
CORRECT_EMAIL      = "cgcaqkpx5wgewsyv@email.partners"

print("=" * 60)
print("BEFORE — current integrations")
print("=" * 60)
cur.execute("""
    SELECT id, tenant_id, external_store_id, enabled,
           config->>'store_id'           AS cfg_store_id,
           config->>'salla_owner_email'  AS owner,
           config->>'salla_merchant_id_alt' AS alt_id
    FROM integrations WHERE provider='salla' ORDER BY tenant_id
""")
for r in cur.fetchall():
    print(f"  id={r[0]}  tenant={r[1]}  ext_store_id={r[2]}  enabled={r[3]}  "
          f"cfg_store={r[4]}  owner={r[5]}  alt_id={r[6]}")

# ── STEP 1: حذف جميع integrations مكررة أولاً (يُحرّر unique constraint) ──
cur.execute("""
    DELETE FROM integrations
    WHERE provider='salla'
      AND tenant_id != %s
      AND (
          external_store_id IN (%s, %s)
          OR config->>'store_id' IN (%s, %s)
      )
""", (CORRECT_TENANT_ID, PRIMARY_STORE_ID, ALT_MERCHANT_ID,
      PRIMARY_STORE_ID, ALT_MERCHANT_ID))
deleted = cur.rowcount
print(f"\n[FIX 1] Deleted {deleted} duplicate integration(s) from wrong tenants")

# ── STEP 2: تحديث integration tenant 1 ──────────────────────────────
cur.execute("""
    SELECT id, config FROM integrations
    WHERE tenant_id=%s AND provider='salla'
""", (CORRECT_TENANT_ID,))
row1 = cur.fetchone()

if row1:
    id1, cfg1 = row1
    cfg1 = cfg1 or {}
    cfg1["store_id"]               = PRIMARY_STORE_ID
    cfg1["salla_owner_email"]      = CORRECT_EMAIL
    cfg1["salla_merchant_id_alt"]  = ALT_MERCHANT_ID  # يُستخدم في fallback lookup

    cur.execute("""
        UPDATE integrations
        SET external_store_id = %s,
            config = %s,
            enabled = TRUE
        WHERE id = %s
    """, (PRIMARY_STORE_ID, json.dumps(cfg1), id1))
    print(f"[FIX 2] Updated tenant 1 integration id={id1}: "
          f"store_id={PRIMARY_STORE_ID}  alt={ALT_MERCHANT_ID}  owner={CORRECT_EMAIL}")
else:
    print("ERROR: No integration found for tenant 1!")

conn.commit()
print("\n  COMMIT OK")

# ── 4. التحقق ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("AFTER — final state")
print("=" * 60)
cur.execute("""
    SELECT id, tenant_id, external_store_id, enabled,
           config->>'store_id'           AS cfg_store_id,
           config->>'salla_owner_email'  AS owner,
           config->>'salla_merchant_id_alt' AS alt_id
    FROM integrations WHERE provider='salla' ORDER BY tenant_id
""")
for r in cur.fetchall():
    print(f"  id={r[0]}  tenant={r[1]}  ext_store_id={r[2]}  enabled={r[3]}  "
          f"cfg_store={r[4]}  owner={r[5]}  alt_id={r[6]}")

cur.close(); conn.close()
print("\nDONE.")
