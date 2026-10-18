"""Explicit graph/profile/rollback admission; no live database or approval."""
import json
from pathlib import Path
import pytest
from backend.tests import _shopify_release_gate_harness as h
from scripts.operators import shopify_release_gate as gate
from scripts.operators import shopify_release_gate_contract as k
from scripts.operators import shopify_release_gate_render as render
from scripts.operators.bootstrap_migration_contract import INTEGRATION_BOOTSTRAP_TARGET, repository_heads_expected

ROOT=Path(__file__).resolve().parents[1]


def test_exact_new_graph_and_closed_named_profiles():
    assert INTEGRATION_BOOTSTRAP_TARGET=='0093'
    assert repository_heads_expected({'0092','0111','0114','0115','0126'})
    assert repository_heads_expected({'0092','0111','0114','0115','0124'})
    assert not repository_heads_expected({'0092','0111','0114','0115','0126','0125'})
    assert not repository_heads_expected({'0092','0111','0114','0115','9126'})
    for mode in ('DORMANT','PILOT'):
        for state,tip in [('PRE','0124'),('POST','0126')]:
            phase=k.phase('S4_COUPON_'+state+'_'+mode)
            assert phase['rows']==frozenset({'0111','0113',tip})
            assert phase['shopify_row']=='0124' and len(phase['tables'])==17
            assert gate.check_graph(ROOT,phase)[0].status==k.PASS
    assert k.phase('S4_COUPON_UNKNOWN') is None


@pytest.mark.parametrize('mutation',[None,'old_source','missing_proof','wrong_proof_source','false_batch','wrong_heads','wrong_fence','errors','empty'])
def test_post_schema_rollback_requires_same_tested_alias_bridge(tmp_path,mutation):
    root=h.build_root(tmp_path/'root')
    phase='S4_COUPON_POST_DORMANT'
    db='disposable.example.test:5432/synthetic'
    path=h.approval_for(root,phase,db)
    record=json.loads(path.read_text())
    evidence=record['schema_rollback']
    if mutation=='old_source': evidence['source_sha']='efb8f7a7d7c2fabe876f71cfff17bdaa8a87ccd6'
    if mutation=='missing_proof': evidence.pop('functional_bridge_proof')
    if mutation=='wrong_proof_source': evidence['functional_bridge_proof']['source_sha']='a4e96891bda691b1ee0f7cb105c5810e727c39bd'
    if mutation=='false_batch': evidence['functional_bridge_proof']['ordinary_and_canonical_batch_committed']=False
    if mutation=='wrong_heads': evidence['database_heads']=['0111','0113','0124']
    if mutation=='wrong_fence': evidence['coupon_schema_sha256']='0'*64
    if mutation=='errors': evidence['functional_bridge_proof']['errors']=1
    if mutation=='empty': evidence['functional_bridge_proof']['tests']=0
    path=h.approval_file(tmp_path/'approval',record)
    verdict=render.approval_verdict(root,approval_file=path,release_sha=h.SYNTHETIC_RELEASE_SHA,
        phase=phase,identity=h.SYNTHETIC_IDENTITY,db=db,backup_service=h.SYNTHETIC_BACKUP['service'],
        backup_volume=h.SYNTHETIC_BACKUP['volume'])
    assert verdict['status']==(k.PASS if mutation is None else k.FAIL),verdict
