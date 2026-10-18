"""Read-only, closed observation of the0126 quarantine ownership surface."""
from sqlalchemy import text


def observe(conn, gate):
    def rows(sql):
        return [list(row) for row in conn.execute(text(sql)).fetchall()]
    relations = rows("""SELECT n.nspname,c.relname,c.relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
        AND (c.relname LIKE 'coupon_identity_%' OR c.relname LIKE '%coupon_retirement%')
        ORDER BY n.nspname,c.relname""")
    functions = rows("""SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid),
        pg_get_functiondef(p.oid),l.lanname,p.provolatile,p.proparallel,p.prosecdef,
        p.proleakproof,p.proisstrict,p.proconfig,p.proacl::text,
        p.proowner=(SELECT relowner FROM pg_class WHERE oid='public.coupons'::regclass),
        p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_language l ON l.oid=p.prolang
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
        AND (p.proname LIKE 'guard_coupon_retirement%' OR p.oid IN (
          SELECT tgfoid FROM pg_trigger WHERE NOT tgisinternal AND
          (tgrelid='public.coupons'::regclass OR tgrelid=to_regclass('public.coupon_identity_retirements'))))
        ORDER BY n.nspname,p.proname,p.oid""")
    triggers = rows("""SELECT n.nspname,c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid),
        pn.nspname,p.proname,pg_get_function_identity_arguments(p.oid)
        FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_proc p ON p.oid=t.tgfoid
        JOIN pg_namespace pn ON pn.oid=p.pronamespace
        WHERE NOT t.tgisinternal AND (c.oid='public.coupons'::regclass OR t.tgname LIKE '%coupon_retirement%'
          OR c.relname LIKE 'coupon_identity_%' OR p.proname LIKE 'guard_coupon_retirement%')
        ORDER BY n.nspname,c.relname,t.tgname""")
    exists = conn.execute(text("SELECT to_regclass('public.coupon_identity_retirements')")).scalar()
    types = rows("""SELECT n.nspname,t.typname,t.typtype,c.relname
        FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        LEFT JOIN pg_class c ON c.oid=t.typrelid
        WHERE n.nspname NOT IN ('pg_catalog','information_schema')
        AND (t.typname LIKE 'coupon_identity_%' OR t.typname LIKE '\\_coupon_identity_%')
        ORDER BY n.nspname,t.typname""")
    security = rows("""SELECT n.nspname,c.relname,c.relrowsecurity,c.relforcerowsecurity,
        c.relpersistence,c.relowner=(SELECT relowner FROM pg_class WHERE oid='public.coupons'::regclass),
        c.relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.oid IN ('public.coupons'::regclass,to_regclass('public.coupon_identity_retirements'))
        ORDER BY n.nspname,c.relname""")
    policies = rows("""SELECT schemaname,tablename,policyname,permissive,roles::text,cmd,qual,with_check
        FROM pg_policies WHERE schemaname='public' AND tablename IN ('coupons','coupon_identity_retirements')
        ORDER BY schemaname,tablename,policyname""")
    return {"relations":relations,"functions":functions,"triggers":triggers,
            "types":types,"security":security,"policies":policies,
            "execution":[conn.execute(text('SHOW session_replication_role')).scalar(),
                         conn.execute(text('SHOW row_security')).scalar()],
            "table":gate(conn,"coupon_identity_retirements") if exists else None}


EMPTY = {"relations":[],"functions":[],"triggers":[],"types":[],"table":None}


def matches(actual, expected):
    # JSON makes array/tuple representation portable, never drops unknown objects.
    import json
    return json.dumps(actual,sort_keys=True) == json.dumps(expected,sort_keys=True)
