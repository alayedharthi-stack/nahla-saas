/**
 * Client-side heuristics for KB dashboard (PR-2).
 * Used for badges, search filters, and AI preview — does NOT affect runtime.
 */

import type { KnowledgeSection } from '../../api/knowledge'
import {
  BEHAVIOR_KINDS_UI,
  ESCALATION_KINDS_UI,
  PAYMENT_KINDS,
  uiBucketForKind,
} from './kbUiCategories'

const PHONE_RE = /(?:\+?966|00966|0)?5\d[\d\s\-()]{7,12}/
const PRICE_RE = /\d+\s*(?:ريال|ر\.?\s*س|SAR|sar|درهم|aed)/i
const IBAN_RE = /\b(?:SA|AE)\d{2}[A-Z0-9]{20,}\b/i

export function sectionHasPhone(section: KnowledgeSection): boolean {
  const text = `${section.title || ''} ${section.body || ''}`
  return PHONE_RE.test(text)
}

export function sectionHasPrice(section: KnowledgeSection): boolean {
  const text = `${section.title || ''} ${section.body || ''}`
  return PRICE_RE.test(text)
}

export function sectionHasSensitiveOperational(section: KnowledgeSection): boolean {
  const kind = (section.kind || '').toLowerCase()
  if ((PAYMENT_KINDS as readonly string[]).includes(kind)) return true
  if ((ESCALATION_KINDS_UI as readonly string[]).includes(kind)) return true
  return sectionHasPhone(section) || sectionHasPrice(section) || IBAN_RE.test(section.body || '')
}

export function sectionIsBehavioralKind(kind: string): boolean {
  const k = (kind || '').trim().toLowerCase()
  return (
    (BEHAVIOR_KINDS_UI as readonly string[]).includes(k)
    || (ESCALATION_KINDS_UI as readonly string[]).includes(k)
  )
}

export function sectionNeedsReview(section: KnowledgeSection): boolean {
  if (section.kind === 'quick_update') return true
  if (section.ai_status === 'pending') return true
  if (section.conflicts_json && Object.keys(section.conflicts_json).length > 0) return true
  return false
}

export function sectionIsDraftLike(section: KnowledgeSection): boolean {
  return section.kind === 'quick_update' || section.ai_status === 'pending'
}

export function sectionHasProductLink(section: KnowledgeSection): boolean {
  return (section.product_links?.length ?? 0) > 0
}

export function sectionHasMetadataOnlyBody(section: KnowledgeSection): boolean {
  const meta = section.metadata_json
  if (!meta || typeof meta !== 'object') return false
  const keys = Object.keys(meta)
  if (keys.length === 0) return false
  const body = (section.body || '').trim()
  return body.length < 40 && keys.length > 0
}

export type SearchFilterKey =
  | 'all'
  | 'active'
  | 'drafts'
  | 'inactive'
  | 'needs_review'
  | 'has_phone'
  | 'has_price'
  | 'linked_product'
  | 'ai_visible'
  | 'ai_hidden'

export function matchesClientFilter(
  section: KnowledgeSection,
  filter: SearchFilterKey,
): boolean {
  switch (filter) {
    case 'all':
      return true
    case 'active':
      return section.is_active
    case 'drafts':
      return sectionIsDraftLike(section)
    case 'inactive':
      return !section.is_active
    case 'needs_review':
      return sectionNeedsReview(section)
    case 'has_phone':
      return sectionHasPhone(section)
    case 'has_price':
      return sectionHasPrice(section)
    case 'linked_product':
      return sectionHasProductLink(section)
    case 'ai_visible':
      return section.is_active && !section.deleted_at
    case 'ai_hidden':
      return !section.is_active || !!section.deleted_at
    default:
      return true
  }
}

/** Registry group number (backend) from kind — for prompt preview label. */
export function registryGroupForKind(kind: string): number {
  const bucket = uiBucketForKind(kind)
  const map: Record<string, number> = {
    store_info: 2,
    shipping: 4,
    payment: 3,
    policies: 3,
    faq: 3,
    product_notes: 5,
    escalation: 7,
    assistant_behavior: 7,
    review: 1,
    sales_rules: 2,
    media: 6,
  }
  return map[bucket] ?? 2
}
