import type { NahlaLibraryGroup, NahlaLibraryTemplate } from '../../api/templates'
import { ORDER_UPDATE_SERVICE_KEYS } from '../../api/orderUpdates'

const ORDER_UPDATE_SERVICE_SET = new Set<string>(ORDER_UPDATE_SERVICE_KEYS)

export const ORDER_UPDATES_LIBRARY_TAG = 'order_updates' as const

export function isOrderUpdatesLibraryTemplate(tpl: NahlaLibraryTemplate): boolean {
  return ORDER_UPDATE_SERVICE_SET.has(tpl.service_key)
}

export function filterOrderUpdatesLibraryTemplates(
  templates: NahlaLibraryTemplate[],
): NahlaLibraryTemplate[] {
  return templates.filter(isOrderUpdatesLibraryTemplate)
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
      templates: filterWhatsAppLibraryTemplates(group.templates ?? []),
    }))
    .filter(group => (group.templates?.length ?? 0) > 0)
}
