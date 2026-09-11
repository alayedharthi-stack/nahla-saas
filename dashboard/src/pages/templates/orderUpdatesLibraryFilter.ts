import type { NahlaLibraryGroup, NahlaLibraryTemplate } from '../../api/templates'
import { ORDER_UPDATE_SERVICE_KEYS } from '../../api/orderUpdates'

const ORDER_UPDATE_SERVICE_SET = new Set<string>(ORDER_UPDATE_SERVICE_KEYS)

export const ORDER_UPDATES_LIBRARY_TAG = 'order_updates' as const

export function isOrderUpdatesLibraryTemplate(tpl: NahlaLibraryTemplate): boolean {
  // The public order-updates filter is explicit so lifecycle templates with
  // distinct service keys (for example post-delivery) remain discoverable.
  // Meta-review demos share service keys with real templates, but must never
  // be presented as merchant-facing order-update templates.
  if (tpl.filter_tags.includes('english_demo')) return false
  return tpl.filter_tags.includes(ORDER_UPDATES_LIBRARY_TAG)
    || ORDER_UPDATE_SERVICE_SET.has(tpl.service_key)
}

export function filterOrderUpdatesLibraryTemplates(
  templates: NahlaLibraryTemplate[],
): NahlaLibraryTemplate[] {
  const servicePriority: Record<string, number> = {
    cod_confirmation: 0,
    order_confirmation: 1,
  }
  return templates
    .filter(isOrderUpdatesLibraryTemplate)
    .map((template, index) => ({ template, index }))
    .sort((a, b) =>
      (servicePriority[a.template.service_key] ?? 2)
      - (servicePriority[b.template.service_key] ?? 2)
      || a.index - b.index,
    )
    .map(({ template }) => template)
}

export function filterOrderUpdatesLibraryGroups(
  groups: NahlaLibraryGroup[],
): NahlaLibraryGroup[] {
  return groups
    .map(group => ({
      ...group,
      templates: filterOrderUpdatesLibraryTemplates(group.templates ?? []),
    }))
    .filter(group => (group.templates?.length ?? 0) > 0)
}

/**
 * The WhatsApp templates screen owns customer-facing WhatsApp templates.
 * Order-update templates belong to the store templates screen, where their
 * lifecycle settings and revisions are managed together.
 */
export function filterWhatsAppLibraryTemplates(
  templates: NahlaLibraryTemplate[],
): NahlaLibraryTemplate[] {
  return templates.filter(tpl => !isOrderUpdatesLibraryTemplate(tpl))
}

export function filterWhatsAppLibraryGroups(
  groups: NahlaLibraryGroup[],
): NahlaLibraryGroup[] {
  return groups
    .map(group => ({
      ...group,
      // The endpoint returns both channel groups. Store templates must stay
      // visible there even when their lifecycle service key is shared with an
      // order-update row (order_summary uses order_confirmation internally).
      templates:
        group.channel === 'whatsapp'
          ? filterWhatsAppLibraryTemplates(group.templates ?? [])
          : (group.templates ?? []),
    }))
    .filter(group => (group.templates?.length ?? 0) > 0)
}
