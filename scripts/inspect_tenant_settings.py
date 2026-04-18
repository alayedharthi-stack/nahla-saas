import os, psycopg2, psycopg2.extras, sys
sys.stdout.reconfigure(encoding='utf-8')
conn = psycopg2.connect(os.environ['DATABASE_URL'])
cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    SELECT column_name, data_type, is_nullable, column_default
    FROM information_schema.columns
    WHERE table_name='tenant_settings'
    ORDER BY ordinal_position
""")
for r in cur.fetchall():
    nn = '  NOT NULL' if r['is_nullable']=='NO' else ''
    d  = f'  DEFAULT={r["column_default"]}' if r['column_default'] else ''
    print(f"{r['column_name']} ({r['data_type']}){nn}{d}")
conn.close()
