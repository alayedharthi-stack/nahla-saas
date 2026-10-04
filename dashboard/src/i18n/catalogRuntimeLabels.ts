/**
 * Catalog runtime labels — backend semantic codes → static UI labels.
 *
 * The catalog APIs return Arabic copy (`message_ar`, `action_ar`) next to a
 * stable code (`blocker_code`, `action_code`, issue `code`). Never pass the
 * Arabic copy through t(); map the code here and fall back to the backend
 * copy only when the code is unknown (so no information is lost).
 */
import type { Translations } from './types'

type Lang = 'ar' | 'en'

/** Readiness blocker → label (+ optional action label) for the WhatsApp sync card. */
export function resolveCatalogBlocker(
  status: { blocker_code?: string | null; action_code?: string | null; message_ar?: string | null; action_ar?: string | null } | null | undefined,
  copy: Translations['catalogMgmt']['whatsappSync'],
  lang: Lang,
): string | null {
  if (!status) return null
  const code = status.blocker_code ?? ''
  const action = status.action_code ?? ''
  const blockerLabel = code ? copy.blockers[code] : undefined
  const actionLabel = action ? copy.actions[action] : undefined
  if (blockerLabel) {
    return [blockerLabel, actionLabel].filter(Boolean).join(' ')
  }
  // Unknown code: keep the backend copy (Arabic) so nothing is hidden.
  const raw = [status.message_ar, status.action_ar].filter(Boolean).join(' ')
  if (raw) return raw
  if (lang === 'en' && code) return `${copy.phaseBlocked} (${code})`
  return null
}

/** Preview / confirm issue (`{code, message_ar}`) → label. */
export function resolveCatalogIssue(
  issue: { code?: string | null; message_ar?: string | null } | null | undefined,
  drawer: Translations['catalogMgmt']['studio']['drawer'],
  lang: Lang,
): string {
  if (!issue) return ''
  const code = issue.code ?? ''
  const mapped = code ? drawer.metaSyncIssues[code] : undefined
  if (mapped) return mapped
  if (issue.message_ar && issue.message_ar.trim()) return issue.message_ar
  if (code) return lang === 'en' ? `${drawer.metaSyncIssueFallback} (${code})` : `${drawer.metaSyncIssueFallback} (${code})`
  return drawer.metaSyncIssueFallback
}
