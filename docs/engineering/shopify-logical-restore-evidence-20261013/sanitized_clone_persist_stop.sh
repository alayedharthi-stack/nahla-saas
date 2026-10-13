set -euo pipefail
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${CLONE_REHEARSAL_NONCE:-}" = 20261013T0924-no-source
for key in SRC_PGHOST SRC_PGPORT SRC_PGUSER SRC_PGPASSWORD SRC_PGDATABASE; do
 test "${!key:-}" = __REVOKED_NO_SOURCE_ACCESS__
 unset "$key"
done
test -z "${DATABASE_URL:-}"
set -euo pipefail
umask 077
test "${RAILWAY_SERVICE_ID:-}" = 6afcbd3e-15dd-46c1-9183-932fe02d075f
test "${RAILWAY_DEPLOYMENT_ID:-}" = 36a2d33d-ab11-4666-b91c-a1a5ca7a1590
/tmp/nahla-clone-venv/bin/python -I -B - <<'PY'
import datetime,hashlib,json,os,pathlib,subprocess
base=pathlib.Path('/recovery/clone-rehearsal-20261013')
archive=pathlib.Path('/recovery/shopify-logical-20261013/nahla.dump')
sha=hashlib.sha256(archive.read_bytes()).hexdigest()
assert sha=='5d677073e9a55d6d44c1d65a808f07d7b085802d8a241091919d66f1ec2fe63c'
assert all(not os.environ.get(k) for k in ('SRC_PGHOST','SRC_PGUSER','SRC_PGPASSWORD','SRC_PGPORT','SRC_PGDATABASE','DATABASE_URL'))
def scalar(sql):
 return subprocess.check_output(['psql','-XAt','--no-password','-h','/var/run/postgresql','-U','postgres','-d','railway','-v','ON_ERROR_STOP=1','-c',sql]).decode().strip()
heads=scalar("SELECT string_agg(version_num,',' ORDER BY version_num) FROM alembic_version").split(',')
assert set(heads)<=set(('0111','0113','0120','0121','0122','0123'))
result_path=base/'rehearsal-result.json'
result=json.loads(result_path.read_text()) if result_path.exists() else {'status':'BLOCKED','stage':'incomplete_process'}
result_sha=hashlib.sha256(result_path.read_bytes()).hexdigest() if result_path.exists() else None
checkpoint={'kind':'clone_final_checkpoint','at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z'),'target_service_id':os.environ['RAILWAY_SERVICE_ID'],'deployment_id':os.environ['RAILWAY_DEPLOYMENT_ID'],'archive_sha256':sha,'archive_unchanged':True,'archive_mode':oct(archive.stat().st_mode&0o777),'rehearsal_status':result['status'],'rehearsal_report_sha256':result_sha,'heads':heads,'source_access_absent':True,'public_listener_absent':scalar('SHOW listen_addresses')=='127.0.0.1','other_client_sessions':int(scalar("SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()")),'next_action':'stop_compute','volume_retained':True}
assert checkpoint['other_client_sessions']==0
if result['status']=='PASS': assert heads==['0111','0113','0120']
path=base/'final-checkpoint.json'
with path.open('xb') as f: f.write((json.dumps(checkpoint,sort_keys=True)+'\n').encode())
path.chmod(0o400)
print('NAHLA_RECOVERY '+json.dumps(checkpoint,sort_keys=True),flush=True)
PY
exec gosu postgres pg_ctl -D /tmp/nahla-shopify-restore-pgdata -m fast -w stop
