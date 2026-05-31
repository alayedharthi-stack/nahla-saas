/**
 * Resolve backend semantic keys → static UI labels via i18n.
 *
 * API payloads may carry Arabic copy (labelAr) or opaque keys — never
 * pass those strings through t(). Map the key here instead.
 */
import type { OutboundSendError } from '../api/featureReality'
import type { Translations } from './types'

type SendErrorCopy = { label: string; advice?: string | null }

export function resolveOutboundSendError(
  err: OutboundSendError,
  cp: Translations['conversationsPage'],
  lang: 'ar' | 'en',
): SendErrorCopy {
  const key = err.key ?? ''
  const bucket = cp.sendErrors as Record<string, { label: string; advice: string }>
  const mapped = key ? bucket[key] : undefined
  if (mapped) {
    return { label: mapped.label, advice: mapped.advice ?? null }
  }
  if (lang === 'ar') {
    return { label: err.labelAr, advice: err.adviceAr ?? null }
  }
  return { label: cp.sendErrors.default.label, advice: cp.sendErrors.default.advice }
}

/** Conversation / inbox semantic tags — stable keys, not stored display text. */
export type ConversationSemanticTag =
  | 'staff_request'
  | 'human_active'
  | 'ai_paused'
  | 'blocked'
  | 'unsubscribed'
  | 'pending_unsub'
  | 'paid'
  | 'closed'
  | 'customer_message'
  | 'open'

export function conversationTagLabel(
  tag: ConversationSemanticTag,
  tags: Translations['conversationsPage']['conversationTags'],
): string {
  return tags[tag] ?? tag
}
