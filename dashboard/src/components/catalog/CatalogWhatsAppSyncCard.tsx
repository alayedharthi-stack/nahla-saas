import { AlertTriangle, Clock, Loader2, RefreshCw } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  catalogApi,
  type WhatsappCatalogSyncPhase,
  type WhatsappCatalogSyncStatus,
} from '../../api/catalog'
import { useLanguage } from '../../i18n/context'
import { resolveCatalogBlocker } from '../../i18n/catalogRuntimeLabels'
import type { Lang } from '../../i18n/types'

const FOLLOW_PHASES = new Set<WhatsappCatalogSyncPhase>([
  'queued',
  'syncing',
  'pending_verification',
  'retrying',
])
const POLL_MS_FAST = 2500
const POLL_MS_SLOW = 15000
const FAST_WINDOW_MS = 60_000

function localeTag(lang: Lang): string {
  return lang === 'en' ? 'en-US' : 'ar-SA'
}

function fmtCount(n: number, lang: Lang): string {
  return n.toLocaleString(localeTag(lang))
}

function fmtAt(iso: string | null, lang: Lang): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString(localeTag(lang))
  } catch {
    return iso
  }
}

function phaseLabel(
  phase: WhatsappCatalogSyncPhase,
  copy: ReturnType<typeof useWhatsappSyncCopy>,
): string {
  if (phase === 'queued') return copy.phaseQueued
  if (phase === 'syncing') return copy.phaseSyncing
  if (phase === 'published') return copy.phasePublished
  if (phase === 'pending_verification') return copy.phasePendingVerification
  if (phase === 'blocked') return copy.phaseBlocked
  if (phase === 'retrying') return copy.phaseRetrying
  if (phase === 'needs_attention') return copy.phaseNeedsAttention
  return copy.phaseIdle
}

function useWhatsappSyncCopy() {
  const { tStatic } = useLanguage()
  return tStatic(tr => tr.catalogMgmt.whatsappSync)
}

function fmtSeconds(value: number | null | undefined, lang: Lang): string {
  if (value === null || value === undefined) return '—'
  return Math.round(value).toLocaleString(localeTag(lang))
}

function linkLine(
  status: WhatsappCatalogSyncStatus,
  copy: ReturnType<typeof useWhatsappSyncCopy>,
  lang: Lang,
): { text: string; tone: 'ok' | 'warn' | 'unknown' } {
  const link = status.catalog_link
  const at = link?.evidence_at ? fmtAt(link.evidence_at, lang) : '—'
  if (link?.state === 'linked') {
    return { text: copy.linkLinked.replace('{at}', at), tone: 'ok' }
  }
  if (link?.state === 'not_linked') {
    return { text: copy.linkNotLinked.replace('{at}', at), tone: 'warn' }
  }
  return {
    text: status.catalog_configured ? `${copy.linkUnknown} ${copy.configuredNotProven}` : copy.linkUnknown,
    tone: 'unknown',
  }
}

export default function CatalogWhatsAppSyncCard() {
  const { lang } = useLanguage()
  const copy = useWhatsappSyncCopy()
  const [status, setStatus] = useState<WhatsappCatalogSyncStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const inFlight = useRef(false)
  const lockUntil = useRef(0)
  const refreshInFlight = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return
    refreshInFlight.current = true
    try {
      const next = await catalogApi.whatsappSyncStatus()
      if (!mounted.current) return
      setStatus(next)
      setError(null)
    } catch {
      if (!mounted.current) return
      setError(copy.loadFailed)
    } finally {
      refreshInFlight.current = false
      if (mounted.current) setLoading(false)
    }
  }, [copy.loadFailed])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    const phase = status?.phase
    if (!phase || !FOLLOW_PHASES.has(phase)) return
    let cancelled = false
    let timeoutId = 0
    const started = Date.now()
    const arm = (delay: number) => {
      timeoutId = window.setTimeout(() => {
        if (cancelled) return
        void refresh()
        if (cancelled) return
        const elapsed = Date.now() - started
        arm(elapsed < FAST_WINDOW_MS ? POLL_MS_FAST : POLL_MS_SLOW)
      }, delay)
    }
    arm(POLL_MS_FAST)
    return () => {
      cancelled = true
      window.clearTimeout(timeoutId)
    }
  }, [status?.phase, refresh])

  const autoSyncOn = status?.auto_sync_enabled !== false
  const ready = Boolean(status?.ready)
  const canEnqueue = ready && autoSyncOn && !busy && !loading

  const onSync = async () => {
    if (!canEnqueue || inFlight.current || Date.now() < lockUntil.current) return
    inFlight.current = true
    lockUntil.current = Date.now() + 2000
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await catalogApi.enqueueWhatsappSync(true)
      if (!result.queued) {
        setError(resolveCatalogBlocker(result as { blocker_code?: string | null; action_code?: string | null; message_ar?: string | null; action_ar?: string | null }, copy, lang) || copy.enqueueFailed)
      } else {
        setNotice(copy.queued)
      }
      await refresh()
    } catch (e: unknown) {
      const err = e as { message?: string; detail?: { blocker_code?: string | null; action_code?: string | null; message_ar?: string; action_ar?: string } }
      const detail = err?.detail
      setError(resolveCatalogBlocker(detail, copy, lang) || err?.message || copy.enqueueFailed)
      setNotice(null)
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const counts = status?.counts
  const blockerText = status && !ready
    ? resolveCatalogBlocker(status, copy, lang)
    : null

  return (
    <section className="bg-white rounded-2xl border border-slate-200 shadow-sm p-5 space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-4">
        <div className="min-w-0 space-y-2">
          <h2 className="text-base font-bold text-slate-900">{copy.title}</h2>
          {loading ? (
            <p className="text-sm text-slate-500 flex items-center gap-2">
              <Loader2 className="w-4 h-4 animate-spin" />
            </p>
          ) : (
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1 text-sm text-slate-700">
              <div>
                <dt className="sr-only">{copy.title}</dt>
                <dd className="font-semibold">
                  {status ? phaseLabel(status.phase, copy) : '—'}
                </dd>
              </div>
              <div>
                <dd>
                  {status?.last_success_at
                    ? copy.lastSuccess.replace('{at}', fmtAt(status.last_success_at, lang))
                    : (status?.catalog_configured
                        && (status.queue_count ?? counts?.pending ?? 0) === 0
                        && (status.meta_available_count ?? 0) > 0
                      ? null
                      : copy.lastSuccessNever)}
                </dd>
              </div>
              <div>
                <dd>
                  {status?.catalog_configured
                    ? copy.availableCount.replace(
                        '{count}',
                        fmtCount(status.meta_available_count ?? counts?.synced ?? 0, lang),
                      )
                    : copy.eligible.replace('{count}', fmtCount(counts?.eligible ?? 0, lang))}
                </dd>
              </div>
              <div>
                <dd>
                  {(status?.queue_count ?? counts?.pending ?? 0) === 0
                    ? copy.queueEmpty
                    : copy.pending.replace(
                        '{count}',
                        fmtCount(status?.queue_count ?? counts?.pending ?? 0, lang),
                      )}
                </dd>
              </div>
              <div>
                <dd>{copy.synced.replace('{count}', fmtCount(counts?.synced ?? 0, lang))}</dd>
              </div>
              <div>
                <dd>{copy.failed.replace('{count}', fmtCount((counts?.failed ?? 0) + (counts?.blocked ?? 0), lang))}</dd>
              </div>
            </dl>
          )}
        </div>
        <button
          type="button"
          onClick={() => void onSync()}
          disabled={!canEnqueue}
          className="inline-flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-semibold px-4 py-2.5 rounded-xl text-sm transition shadow-sm shrink-0 min-h-[44px]"
        >
          {busy
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <RefreshCw className="w-4 h-4" />}
          {copy.button}
        </button>
      </div>

      {status && (() => {
        const line = linkLine(status, copy, lang)
        const cls = line.tone === 'ok'
          ? 'bg-emerald-50 border-emerald-200 text-emerald-900'
          : line.tone === 'warn'
            ? 'bg-amber-50 border-amber-200 text-amber-900'
            : 'bg-slate-50 border-slate-200 text-slate-800'
        return (
          <div className={`flex items-start gap-2 border rounded-xl p-3 text-sm ${cls}`}>
            {line.tone === 'ok' ? <Clock className="w-4 h-4 mt-0.5 shrink-0" /> : <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />}
            <p>
              {line.text}
              {status.catalog_link?.stale ? ` · ${copy.linkStale}` : ''}
            </p>
          </div>
        )
      })()}
      {status?.stages && (
        <dl className="text-xs text-slate-600 space-y-1 border border-slate-100 rounded-xl p-3">
          <dt className="font-semibold text-slate-700">{copy.stagesTitle}</dt>
          <dd>
            {status.stages.source.last_sync_at
              ? copy.stageSource.replace('{at}', fmtAt(status.stages.source.last_sync_at, lang))
              : copy.stageSourceNever}
          </dd>
          <dd>
            {copy.stagePublish
              .replace('{verified}', fmtCount(status.stages.publish.verified_in_meta, lang))
              .replace('{waiting}', fmtCount(status.stages.publish.waiting, lang))
              .replace('{rejected}', fmtCount(status.stages.publish.rejected_or_blocked, lang))}
          </dd>
          {(status.stages.retirement.pending > 0 || status.stages.retirement.exhausted > 0) && (
            <dd>
              {copy.stageRetirement
                .replace('{pending}', fmtCount(status.stages.retirement.pending, lang))
                .replace('{exhausted}', fmtCount(status.stages.retirement.exhausted, lang))}
            </dd>
          )}
          <dd>{copy.stageVisibility}</dd>
        </dl>
      )}
      {status?.latency && (
        <dl className="text-xs text-slate-600 space-y-1">
          <dt className="font-semibold text-slate-700">{copy.latencyTitle}</dt>
          {status.latency.platform.n === 0 && status.latency.channel.n === 0 && status.latency.waiting.n === 0 ? (
            <dd>{copy.latencyNone}</dd>
          ) : (
            <>
              {status.latency.platform.n > 0 && (
                <dd>
                  {copy.latencyPlatform
                    .replace('{p50}', fmtSeconds(status.latency.platform.p50_seconds, lang))
                    .replace('{max}', fmtSeconds(status.latency.platform.max_seconds, lang))}
                </dd>
              )}
              {status.latency.channel.n > 0 && (
                <dd>
                  {copy.latencyChannel
                    .replace('{p50}', fmtSeconds(status.latency.channel.p50_seconds, lang))
                    .replace('{max}', fmtSeconds(status.latency.channel.max_seconds, lang))}
                </dd>
              )}
              {status.latency.waiting.n > 0 && (
                <dd>{copy.latencyWaiting.replace('{max}', fmtSeconds(status.latency.waiting.max_seconds, lang))}</dd>
              )}
            </>
          )}
        </dl>
      )}
      {status && status.auto_sync_enabled === false && (
        <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl p-3 text-sm text-amber-900">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <p>{copy.autoSyncOff}</p>
        </div>
      )}
      {!ready && blockerText && (
        <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl p-3 text-sm text-amber-900">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <p>{blockerText}</p>
        </div>
      )}
      {notice && (
        <div className="flex items-start gap-2 bg-slate-50 border border-slate-200 rounded-xl p-3 text-sm text-slate-800">
          <Clock className="w-4 h-4 mt-0.5 shrink-0" />
          <p>{notice}</p>
        </div>
      )}
      <p className="text-xs text-slate-500 leading-relaxed">{copy.verifyNote}</p>
      {error && (
        <p className="text-sm text-rose-700 bg-rose-50 border border-rose-200 rounded-xl p-3">{error}</p>
      )}
      {status?.failures && status.failures.length > 0 && (
        <ul className="text-xs text-slate-600 space-y-1">
          {status.failures.slice(0, 5).map((row) => {
            const action = row.action_code ? copy.actions[row.action_code] : undefined
            return (
              <li key={row.product_id}>
                {row.title || `#${row.product_id}`}: {row.error_summary}
                {action ? ` — ${copy.failedAction.replace('{action}', action)}` : ''}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
