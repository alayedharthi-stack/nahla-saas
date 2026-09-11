import type { NahlaLibraryResponse, WhatsAppTemplateRecord } from '../../api/templates'
import { isOrderUpdateServiceKey } from '../../lib/orderUpdateServiceKeys'

export type TemplateChannel = 'all' | 'whatsapp' | 'store'

// Channel comes from the library contract, never from display names or message text.
export function libraryChannels(library: NahlaLibraryResponse): Record<string, string> {
  const channels: Record<string, string> = {}
  for (const group of library.groups ?? []) {
    for (const item of group.templates) channels[item.key] = item.filter_meta?.order_channel ?? group.channel
  }
  for (const item of library.templates) {
    if (item.filter_meta?.order_channel) channels[item.key] = item.filter_meta.order_channel
  }
  return channels
}

export function matchesTemplateChannel(
  template: WhatsAppTemplateRecord, channel: TemplateChannel, channels: Record<string, string>,
): boolean {
  if (channel === 'all') return true
  const declared = channels[template.nahla_source_key ?? '']
  if (declared === 'external_store') return channel === 'store'
  if (declared === 'whatsapp') return channel === 'whatsapp'
  if (declared === 'adaptive') return true
  // Legacy lifecycle records have an explicit service contract; unknown records
  // remain visible under All rather than being falsely labeled as WhatsApp-only.
  return channel === 'store' && isOrderUpdateServiceKey(template.service_key ?? '')
}

export function upsertImportedTemplate(records: WhatsAppTemplateRecord[], imported: WhatsAppTemplateRecord) {
  return [imported, ...records.filter(record => record.id !== imported.id)]
}
