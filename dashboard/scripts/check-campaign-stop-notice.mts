import assert from 'node:assert/strict'
import { campaignStopNotice } from '../src/i18n/campaignRuntimeLabels.ts'
import { campaignsListAr, campaignsListEn } from '../src/i18n/campaignsListPageLabels.ts'
for (const labels of [campaignsListAr, campaignsListEn]) {
  const blocked = campaignStopNotice({status: 'paused', lifecycle: 'marketing_delivery_blocked', pause_reason: 'provider_throttling'}, labels)
  assert.equal(blocked?.tone, 'amber')
  assert.match(blocked!.text, /131049/)
  for (const reason of ['evidence_unreadable', 'uncertain_sends', 'provider_repeated_error']) {
    assert.equal(campaignStopNotice({status: 'paused', pause_reason: reason}, labels)?.tone, 'red')
  }
  assert.equal(campaignStopNotice({status: 'active', pause_reason: 'provider_throttling'}, labels), null)
  assert.equal(campaignStopNotice({status: 'paused', pause_reason: 'unrecognized'}, labels), null)
}
console.log('Campaign stop notice: provider pause distinguished from operational failure in AR/EN')
