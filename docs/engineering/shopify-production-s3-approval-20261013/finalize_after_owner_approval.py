"""Local-only finalizer. Invoke only after actual main-chat owner approval and binding review.

No Railway, network, database, migration, environment-variable or deployment calls.
It writes the paired payload/command for S3 and the separately scoped S0 rollback.
"""
from pathlib import Path
import argparse,datetime,hashlib,json,sys

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--owner-approved-at',required=True)
    parser.add_argument('--owner-evidence-ref',required=True)
    parser.add_argument('--independent-review-ref',required=True)
    parser.add_argument('--backup-binding-review-ref',required=True)
    parser.add_argument('--deployment-exclusion-confirmation-ref',required=True)
    args=parser.parse_args()
    t=datetime.datetime.fromisoformat(args.owner_approved_at.replace('Z','+00:00'))
    now=datetime.datetime.now(datetime.timezone.utc)
    if t.tzinfo is None or not t<=now<t+datetime.timedelta(minutes=30):
        raise SystemExit('REFUSED: actual owner timestamp must be current and unexpired')
    for field in ('owner_evidence_ref','independent_review_ref','backup_binding_review_ref','deployment_exclusion_confirmation_ref'):
        if not getattr(args,field).strip(): raise SystemExit('REFUSED: missing '+field)
    sys.path.insert(0,str(args.root.resolve()))
    from scripts.operators import shopify_release_gate_render as render
    inv=render.inventory_report(args.root)
    if inv['inventory_count']!=146 or inv['inventory_digest']!='31ef4cd7effa7906ad376341f501f47bd71293eb61b794362462b6dad2205174':
        raise SystemExit('REFUSED: frozen source inventory mismatch')
    args.out.mkdir(parents=True,exist_ok=True)
    results={}
    for label in ('s3','s0-rollback'):
        record=json.loads((Path(__file__).parent/(label+'-approval.unsigned.json')).read_bytes())
        record['_status']='Owner-approved bounded execution record; use only within the documented scope and window.'
        record['approval']={'approved':True,'reviewer':args.owner_evidence_ref,'independent_review_ref':args.independent_review_ref,'approved_at_utc':t.isoformat().replace('+00:00','Z'),'valid_until_utc':(t+datetime.timedelta(minutes=30)).isoformat().replace('+00:00','Z')}
        record['candidate_backup_binding']['independent_review_required']=False
        record['candidate_backup_binding']['owner_acceptance_required']=False
        record['candidate_backup_binding']['accepted_review_ref']=args.backup_binding_review_ref
        record['execution_window']['confirmed_exclusion_ref']=args.deployment_exclusion_confirmation_ref
        record['execution_window']['actual_start_utc']=record['approval']['approved_at_utc']
        record['execution_window']['actual_end_utc']=record['approval']['valid_until_utc']
        raw=(json.dumps(record,indent=2,sort_keys=True)+'\n').encode()
        path=args.out/(label+'-approval.json')
        with path.open('xb') as f: f.write(raw)
        target=record['target'];backup=record['db_recovery_evidence']['backup']
        common=dict(release_sha=record['release_sha'],phase=record['phase'],db=target['db'],backup_service=backup['service_id'],backup_volume=backup['volume_id'])
        verdict=render.approval_verdict(args.root,approval_file=path,identity={n:target[n] for n in ('project','environment','service')},**common)
        if verdict['status']!='PASS': raise SystemExit('REFUSED: '+str(verdict['reason']))
        command=render.render_command(args.root,approval_sha256=verdict['approval_sha256'],project=target['project'],environment=target['environment'],service=target['service'],**common)
        with (args.out/(label+'-predeploy.txt')).open('x') as f:f.write(command+'\n')
        with (args.out/(label+'-bootstrap.py')).open('x') as f:f.write(render.decode_command(command))
        # Ready for independent review / native staging after schema verification.
        # Merely creating this local JSON does not apply or stage it.
        patch={'services':{target['service']:{'deploy':{'preDeployCommand':[command]},'variables':{'NAHLA_SHOPIFY_RELEASE_APPROVAL_B64':{'value':verdict['approval_b64'],'isSealed':True}}}}}
        with (args.out/(label+'-api-binding.patch.json')).open('x') as f:json.dump(patch,f,indent=2,sort_keys=True);f.write('\n')
        results[record['phase']]={'payload_sha256':verdict['approval_sha256'],'command_sha256':hashlib.sha256(command.encode()).hexdigest(),'strict_offline_approval':'PASS','live_changes':False}
    print(json.dumps(results,sort_keys=True))

if __name__=='__main__':main()
