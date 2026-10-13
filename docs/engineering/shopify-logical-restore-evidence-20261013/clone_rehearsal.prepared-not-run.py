"""CLONE-ONLY rehearsal. Never a production gate or production migration runner."""
from __future__ import annotations
import ast, copy, datetime, hashlib, importlib.metadata, json, os, re, runpy, sys, types
from pathlib import Path

R = 'a8fb88e32bca9a08ae14bef9c27eb39fbfee97ec'
ROOT = Path('/recovery/clone-rehearsal-20261013/source')
OUT = ROOT.parent
B = Path('/recovery/shopify-logical-20261013/nahla.dump')
BH = '5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c'
IDS = {'RAILWAY_PROJECT_ID':'f0090862-0a40-4293-bd5d-e94df58762b5', 'RAILWAY_ENVIRONMENT_ID':'ede962ce-3042-4dae-94de-623837e83ed9', 'RAILWAY_SERVICE_ID':'6afcbd3e-15dd-46c1-9183-932fe02d075f', 'CLONE_REHEARSAL_NONCE':'20261013T0924-no-source'}
report = {'kind':'clone_migration_rehearsal','scope':'CLONE ONLY','source_r':R,'production_gate_pass':False,'production_ddl':False,'source_connection':False,'steps':[]}
stage = 'preflight'
def require(ok, why):
    if not ok: raise RuntimeError(why)
def canonical(value): return json.dumps(value, sort_keys=True, separators=(',',':'), default=str).encode()
def sha(value): return hashlib.sha256(canonical(value)).hexdigest()
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
def save(name, value):
    path = OUT / name
    with path.open('xb') as f: f.write(canonical(value)+b'\n')
    path.chmod(0o400)
    return hashlib.sha256(path.read_bytes()).hexdigest()
def emit(value): print('NAHLA_RECOVERY '+json.dumps(value,sort_keys=True), flush=True)
def checked(result, name):
    require(result[0]=='PASS',name+':'+str(result[1])); return {'status':result[0],'detail':result[2]}

try:
    require(all(os.environ.get(k)==v for k,v in IDS.items()),'clone_identity')
    require(all(not os.environ.get(k) for k in ('SRC_PGHOST','SRC_PGUSER','SRC_PGPASSWORD','SRC_PGPORT','SRC_PGDATABASE','DATABASE_URL')),'source_environment_present')
    require(hashlib.sha256(B.read_bytes()).hexdigest()==BH,'archive_hash')
    cg=runpy.run_path(str(ROOT/'scripts/operators/shopify_release_gate_contract.py'))
    k=types.SimpleNamespace(**cg)
    require(all(importlib.metadata.version(name)==v for name,v in k.LIBRARY_PINS.items()),'library_pins')
    checked(k.evaluate_flags(os.environ),'flags')
    import sqlalchemy as sa
    from sqlalchemy.pool import NullPool
    from alembic import command
    from alembic.config import Config
    tree=ast.parse((ROOT/'scripts/operators/shopify_release_gate.py').read_text())
    names={'_quoted','_rows','_scalar','_table_exists','table_definition','_index_shape','nonce_definition','observe_retained','observe_footprint','relation_oids','read_version_rows','_database_py_paths','inventory_paths','digest_paths','inventory','load_graph'}
    nodes=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name in names) or (isinstance(n,ast.Assign) and len(n.targets)==1 and isinstance(n.targets[0],ast.Name) and (n.targets[0].id.endswith('_SQL') or n.targets[0].id=='_USER_SCHEMA'))]
    require({n.name for n in nodes if isinstance(n,ast.FunctionDef)}==names,'observer_selection')
    exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)]+nodes,type_ignores=[])),'unchanged-a8fb-observers','exec'),globals())
    runtime=inventory(ROOT)
    require(runtime==(146,'31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174'),'source_inventory')
    graph=load_graph(ROOT)
    require(graph['heads']==k.EXPECTED_REPOSITORY_HEADS,'graph_heads')
    require(all(graph['down'][rev]==(parent,) for rev,parent in k.EXPECTED_DOWN_REVISIONS.items()),'graph_edges')
    expected=json.loads((ROOT/k.EXPECTATIONS_RELATIVE_PATH).read_text())
    approved=copy.deepcopy(expected['retained'])
    literal=r"'(?:''|[^'])*'::character varying"
    pattern=re.compile(r'\(ARRAY\[('+literal+r'(?:,\s*'+literal+r')*)\]\)::text\[\]')
    overrides=[]
    for table,definition in approved['tables'].items():
        for constraint in definition['constraints']:
            old=constraint[2]
            new,count=pattern.subn(lambda m:'ARRAY['+', '.join('('+s+')::text' for s in re.findall(literal,m.group(1)))+']',old)
            if count:
                require(count==1,'override_multiplicity')
                overrides.append({'table':table,'constraint':constraint[0],'before_sha256':hashlib.sha256(old.encode()).hexdigest(),'after_sha256':hashlib.sha256(new.encode()).hexdigest()})
                constraint[2]=new
    require(len(overrides)==8,'override_count')
    require({t:sum(x['table']==t for x in overrides) for t in {x['table'] for x in overrides}}=={'commerce_runtime_deferred_inbound':4,'commerce_runtime_handover_barrier':2,'commerce_runtime_handover_workers':1,'commerce_runtime_navigation_snapshots':1},'override_scope')
    engine=sa.create_engine('postgresql+psycopg2://postgres@/railway?host=/var/run/postgresql',poolclass=NullPool,echo=False,hide_parameters=True,connect_args={'options':'-c search_path=public,pg_catalog -c statement_timeout=120000 -c lock_timeout=3000'})
    def identity(conn):
        require(_scalar(conn,'SHOW listen_addresses')=='127.0.0.1','listener')
        require(_scalar(conn,'SHOW data_directory')=='/tmp/nahla-shopify-restore-pgdata','data_directory')
        require(_scalar(conn,'SHOW server_version_num')=='180006','server_version')
        require(_scalar(conn,'SELECT inet_server_addr() IS NULL'),'unix_socket_only')
        require(_scalar(conn,'SELECT current_database()')=='railway','database')
        return {'platform':IDS,'deployment_id':os.environ['RAILWAY_DEPLOYMENT_ID'],'system_identifier':str(_scalar(conn,'SELECT system_identifier FROM pg_control_system()')),'database_oid':_scalar(conn,"SELECT oid FROM pg_database WHERE datname=current_database()"),'postmaster_started':str(_scalar(conn,'SELECT pg_postmaster_start_time()')),'server_version_num':180006,'archive_sha256':BH}
    with engine.connect() as conn:
        original_tables=[x[0] for x in _rows(conn,"SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r' ORDER BY relname")]
    require(len(original_tables)==128 and 'alembic_version' in original_tables,'restored_table_count')
    data_tables=[t for t in original_tables if t!='alembic_version']
    def protected(conn):
        data={}
        for table in data_tables:
            digest=hashlib.sha256(); count=0
            sql='SELECT row_to_json(t)::text AS row FROM '+_quoted(table)+' t ORDER BY row_to_json(t)::text COLLATE "C"'
            result=conn.execution_options(stream_results=True).execute(sa.text(sql))
            for row in result:
                raw=row[0].encode();digest.update(len(raw).to_bytes(8,'big'));digest.update(raw);count+=1
            result.close()
            data[table]={'rows':count,'sha256':digest.hexdigest()}
        seq={}
        for (name,) in _rows(conn,"SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='S' AND relname NOT LIKE 'shopify%'"):
            seq[name]=_rows(conn,'SELECT last_value,is_called FROM '+_quoted(name))[0]
        return {'tables':data,'sequences':seq}
    def snapshot(phase):
        tables=() if phase=='S0' else k.FOUNDATION_TABLES if phase=='S1' else k.FOUNDATION_TABLES+k.CATALOG_TABLES if phase=='S2' else k.SHOPIFY_TABLES
        with engine.connect().execution_options(isolation_level='REPEATABLE READ',postgresql_readonly=True) as conn:
            require(_scalar(conn,'SHOW transaction_read_only')=='on','observer_readonly')
            return k.normalize({'identity':identity(conn),'rows':read_version_rows(conn),'retained':observe_retained(conn),'oids':relation_oids(conn,original_tables),'schema':{t:table_definition(conn,t) for t in original_tables},'protected':protected(conn),'footprint':observe_footprint(conn,{'tables':tables})})
    stage='freeze_pre_ddl_baseline'
    baseline=snapshot('S0')
    checked(k.evaluate_rows(baseline['rows'],k.DEPLOY_PHASES['S0']['rows'],graph['known']),'s0_rows')
    stock=k.evaluate_retained(baseline['retained'],expected['retained'])
    require(stock[0]=='FAIL' and stock[1]=='retained_definition_drift','stock_drift_not_exact')
    checked(k.evaluate_retained(baseline['retained'],approved),'only_eight_reviewed_representations')
    require(baseline['retained']==k.normalize(approved),'full_retained_exact')
    checked(k.evaluate_footprint(baseline['footprint'],{'tables':()},{}),'s0_footprint')
    baseline_record={'clone_only':True,'source_r':R,'runtime_inventory':runtime,'archive_sha256':BH,'overrides':overrides,'baseline':baseline}
    baseline_hash=save('pre-ddl-baseline.private.json',baseline_record)
    report.update({'started_at_utc':now(),'clone_identity':baseline['identity'],'archive_sha256':BH,'baseline_sha256':baseline_hash,'approved_representation_overrides':overrides,'runtime_inventory':runtime,'stock_retained_comparator':{'status':stock[0],'reason':stock[1],'detail':stock[2]},'protected_table_count':len(data_tables),'original_sequence_count':len(baseline['protected']['sequences']),'guard_modified':False})
    emit({'kind':'clone_baseline_frozen','baseline_sha256':baseline_hash,'stock_retained_comparator':stock[0],'reviewed_clone_baseline':'PASS','approved_representation_count':8})
    def verify(phase):
        current=snapshot(phase)
        rows=k.DEPLOY_PHASES[phase]['rows'] if phase in k.DEPLOY_PHASES else k.INTERMEDIATE_STATES[phase]
        checked(k.evaluate_rows(current['rows'],rows,graph['known']),'rows_'+phase)
        checked(k.evaluate_retained(current['retained'],baseline['retained']),'retained_'+phase)
        checked(k.evaluate_relation_identity(baseline['oids'],current['oids']),'relation_identity_'+phase)
        require(current['identity']==baseline['identity'],'clone_identity_drift')
        require(current['schema']==baseline['schema'],'original_schema_drift')
        require(current['protected']==baseline['protected'],'original_data_or_sequences_drift')
        tables=() if phase=='S0' else k.FOUNDATION_TABLES if phase=='S1' else k.FOUNDATION_TABLES+k.CATALOG_TABLES if phase=='S2' else k.SHOPIFY_TABLES
        pinned=copy.deepcopy(expected['shopify_s3'])
        pinned['tables']={t:d for t,d in pinned['tables'].items() if t in tables}
        pinned['sequences']=[s for s in pinned['sequences'] if s[1] in ['public.'+t for t in tables]]
        if phase=='S1':
            con=pinned['tables']['shopify_connections']
            con['constraints']=[x for x in con['constraints'] if x[0]!=k.CONNECTION_KEY_CONSTRAINT]
            con['indexes']=[x for x in con['indexes'] if x[0]!=k.CONNECTION_KEY_CONSTRAINT]
        checked(k.evaluate_footprint(current['footprint'],{'tables':tables},pinned),'footprint_'+phase)
        require(hashlib.sha256(B.read_bytes()).hexdigest()==BH,'archive_changed')
        require(hashlib.sha256((OUT/'pre-ddl-baseline.private.json').read_bytes()).hexdigest()==baseline_hash,'baseline_changed')
        sh=save('snapshot-'+str(len(report['steps']))+'-'+phase+'.private.json',current)
        return {'phase':phase,'status':'PASS','rows':sorted(current['rows']),'shopify_tables':len(tables),'snapshot_sha256':sh,'protected_data_sha256':sha(current['protected']),'same_clone_identity':True,'retained_and_original_definitions_unchanged':True,'original_relation_oids_unchanged':True}
    config=Config()
    config.set_main_option('script_location',str(OUT/'alembic-clone'))
    config.set_main_option('version_locations',str(ROOT/'database/migrations/versions'))
    for direction,target,phase,applied in [('upgrade','0121','S1','0121'),('upgrade','0122','S2','0122'),('upgrade','0123','S3','0123'),('downgrade','0123@-1','S2','0123'),('downgrade','0122@-1','S1','0122'),('downgrade','0121@-1','S0','0121')]:
        stage=direction+'_'+target
        events=[]
        def record_step(ctx,step,heads,run_args): events.append({'upgrade':step.is_upgrade,'revision':step.up_revision_id})
        config.attributes['on_version_apply']=record_step
        with engine.begin() as conn:
            require(identity(conn)==baseline['identity'],'pre_ddl_identity')
            config.attributes['connection']=conn
            getattr(command,direction)(config,target)
            require(events==[{'upgrade':direction=='upgrade','revision':applied}],'unexpected_migration_execution')
        result=verify(phase)
        result.update({'command':direction+' '+target,'executed_revisions':events,'at_utc':now()})
        report['steps'].append(result)
        save('step-'+str(len(report['steps']))+'.json',result)
        emit({'kind':'clone_step','command':result['command'],'phase':phase,'status':'PASS','rows':result['rows'],'shopify_tables':result['shopify_tables']})
    require(inventory(ROOT)==runtime,'source_inventory_changed')
    report['status']='PASS'; report['completed_at_utc']=now();report['final_phase']='S0'
except BaseException as exc:
    report.update({'status':'BLOCKED','stage':stage,'error_class':type(exc).__name__})
    if isinstance(exc,RuntimeError): report['reason']=str(exc)[:120]
    # Error bodies from database drivers remain private and are never printed.
    (OUT/'failure.private.txt').write_text(type(exc).__name__+'\n'+str(exc))
finally:
    report_hash=save('rehearsal-result.json',report)
    emit(dict(report,report_sha256=report_hash))
sys.exit(0 if report['status']=='PASS' else 1)
