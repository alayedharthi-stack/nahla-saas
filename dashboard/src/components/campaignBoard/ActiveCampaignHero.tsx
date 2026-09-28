import { useState } from 'react'
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, Clock, Eye, MousePointerClick,
  Pause, Pencil, Play, RefreshCw, Stethoscope,
} from 'lucide-react'
import Badge from '../ui/Badge'
import type { CampaignRecord } from '../../api/campaigns'
import type { CampaignsListLabels } from '../../i18n/campaignsListPageLabels'
import { campaignHeroLabels, fill } from '../../i18n/campaignHeroLabels'
import type { Lang } from '../../i18n/types'
import { lifecycleLabelFromList } from '../../i18n/campaignRuntimeLabels'
import { fmtCount, formatClock, formatUtcTime } from './campaignFormat'
import { describeCampaignStatus, type StatusTone } from './campaignStatus'

type BadgeVariant = 'green' | 'amber' | 'red' | 'blue' | 'slate' | 'purple'

const TONE_BADGE: Record<StatusTone, BadgeVariant> = {
  calm: 'blue', action: 'red', neutral: 'amber', done: 'green',
}

const TONE_BOX: Record<StatusTone, string> = {
  calm: 'bg-sky-50 border-sky-200 text-sky-900',
  action: 'bg-red-50 border-red-200 text-red-900',
  neutral: 'bg-amber-50 border-amber-200 text-amber-900',
  done: 'bg-emerald-50 border-emerald-200 text-emerald-900',
}

interface Props {
  campaigns: CampaignRecord[]
  lang: Lang
  dir: 'rtl' | 'ltr'
  list: CampaignsListLabels
  /** When the list was last read from the server (client clock). */
  lastUpdatedAt: Date | null
  busyId: number | null
  onPause: (c: CampaignRecord) => void
  onResume: (c: CampaignRecord) => void
  onEdit: (c: CampaignRecord) => void
  onDiagnose?: (c: CampaignRecord) => void
}

export default function ActiveCampaignHero({
  campaigns, lang, dir, list, lastUpdatedAt, busyId, onPause, onResume, onEdit, onDiagnose,
}: Props) {
  const L = campaignHeroLabels(lang)
  if (campaigns.length === 0) return null
  return (
    <section dir={dir} className="space-y-3" aria-label={L.hero.sectionTitle}>
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-1">
        <h2 className="text-base font-semibold text-slate-900 dark:text-slate-100">{L.hero.sectionTitle}</h2>
        <p className="text-xs text-slate-500 dark:text-slate-400">{L.hero.sectionHint}</p>
      </div>
      {campaigns.map(c => (
        <HeroCard
          key={c.id} c={c} lang={lang} dir={dir} list={list} L={L}
          lastUpdatedAt={lastUpdatedAt} busy={busyId === c.id}
          onPause={onPause} onResume={onResume} onEdit={onEdit} onDiagnose={onDiagnose}
        />
      ))}
    </section>
  )
}

function HeroCard({
  c, lang, dir, list, L, lastUpdatedAt, busy, onPause, onResume, onEdit, onDiagnose,
}: {
  c: CampaignRecord
  lang: Lang
  dir: 'rtl' | 'ltr'
  list: CampaignsListLabels
  L: ReturnType<typeof campaignHeroLabels>
  lastUpdatedAt: Date | null
  busy: boolean
  onPause: (c: CampaignRecord) => void
  onResume: (c: CampaignRecord) => void
  onEdit: (c: CampaignRecord) => void
  onDiagnose?: (c: CampaignRecord) => void
}) {
  const [showDiag, setShowDiag] = useState(false)
  const s = c.stats
  const status = describeCampaignStatus(c, L, list, lang)
  const lifecycleKey = c.lifecycle || c.status
  const lifecycleLabel = lifecycleLabelFromList(lifecycleKey, c.status, list)

  const total = s?.total_recipients ?? c.audience_count ?? 0
  const excluded = s?.skipped ?? 0
  const planned = Math.max(0, total - excluded)
  const reached = s?.reached ?? s?.delivered ?? 0
  const read = s?.read ?? 0
  const clicked = s?.clicked ?? 0
  const remaining = s?.queued ?? 0
  const acceptedUnconfirmed = s?.not_delivered_yet ?? 0
  const failedFinal = s?.failed_total ?? ((s?.failed ?? 0) + (s?.failed_after_accept ?? 0))
  const recipientLimit = s?.recipients_marketing_limit ?? 0
  const uncertain = s?.uncertain ?? 0
  const messages = s?.messages_attempted ?? 0
  const pct = planned > 0 ? Math.min(100, Math.round((reached / planned) * 100)) : 0

  const click = c.click_tracking
  const clickValue =
    click?.status === 'available' ? fmtCount(clicked, lang)
      : click?.status === 'pending' ? L.hero.clickPending
        : L.hero.clickUnavailable
  const clickNote = click ? (L.hero.clickReasons[click.reason] || null) : null
  const clickPartial = click?.status === 'available' && click.partial
    ? fill(L.hero.clickPartial, { n: fmtCount(click.trackable_messages, lang) }) : null

  const canPause = c.status === 'active'
  const canResume = c.status === 'paused' && lifecycleKey !== 'offer_expired'
  const canEdit = c.status !== 'active'

  return (
    <article className={`card p-4 sm:p-5 space-y-4 border-s-4 ${status.tone === 'action' ? 'border-s-red-400' : status.tone === 'calm' ? 'border-s-sky-400' : status.tone === 'done' ? 'border-s-emerald-400' : 'border-s-amber-400'}`}>
      {/* Header: name, template, badge, actions */}
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base sm:text-lg font-semibold text-slate-900 dark:text-slate-100 truncate">{c.name}</h3>
            <Badge label={lifecycleLabel} variant={TONE_BADGE[status.tone]} dot />
            {(c.content_revision ?? 1) > 1 && (
              <span className="text-[11px] text-slate-500">{fill(L.hero.facts.revision, { n: c.content_revision ?? 1 })}</span>
            )}
          </div>
          <p className="text-[11px] text-slate-400 font-mono mt-0.5 truncate">{c.template_name?.replace(/_/g, ' ')}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2 shrink-0">
          {canPause && (
            <button type="button" disabled={busy} onClick={() => onPause(c)}
              className="inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-lg border border-slate-300 text-slate-700 hover:bg-slate-50 disabled:opacity-50">
              <Pause className="w-3.5 h-3.5" /> {L.hero.actions.pause}
            </button>
          )}
          {canResume && (
            <button type="button" disabled={busy} onClick={() => onResume(c)}
              className="btn-primary text-xs py-1.5 disabled:opacity-50">
              <Play className="w-3.5 h-3.5" /> {busy ? L.hero.actions.resuming : L.hero.actions.resume}
            </button>
          )}
          {canEdit && (
            <button type="button" disabled={busy} onClick={() => onEdit(c)}
              className={`inline-flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded-lg border disabled:opacity-50 ${lifecycleKey === 'offer_expired' ? 'border-red-300 text-red-700 hover:bg-red-50' : 'border-slate-300 text-slate-700 hover:bg-slate-50'}`}>
              <Pencil className="w-3.5 h-3.5" /> {L.hero.actions.edit}
            </button>
          )}
          {onDiagnose && (
            <button type="button" onClick={() => onDiagnose(c)}
              className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg text-slate-500 hover:text-slate-800 hover:bg-slate-50">
              <Stethoscope className="w-3.5 h-3.5" /> {L.hero.actions.diagnose}
            </button>
          )}
        </div>
      </div>

      {/* Status sentence */}
      <div className={`rounded-lg border px-3 py-2.5 text-sm flex items-start gap-2 ${TONE_BOX[status.tone]}`} role={status.tone === 'action' ? 'alert' : undefined}>
        {status.tone === 'action' ? <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          : status.tone === 'done' ? <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" />
            : <Clock className="w-4 h-4 mt-0.5 shrink-0" />}
        <div className="min-w-0">
          <p className="font-medium leading-snug">{status.text}</p>
          {status.detail && <p className="text-xs opacity-90 mt-0.5 leading-snug">{status.detail}</p>}
        </div>
      </div>

      {/* Message preview + progress */}
      <div className="grid gap-4 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-wide text-slate-400 mb-1">{L.hero.messagePreview}</p>
          <div className="rounded-xl bg-[#e5ddd5] dark:bg-slate-700 p-3">
            <div className="bg-white dark:bg-slate-800 rounded-2xl rounded-bl-sm shadow-sm p-3 text-xs text-slate-800 dark:text-slate-100 whitespace-pre-line leading-relaxed max-h-40 overflow-hidden">
              {c.template_body || c.template_name}
            </div>
          </div>
          {c.offer_expires_at && (
            <p className="text-[11px] text-slate-500 mt-1">{fill(L.hero.facts.offerExpires, { time: formatUtcTime(c.offer_expires_at, lang) })}</p>
          )}
        </div>
        <div className="space-y-3 min-w-0">
          <div>
            <div className="flex items-baseline justify-between text-xs text-slate-600 dark:text-slate-300 mb-1">
              <span className="font-medium">{fill(L.hero.progress, { done: fmtCount(reached, lang), total: fmtCount(planned, lang) })}</span>
              <span className="text-slate-400">{pct}%</span>
            </div>
            <div className="h-2.5 w-full rounded-full bg-slate-100 dark:bg-slate-700 overflow-hidden" aria-hidden>
              <div className="h-full bg-emerald-500 transition-all" style={{ width: `${pct}%` }} />
            </div>
            <p className="text-[11px] text-slate-400 mt-1">{fill(L.hero.uniqueNote, { messages: fmtCount(messages, lang) })}</p>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <PrimaryTile icon={<CheckCircle2 className="w-4 h-4 text-emerald-600" />} label={L.hero.primary.reached} value={fmtCount(reached, lang)} />
            <PrimaryTile icon={<Eye className="w-4 h-4 text-sky-600" />} label={L.hero.primary.read} value={fmtCount(read, lang)} />
            <PrimaryTile icon={<MousePointerClick className="w-4 h-4 text-violet-600" />} label={L.hero.primary.clicked}
              value={clickValue} sub={clickPartial} muted={click?.status !== 'available'} title={clickNote || undefined} />
          </div>
          <dl className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-1 text-[11px] text-slate-500 dark:text-slate-400">
            <Secondary label={L.hero.secondary.remaining} value={fmtCount(remaining, lang)} />
            <Secondary label={L.hero.secondary.acceptedUnconfirmed} value={fmtCount(acceptedUnconfirmed, lang)} />
            <Secondary label={L.hero.secondary.failedFinal} value={fmtCount(failedFinal, lang)} strong={failedFinal > 0} />
            <Secondary label={L.hero.secondary.excluded} value={fmtCount(excluded, lang)} />
            {recipientLimit > 0 && <Secondary label={L.hero.secondary.recipientLimit} value={fmtCount(recipientLimit, lang)} />}
            {uncertain > 0 && <Secondary label={L.hero.secondary.uncertain} value={fmtCount(uncertain, lang)} strong />}
          </dl>
        </div>
      </div>

      {/* Footer: freshness + diagnostics toggle */}
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400 border-t border-slate-100 dark:border-slate-700 pt-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="inline-flex items-center gap-1"><RefreshCw className="w-3 h-3" />
            {lastUpdatedAt ? fill(L.hero.lastUpdated, { time: formatClock(lastUpdatedAt, lang) }) : '—'}
          </span>
          {c.last_provider_event_at && (
            <span>{fill(L.hero.lastProviderEvent, { time: formatUtcTime(c.last_provider_event_at, lang) })}</span>
          )}
          <span className="hidden sm:inline">{L.hero.providerLag}</span>
        </div>
        <button type="button" onClick={() => setShowDiag(v => !v)}
          className="inline-flex items-center gap-1 text-slate-500 hover:text-slate-800">
          {showDiag ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          {showDiag ? L.hero.diagnosticsHide : L.hero.diagnostics}
        </button>
      </div>
      {showDiag && <Diagnostics c={c} lang={lang} L={L} list={list} />}
    </article>
  )
}

function PrimaryTile({ icon, label, value, sub, muted, title }: {
  icon: React.ReactNode; label: string; value: string; sub?: string | null; muted?: boolean; title?: string
}) {
  return (
    <div className="rounded-lg border border-slate-100 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-800/40 p-2.5 min-w-0" title={title}>
      <div className="flex items-center gap-1.5 text-[11px] text-slate-500 dark:text-slate-400">{icon}<span className="truncate">{label}</span></div>
      <p className={`mt-1 font-bold tracking-tight ${muted ? 'text-sm text-slate-400' : 'text-2xl text-slate-900 dark:text-slate-100'}`}>{value}</p>
      {sub && <p className="text-[10px] text-slate-400 truncate">{sub}</p>}
    </div>
  )
}

function Secondary({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2 min-w-0">
      <dt className="truncate">{label}</dt>
      <dd className={`font-medium tabular-nums ${strong ? 'text-slate-800 dark:text-slate-200' : ''}`}>{value}</dd>
    </div>
  )
}

function Diagnostics({ c, lang, L, list }: {
  c: CampaignRecord; lang: Lang; L: ReturnType<typeof campaignHeroLabels>; list: CampaignsListLabels
}) {
  const f = L.hero.facts
  const ex = c.execution
  const cw = c.capacity_wait
  const rows: Array<[string, string]> = []
  rows.push([ex?.worker_running ? f.workerRunning : f.workerIdle, ex?.heartbeat_at ? `${f.heartbeat}: ${formatUtcTime(ex.heartbeat_at, lang)}` : f.none])
  if (ex?.pause_reason) rows.push([f.pauseReason, `${list.sendHealth.pauseReasons[ex.pause_reason] || ex.pause_reason} (${ex.pause_reason})`])
  if (ex?.pause_detail) rows.push([f.pauseDetail, ex.pause_detail])
  if (cw && (cw.budget != null || cw.limit != null)) {
    rows.push([L.hero.secondary.remaining, fill(f.capacity, {
      used: fmtCount(cw.used_24h ?? 0, lang), budget: fmtCount(cw.budget ?? 0, lang), limit: fmtCount(cw.limit ?? 0, lang),
    })])
  }
  if (c.stall_recovery?.attempt) rows.push([fill(f.stallAttempt, { n: c.stall_recovery.attempt }), c.stall_recovery.next_eligible_at ? formatUtcTime(c.stall_recovery.next_eligible_at, lang) : f.none])
  rows.push([fill(f.revision, { n: c.content_revision ?? 1 }), c.offer_expires_at ? fill(f.offerExpires, { time: formatUtcTime(c.offer_expires_at, lang) }) : f.noExpiry])
  const errors = c.stats?.error_breakdown ?? []
  return (
    <div className="rounded-lg bg-slate-50 dark:bg-slate-800/60 border border-slate-200 dark:border-slate-700 p-3 text-[11px] text-slate-600 dark:text-slate-300 space-y-2">
      <dl className="grid gap-1 sm:grid-cols-2">
        {rows.map(([k, v], i) => (
          <div key={i} className="flex flex-col sm:flex-row sm:gap-2 min-w-0">
            <dt className="text-slate-400 shrink-0">{k}</dt>
            <dd className="font-mono break-all">{v}</dd>
          </div>
        ))}
      </dl>
      {errors.length > 0 && (
        <div>
          <p className="text-slate-400 mb-1">{f.errorBreakdown}</p>
          <ul className="space-y-0.5">
            {errors.map((e, i) => (
              <li key={i}>• {list.sendHealth.errorPhase[e.phase]}: {e.label_ar} — {fmtCount(e.count, lang)} <span className="font-mono text-slate-400">[{e.key}]</span></li>
            ))}
          </ul>
        </div>
      )}
      {(c.dispatch_errors?.length ?? 0) > 0 && (
        <div>
          <p className="text-slate-400 mb-1">{f.dispatchErrors}</p>
          <ul className="space-y-0.5 font-mono break-all">
            {c.dispatch_errors.map((e, i) => <li key={i}>• {e}</li>)}
          </ul>
        </div>
      )}
    </div>
  )
}
