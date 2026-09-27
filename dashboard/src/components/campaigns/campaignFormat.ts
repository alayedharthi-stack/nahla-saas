import type { Lang } from '../../i18n/types'
import type { CampaignRecord } from '../../api/campaigns'

export function localeTag(lang: Lang): string {
  return lang === 'en' ? 'en-US' : 'ar-SA'
}

export function fmtCount(n: number | null | undefined, lang: Lang): string {
  return (n ?? 0).toLocaleString(localeTag(lang))
}

/** A server UTC timestamp (with or without a zone suffix) in local time. */
export function formatUtcTime(iso: string | null | undefined, lang: Lang): string {
  if (!iso) return '—'
  const z = iso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(iso) ? '' : 'Z'
  const d = new Date(iso + z)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(localeTag(lang), { dateStyle: 'short', timeStyle: 'short' })
}

export function formatClock(d: Date, lang: Lang): string {
  return d.toLocaleTimeString(localeTag(lang), { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** ISO (UTC, naive or zoned) → value for `<input type="datetime-local">`. */
export function isoToLocalInput(iso: string | null | undefined): string {
  if (!iso) return ''
  const z = iso.endsWith('Z') || /[+-]\d\d:?\d\d$/.test(iso) ? '' : 'Z'
  const d = new Date(iso + z)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** `<input type="datetime-local">` value → zoned ISO string for the API. */
export function localInputToIso(value: string): string | null {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return null
  return d.toISOString()
}

/** Terminal states never appear in the live section. */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'draft'])

/**
 * A campaign the merchant is waiting on: started, not finished, and either
 * still sending, waiting for the platform, or stopped with recipients left.
 */
export function isLiveCampaign(c: CampaignRecord): boolean {
  if (TERMINAL_STATUSES.has(c.status)) return false
  if (c.status === 'scheduled' || c.status === 'active') return true
  const remaining = c.stats?.queued ?? 0
  const workerRunning = !!c.execution?.worker_running
  return c.status === 'paused' && (remaining > 0 || workerRunning)
}
