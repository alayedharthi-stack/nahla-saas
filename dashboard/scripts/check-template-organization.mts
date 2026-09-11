import assert from 'node:assert/strict'
import { libraryChannels, matchesTemplateChannel, upsertImportedTemplate } from '../src/pages/templates/templateOrganization'
import type { NahlaLibraryResponse, WhatsAppTemplateRecord } from '../src/api/templates'

const library = {
  templates: [{ key: 'campaign', filter_meta: { order_channel: 'external_store' } }],
  groups: [
    { channel: 'external_store', templates: [{ key: 'confirmation' }] },
    { channel: 'whatsapp', templates: [{ key: 'manual-order' }] },
  ],
} as unknown as NahlaLibraryResponse
const channels = libraryChannels(library)
const record = (id: number, key: string | null, service: string | null = null) => ({
  id, nahla_source_key: key, service_key: service, status: 'PENDING',
  name: 'generic_merchant_template', components: [],
}) as WhatsAppTemplateRecord
const store = record(1, 'confirmation', 'order_confirmation')
const whatsapp = record(2, 'manual-order', 'order_confirmation')
const campaign = record(3, 'campaign', 'promotion')
const unknown = record(4, null)

assert.equal(matchesTemplateChannel(store, 'store', channels), true)
assert.equal(matchesTemplateChannel(whatsapp, 'store', channels), false)
assert.equal(matchesTemplateChannel(whatsapp, 'whatsapp', channels), true)
assert.equal(matchesTemplateChannel(campaign, 'store', channels), true)
assert.equal(matchesTemplateChannel(unknown, 'all', channels), true)
assert.equal(matchesTemplateChannel(unknown, 'whatsapp', channels), false)
assert.equal(matchesTemplateChannel(store, 'store', {}), true)
const before = JSON.stringify(store)
const records = upsertImportedTemplate([store, whatsapp], store)
assert.equal(records.length, 2)
assert.equal(records[0].id, store.id)
assert.equal(records[0].status, 'PENDING')
assert.equal(JSON.stringify(store), before)
console.log('PASS template organization: channel metadata, legacy fallback, duplicate import, status preserved')
