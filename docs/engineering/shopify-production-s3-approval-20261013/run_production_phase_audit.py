"""Official SSH relay for a SELECT-only audit. Never runs migrations or changes config."""
import argparse,base64,datetime,hashlib,json,os,shlex,subprocess,sys,zlib
from pathlib import Path
P=Path(__file__).parent
parser=argparse.ArgumentParser()
parser.add_argument('--phase',required=True,choices=['S0','S1','S2','S3'])
parser.add_argument('--baseline-file',type=Path)
args=parser.parse_args()
code=(P/'production_phase_audit.py').read_bytes().replace(b'\r\n',b'\n')
compile(code,'production_phase_audit.py','exec')
argv=['production-phase-audit','--phase',args.phase]
if args.baseline_file:
    baseline=json.loads(args.baseline_file.read_text())['baseline']
    argv+=['--baseline-b64',base64.b64encode(json.dumps(baseline,sort_keys=True,separators=(',',':')).encode()).decode()]
program='import sys,base64,zlib;sys.argv='+repr(argv)+';exec(zlib.decompress(base64.b64decode('+repr(base64.b64encode(zlib.compress(code,9)).decode())+')))'
remote='python -I -B -c '+shlex.quote(program)
cli=r'C:\Users\STARS\AppData\Roaming\npm\node_modules\@railway\cli\bin\railway.exe'
env=dict(os.environ,RAILWAY_CALLER='skill:use-railway@1.3.5',RAILWAY_AGENT_SESSION='nahla-s3-readonly-approval-20261013')
result=subprocess.run([cli,'ssh','--project','f0090862-0a40-4293-bd5d-e94df58762b5','--environment','ede962ce-3042-4dae-94de-623837e83ed9','--service','686b36c5-a926-4e58-912a-5e9d13fbc2e7','--',remote],env=env,capture_output=True,timeout=90)
reports=[json.loads(x.removeprefix('NAHLA_PRODUCTION_PHASE ')) for x in result.stdout.decode(errors='replace').splitlines() if x.startswith('NAHLA_PRODUCTION_PHASE ')]
if not reports:
    print(json.dumps({'status':'BLOCKED','reason':'no_safe_phase_audit_report','exit_code':result.returncode}));raise SystemExit(1)
report=reports[-1]
report['operator_sha256']=hashlib.sha256(code).hexdigest()
raw=(json.dumps(report,indent=2,sort_keys=True)+'\n').encode()
stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
with (P/('production-phase-'+args.phase+'-'+stamp+'.json')).open('xb') as f:f.write(raw)
latest='production-phase-'+args.phase+('-audit-latest.json' if args.baseline_file else '-preapproval.json')
(P/latest).write_bytes(raw)
print(json.dumps(report,sort_keys=True))
raise SystemExit(0 if result.returncode==0 and report['status']=='PASS' else 1)
