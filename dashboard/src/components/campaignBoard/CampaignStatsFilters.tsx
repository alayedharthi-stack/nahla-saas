import { useCallback, useEffect, useMemo, useState } from 'react'
import { BarChart2, CheckCircle2, Eye, MousePointerClick, Send, XCircle, Hourglass, UserX } from 'lucide-react'
import {
  campaignsApi, type CampaignFilteredStats, type CampaignRecord, type CampaignStatsPeriod,
} from '../../api/campaigns'
import { campaignHeroLabels, fill } from '../../i18n/campaignHeroLabels'
import type { Lang } from '../../i18n/types'
import { fmtCount, formatUtcTime, isLiveCampaign } from './campaignFormat'

type ScopeSel = 'all' | 'running' | 'campaign'

interface Props {
  campaigns: CampaignRecord[]
  lang: Lang
  dir: 'rtl' | 'ltr'
  /** Bumped by the parent whenever the campaign list was refreshed. */
  refreshToken: number
  /** Preselect one campaign (detail view); its stats show alone by default. */
  initialCampaignId?: number | null
}

const PERIODS: CampaignStatsPeriod[] = ['today', 'week', 'month', 'year', 'all', 'custom']

export default function CampaignStatsFilters({ campaigns, lang, dir, refreshToken, initialCampaignId }: Props) {
  const L = campaignHeroLabels(lang)
  const [scopeSel, setScopeSel] = useState<ScopeSel>(initialCampaignId ? 'campaign' : 'all')
  const [campaignId, setCampaignId] = useState<number | null>(initialCampaignId ?? null)
  const [period, setPeriod] = useState<CampaignStatsPeriod>('all')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [data, setData] = useState<CampaignFilteredStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const liveCount = useMemo(() => campaigns.filter(isLiveCampaign).length, [campaigns])
  const scope = scopeSel === 'campaign' ? (campaignId ?? 'all') : scopeSel

  const load = useCallback(async (signal?: AbortSignal) => {
    if (period === 'custom' && (!from || !to)) return
    setLoading(true)
    try {
      const res = await campaignsApi.stats({
        scope,
        period,
        from: period === 'custom' && from ? new Date(from).toISOString() : undefined,
        to: period === 'custom' && to ? new Date(to).toISOString() : undefined,
      }, signal ? { signal } : undefined)
      setData(res)
      setError(null)
    } catch (e) {
      if (signal?.aborted) return
      setError(e instanceof Error ? e.message : L.filters.loadFailed)
    } finally {
      setLoading(false)
    }
  }, [scope, period, from, to, L.filters.loadFailed])

  useEffect(() => {
    const ctrl = new AbortController()
    void load(ctrl.signal)
    return () => ctrl.abort()
  }, [load, refreshToken])

  const r = data?.recipients
  const clickAvailable = data?.click_tracking.status === 'available'
  const selectCls = 'text-xs rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2.5 py-1.5 text-slate-700 dark:text-slate-200'

  return (
    <section dir={dir} className="card p-4 sm:p-5 space-y-3" aria-label={L.filters.title}>
      <div className="flex flex-col gap-2 lg:flex-row lg:items-end lg:justify-between">
        <div className="flex items-center gap-2">
          <BarChart2 className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">{L.filters.title}</h2>
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1 text-[11px] text-slate-500">
            {L.filters.scope}
            <select className={selectCls} value={scopeSel} onChange={e => setScopeSel(e.target.value as ScopeSel)}>
              <option value="all">{L.filters.scopeAll}</option>
              <option value="running">{L.filters.scopeRunning} ({fmtCount(liveCount, lang)})</option>
              <option value="campaign">{L.filters.scopeCampaign}</option>
            </select>
          </label>
          {scopeSel === 'campaign' && (
            <label className="flex flex-col gap-1 text-[11px] text-slate-500">
              {L.filters.scopeCampaign}
              <select className={`${selectCls} max-w-[16rem]`} value={campaignId ?? ''} onChange={e => setCampaignId(e.target.value ? Number(e.target.value) : null)}>
                <option value="">—</option>
                {campaigns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
            </label>
          )}
          <label className="flex flex-col gap-1 text-[11px] text-slate-500">
            {L.filters.period}
            <select className={selectCls} value={period} onChange={e => setPeriod(e.target.value as CampaignStatsPeriod)}>
              {PERIODS.map(p => <option key={p} value={p}>{L.filters.periods[p]}</option>)}
            </select>
          </label>
          {period === 'custom' && (
            <>
              <label className="flex flex-col gap-1 text-[11px] text-slate-500">
                {L.filters.from}
                <input type="datetime-local" className={selectCls} value={from} onChange={e => setFrom(e.target.value)} />
              </label>
              <label className="flex flex-col gap-1 text-[11px] text-slate-500">
                {L.filters.to}
                <input type="datetime-local" className={selectCls} value={to} onChange={e => setTo(e.target.value)} />
              </label>
            </>
          )}
        </div>
      </div>

      {error && <p className="text-xs text-red-600">{error}</p>}
      <div className={`grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2 ${loading ? 'opacity-60' : ''}`} aria-busy={loading}>
        <Tile icon={<CheckCircle2 className="w-4 h-4 text-emerald-600" />} label={L.filters.tiles.reached} value={fmtCount(r?.reached, lang)} primary />
        <Tile icon={<Eye className="w-4 h-4 text-sky-600" />} label={L.filters.tiles.read} value={fmtCount(r?.read, lang)} primary />
        <Tile icon={<MousePointerClick className="w-4 h-4 text-violet-600" />} label={L.filters.tiles.clicked}
          value={clickAvailable ? fmtCount(r?.clicked, lang) : L.filters.notAvailable} muted={!clickAvailable} primary />
        <Tile icon={<Send className="w-4 h-4 text-slate-500" />} label={L.filters.tiles.accepted} value={fmtCount(r?.accepted, lang)} />
        <Tile icon={<XCircle className="w-4 h-4 text-red-500" />} label={L.filters.tiles.failedFinal} value={fmtCount(r?.failed_final, lang)} />
        <Tile icon={<Hourglass className="w-4 h-4 text-amber-500" />} label={L.filters.tiles.remaining} value={fmtCount(data?.remaining, lang)} title={L.filters.remainingNote} />
        <Tile icon={<UserX className="w-4 h-4 text-slate-400" />} label={L.filters.tiles.excluded} value={fmtCount(r?.excluded, lang)} />
        <Tile icon={<UserX className="w-4 h-4 text-slate-400" />} label={L.filters.tiles.recipientLimit} value={fmtCount(r?.recipient_limit, lang)} />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400">
        <span>{L.filters.basisNote}</span>
        <span>{data?.as_of ? fill(L.filters.asOf, { time: formatUtcTime(data.as_of, lang) }) : (loading ? L.filters.loading : '')}</span>
      </div>
    </section>
  )
}

function Tile({ icon, label, value, primary, muted, title }: {
  icon: React.ReactNode; label: string; value: string; primary?: boolean; muted?: boolean; title?: string
}) {
  return (
    <div className={`rounded-lg border p-2.5 min-w-0 ${primary ? 'border-slate-200 dark:border-slate-600 bg-white dark:bg-slate-800' : 'border-slate-100 dark:border-slate-700 bg-slate-50/60 dark:bg-slate-800/40'}`} title={title}>
      <div className="flex items-center gap-1.5 text-[11px] text-slate-500 dark:text-slate-400">{icon}<span className="truncate">{label}</span></div>
      <p className={`mt-1 font-bold tracking-tight ${muted ? 'text-sm text-slate-400' : primary ? 'text-2xl text-slate-900 dark:text-slate-100' : 'text-lg text-slate-700 dark:text-slate-200'}`}>{value}</p>
    </div>
  )
}
