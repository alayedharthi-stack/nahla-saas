"""Read-only production phase audit; no DDL, DML, app startup or approval mutation."""
from __future__ import annotations
import argparse,ast,base64,datetime,hashlib,json,os,runpy,sys,types
from pathlib import Path

R='a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec'
ROOT=Path('/app')
IDS={'RAILWAY_PROJECT_ID':'f0090862-0a40-4293-bd5d-e94df58762b5','RAILWAY_ENVIRONMENT_ID':'ede962ce-3042-4dae-94de-623837e83ed9','RAILWAY_SERVICE_ID':'686b36c5-a926-4e58-912a-5e9d13fbc2e7'}
FIXED=('requirements.txt','database/alembic.ini','scripts/operators/commerce_runtime_pilot_migration.py','scripts/operators/commerce_runtime_pilot_migration_contract.py','scripts/operators/shopify_release_gate.py','scripts/operators/shopify_release_gate_contract.py','scripts/operators/shopify_release_schema_expectations.json','scripts/operators/__init__.py','scripts/operators/bootstrap_migration_contract.py','backend/__init__.py','backend/payments/__init__.py','backend/payments/models.py','backend/services/shopify_connection/config.py','backend/services/shopify_catalog/config.py','backend/services/shopify_privacy/config.py')
def require(ok,reason):
    if not ok: raise RuntimeError(reason)
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()
parser=argparse.ArgumentParser()
parser.add_argument('--phase',required=True,choices=['S0','S1','S2','S3'])
parser.add_argument('--baseline-b64')
args=parser.parse_args()
out={'kind':'production_phase_audit','readonly':True,'phase':args.phase,'production_gate_pass':False}
try:
    require(all(os.environ.get(key)==value for key,value in IDS.items()),'platform_identity')
    require(os.environ.get('RAILWAY_GIT_COMMIT_SHA')==R,'deployed_source')
    paths=set(FIXED)|{p.relative_to(ROOT).as_posix() for p in (ROOT/'database').rglob('*.py')}
    h=hashlib.sha256()
    for p in sorted(paths): h.update(p.encode()+b'\0'+(ROOT/p).read_bytes()+b'\0')
    require(len(paths)==146 and h.hexdigest()=='31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174','inventory')
    require(not (ROOT/'scripts/__init__.py').exists(),'unexpected_initializer')
    cg={};exec(compile((ROOT/'scripts/operators/shopify_release_gate_contract.py').read_bytes(),'pinned-contract','exec'),cg)
    k=types.SimpleNamespace(**cg)
    require(k.evaluate_flags(os.environ)[0]=='PASS','shopify_flags')
    require(all(not os.environ.get(n,'').strip() for n in k.LIBPQ_TARGET_ENV_VARS),'libpq_target_override')
    names={'_quoted','_rows','_scalar','_table_exists','table_definition','_index_shape','nonce_definition','observe_retained','observe_footprint','relation_oids','read_version_rows'}
    tree=ast.parse((ROOT/k.GATE_RELATIVE_PATH).read_text())
    nodes=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name in names) or (isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and (n.targets[0].id.endswith('_SQL') or n.targets[0].id=='_USER_SCHEMA'))]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+nodes,type_ignores=[])),'unchanged-observers','exec'),globals())
    expected=json.loads((ROOT/k.EXPECTATIONS_RELATIVE_PATH).read_text())
    tables=() if args.phase=='S0' else k.FOUNDATION_TABLES if args.phase=='S1' else k.FOUNDATION_TABLES+k.CATALOG_TABLES if args.phase=='S2' else k.SHOPIFY_TABLES
    phases={'S0':{'0111','0113','0120'},'S1':{'0111','0113','0121'},'S2':{'0111','0113','0122'},'S3':{'0111','0113','0123'}}
    pinned=expected['shopify_s3']
    pinned['tables']={t:d for t,d in pinned['tables'].items() if t in tables}
    pinned['sequences']=[s for s in pinned['sequences'] if s[1] in ['public.'+t for t in tables]]
    if args.phase=='S1':
        for field in ('constraints','indexes'):
            pinned['tables']['shopify_connections'][field]=[v for v in pinned['tables']['shopify_connections'][field] if v[0]!=k.CONNECTION_KEY_CONSTRAINT]
    import sqlalchemy as sa
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool
    import importlib.metadata
    require(all(importlib.metadata.version(n)==v for n,v in k.LIBRARY_PINS.items()),'library_pins')
    url=make_url(os.environ['DATABASE_URL'])
    require(url.drivername in ('postgresql','postgresql+psycopg2') and url.host=='postgres-ancu.railway.internal' and (url.port or 5432)==5432 and url.database=='railway' and not url.query,'original_production_target')
    engine=sa.create_engine(url,poolclass=NullPool,echo=False,hide_parameters=True,connect_args={'connect_timeout':5,'options':'-c search_path=public,pg_catalog -c statement_timeout=15000 -c lock_timeout=3000'})
    try:
        with engine.connect().execution_options(isolation_level='REPEATABLE READ',postgresql_readonly=True) as conn:
            require(_scalar(conn,'SHOW transaction_read_only')=='on','readonly_transaction')
            require(_scalar(conn,'SELECT current_database()')=='railway' and not _scalar(conn,'SELECT pg_is_in_recovery()'),'production_primary')
            rows=read_version_rows(conn)
            require(k.evaluate_rows(rows,phases[args.phase],set.union(*phases.values()))[0]=='PASS','phase_heads')
            retained=observe_retained(conn)
            require(k.evaluate_retained(retained,expected['retained'])[0]=='PASS','stock_retained_definitions')
            footprint=observe_footprint(conn,{'tables':tables})
            require(k.evaluate_footprint(footprint,{'tables':tables},pinned)[0]=='PASS','phase_footprint')
            protected=tuple(sorted(set(k.IDENTITY_TABLES)|{'products','product_variants','customers','customer_addresses','customer_address_provenance','meta_catalog_memberships'}))
            baseline=k.normalize({'target':{'project':IDS['RAILWAY_PROJECT_ID'],'environment':IDS['RAILWAY_ENVIRONMENT_ID'],'service':IDS['RAILWAY_SERVICE_ID'],'db':'postgres-ancu.railway.internal:5432/railway'},'retained_sha256':digest(k.normalize(retained)),'relation_oids':relation_oids(conn,protected),'definition_sha256':{t:digest(k.normalize(table_definition(conn,t))) for t in protected}})
            nonempty=sum(bool(_scalar(conn,'SELECT EXISTS(SELECT 1 FROM '+_quoted(t)+')')) for t in tables)
            require(nonempty==0,'shopify_rows_present_no_downgrade_authority')
            if args.baseline_b64:
                before=json.loads(base64.b64decode(args.baseline_b64,validate=True))
                require(before==baseline,'protected_baseline_changed')
            elif args.phase!='S0': require(False,'s0_baseline_required')
            out.update({'status':'PASS','source_r':R,'inventory_count':146,'inventory_sha256':h.hexdigest(),'observed_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'heads':sorted(rows),'shopify_tables':len(tables),'shopify_nonempty_tables':nonempty,'stock_retained_definitions':'PASS','baseline':baseline,'baseline_sha256':digest(baseline),'baseline_compared':bool(args.baseline_b64),'shopify_flags':'OFF'})
            conn.rollback()
    finally: engine.dispose()
except BaseException as exc:
    out.update({'status':'BLOCKED','error_class':type(exc).__name__})
    if isinstance(exc,RuntimeError): out['reason']=str(exc)
print('NAHLA_PRODUCTION_PHASE '+json.dumps(out,sort_keys=True))
sys.exit(0 if out['status']=='PASS' else 1)
