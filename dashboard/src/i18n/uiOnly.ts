/**
 * UI-only translation helpers.
 *
 * IMPORTANT — Only pass static UI labels through `t()` / `tStatic()`.
 * NEVER pass merchant/customer/API content (names, messages, product titles,
 * order notes, phone numbers, catalog titles from Meta/Salla, etc.).
 *
 * Dynamic data must render as-is from the backend.
 * Semantic runtime keys → use `runtimeLabels.ts` (e.g. sendErrors by key).
 */
export { createTStatic, type StaticLabelSelector, type StaticUiLabel } from './tStatic'
export { resolveOutboundSendError, conversationTagLabel, type ConversationSemanticTag } from './runtimeLabels'

export const UI_ONLY_GUARD =
  'Only pass static UI labels through t() / tStatic(); never pass merchant/customer/API content.'