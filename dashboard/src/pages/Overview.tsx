import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import {
  DollarSign, MessageSquare, ShoppingCart, TrendingUp, Bot, User, ExternalLink,
  Sparkles, Clock, AlertTriangle, RefreshCw, CheckCircle,
} from 'lucide-react'

const ArrowUp = TrendingUp
import StatCard from '../components/ui/StatCard'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import { UI_ONLY_GUARD } from '../i18n/uiOnly'
import { apiCall } from '../api/client'
import { trackPlatformEvent } from '../lib/platformTelemetry'

// UI_ONLY_GUARD: only static labels use t(); merchant/customer data stays as API values.

// Placeholder chart day keys — localized labels applied in component
const PLACEHOLDER_CHART_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'] as const

const statusVariant = (s: string) =>
  s === 'paid'    ? 'green'  :
  s === 'pending' ? 'amber'  :
  s === 'failed'  ? 'red'    : 'slate'

type Period = 'today' | 'last_7_days' | 'this_month'

const PERIOD_KEYS: Record<Period, 'periodToday' | 'periodLast7Days' | 'periodThisMonth'> = {
  today:        'periodToday',
  last_7_days:  'periodLast7Days',
  this_month:   'periodThisMonth',
}

interface OverviewStats {
  /** Backend echoes the requested period so the UI never re-derives it. */
  period?: Period
  period_label_ar?: string
  /** Period-agnostic field names (preferred). */
  conversations?: number
  orders?: number
  revenue?: number
  /** Total marketing-campaign template sends in the selected window —
   *  surfaced as its own KPI so a big blast is visible even when the
   *  conversation counter barely moves (recipients with open windows). */
  messages_sent?: number
  /** Legacy names — still returned by backend for backwards compat; the
   *  values now reflect the SELECTED period, not literally today. */
  conversations_today: number
  orders_today: number
  revenue_today: number
  today_billable_conversations_count?: number
  today_messages_count?: number
  metric_kind_conversations?: string
  metric_kind_messages?: string
  analytics_timezone?: string
  ai_rate: number
  ai_revenue: number
  ai_orders: number
  recent_conversations: any[]
  recent_orders: any[]
  revenue_chart: { day: string; revenue: number }[]
}

interface WaUsage {
  conversations_used:    number
  conversations_limit:   number
  current_period_conversations_used?: number
  current_period_conversations_limit?: number
  today_conversations_count?: number
  today_billable_conversations_count?: number
  today_in_period_conversations_count?: number
  today_messages_count?: number
  today_pre_renewal_conversations_count?: number
  analytics_timezone?: string
  metric_kind_period_usage?: string
  metric_kind_today_conversations?: string
  remaining_conversations?: number
  lifetime_conversations_used?: number
  period_mode?:          string
  usage_pct:             number
  exceeded:              boolean
  near_limit:            boolean
  warning_70?:           boolean
  warning_90?:           boolean
  marketing_blocked:     boolean
  emergency_stop:        boolean
  unlimited:             boolean
  meta_messaging_limit?:     string | null
  meta_messaging_limit_num?: number | null
  meta_tier_label?:          string | null
  meta_tier_source?:         string | null   // 'meta_graph' | 'dialog360'
  meta_tier_last_synced_at?: string | null   // ISO timestamp from provider sync
  meta_tier_is_stale?:       boolean
  meta_quality_rating?:      string | null
}

const TIER_SOURCE_LABEL: Record<string, string> = {
  meta_graph: 'Meta Cloud API',
  dialog360:  '360dialog (Coexistence)',
}

function trackOverviewCta(target: '/wa-usage' | '/billing') {
  trackPlatformEvent('overview_cta_clicked', { target })
}

function formatSyncedAt(
  iso: string | null | undefined,
  sync: { never: string; unavailable: string; momentsAgo: string; minutesAgo: string; hoursAgo: string },
  locale: string,
): string {
  if (!iso) return sync.never
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return sync.unavailable
    const diffMin = Math.round((Date.now() - d.getTime()) / 60_000)
    if (diffMin < 1)   return sync.momentsAgo
    if (diffMin < 60)  return sync.minutesAgo.replace('{count}', String(diffMin))
    if (diffMin < 1440) return sync.hoursAgo.replace('{count}', String(Math.round(diffMin / 60)))
    return d.toLocaleString(locale, { dateStyle: 'short', timeStyle: 'short' })
  } catch {
    return sync.unavailable
  }
}

export default function Overview() {
  const { t, lang } = useLanguage()
  const ov = t(tr => tr.overview)
  const wu = ov.waUsage
  const mt = wu.metaTier
  const sync = wu.sync
  const diag = wu.diagnostics
  void UI_ONLY_GUARD
  const locale = lang === 'ar' ? 'ar-SA' : 'en-US'
  const periodLabel = (p: Period) => ov[PERIOD_KEYS[p]]
  const chartPlaceholder = PLACEHOLDER_CHART_KEYS.map(k => ({
    day: ov.chartDays[k],
    revenue: 0,
  }))
  const [stats, setStats]     = useState<OverviewStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [waUsage, setWaUsage] = useState<WaUsage | null>(null)
  const [tierRefreshing, setTierRefreshing] = useState(false)
  // Single timeframe controller — every KPI card + the recent lists
  // re-fetch when this changes. We keep ``today`` as the default so the
  // first paint matches the legacy behaviour exactly (and the merchant
  // doesn't see a number flicker if they previously memorised "today").
  const [period, setPeriod] = useState<Period>('today')
  // Diagnostics returned by /refresh-meta-tier. We surface this in a
  // collapsible panel so merchants can see WHY the cached tier doesn't
  // match what Meta Business Manager shows them. Provider-agnostic —
  // works the same whether we're talking to Meta directly or via a
  // relay like 360dialog.
  const [tierDiagnostics, setTierDiagnostics] = useState<{
    updated: boolean
    provider?: string
    reason?: string | null
    diagnostics?: Array<{ path: string; status?: any; error?: string | null; body?: any }>
  } | null>(null)
  const [showTierDiag, setShowTierDiag] = useState(false)
  const [paymentSuccess, setPaymentSuccess] = useState(false)

  useEffect(() => {
    try {
      const params = new URLSearchParams(window.location.search)
      if (params.get('payment') === 'success') {
        setPaymentSuccess(true)
        params.delete('payment')
        const cleanSearch = params.toString()
        const cleanUrl = window.location.pathname + (cleanSearch ? `?${cleanSearch}` : '')
        window.history.replaceState(null, '', cleanUrl)
      }
    } catch { /* noop */ }
  }, [])

  const refreshMetaTier = async () => {
    if (tierRefreshing) return
    setTierRefreshing(true)
    try {
      const result = await apiCall<{
        updated: boolean
        provider?: string
        reason?: string | null
        diagnostics?: any[]
      }>('/whatsapp/refresh-meta-tier', { method: 'POST' }).catch(() => null)
      if (result) setTierDiagnostics(result as any)
      const fresh = await apiCall<WaUsage>('/whatsapp/usage').catch(() => null)
      if (fresh) setWaUsage(fresh)
    } catch {
      // silent — UI already shows "stale" state; merchant can retry
    } finally {
      setTierRefreshing(false)
    }
  }

  useEffect(() => {
    // WhatsApp usage is timeframe-independent (always "this month" per
    // Meta's billing window), so we fetch it once on mount only.
    apiCall<WaUsage>('/whatsapp/usage').then(setWaUsage).catch(() => null)
  }, [])

  useEffect(() => {
    // Re-fetch KPIs whenever the merchant changes the timeframe. We
    // keep the loading state truthy only when we have NO prior data so
    // switching periods doesn't blank the cards mid-fetch — the numbers
    // update in place instead, which is less jarring.
    if (stats === null) setLoading(true)
    Promise.all([
      apiCall<any>(`/store-sync/status?period=${encodeURIComponent(period)}`).catch(() => null),
      apiCall<any>('/store-sync/knowledge').catch(() => null),
    ]).then(([syncStatus]) => {
      let hasData = false
      if (syncStatus) {
        hasData =
          (syncStatus.orders_today ?? syncStatus.orders ?? 0) > 0
          || (syncStatus.recent_orders?.length ?? 0) > 0
        setStats({
          period:               (syncStatus.period as Period) ?? period,
          period_label_ar:      syncStatus.period_label_ar ?? periodLabel(period),
          conversations:        syncStatus.conversations  ?? syncStatus.conversations_today ?? 0,
          orders:               syncStatus.orders         ?? syncStatus.orders_today        ?? 0,
          revenue:              syncStatus.revenue        ?? syncStatus.revenue_today       ?? 0,
          messages_sent:        syncStatus.messages_sent  ?? 0,
          conversations_today:  syncStatus.conversations_today ?? 0,
          orders_today:         syncStatus.orders_today        ?? 0,
          revenue_today:        syncStatus.revenue_today       ?? 0,
          ai_rate:              syncStatus.ai_rate             ?? 0,
          ai_revenue:           syncStatus.ai_revenue          ?? 0,
          ai_orders:            syncStatus.ai_orders           ?? 0,
          recent_conversations: syncStatus.recent_conversations ?? [],
          recent_orders:        syncStatus.recent_orders        ?? [],
          revenue_chart:        syncStatus.revenue_chart        ?? chartPlaceholder,
        })
      }
      trackPlatformEvent('overview_loaded', { has_data: hasData, period })
    }).finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [period])

  const revenueData         = stats?.revenue_chart        ?? chartPlaceholder
  const recentConversations = stats?.recent_conversations ?? []
  const recentOrders        = stats?.recent_orders        ?? []
  const hasRealData         = (stats?.orders_today ?? 0) > 0 || recentOrders.length > 0

  // Pull from the period-agnostic field with a fallback to the legacy
  // ``_today`` aliases so the page still renders correctly against an
  // older backend during a partial deploy.
  const kpiRevenue       = stats?.revenue       ?? stats?.revenue_today       ?? 0
  const kpiConversations = stats?.conversations ?? stats?.conversations_today ?? 0
  const kpiMessagesToday = stats?.today_messages_count ?? 0
  const kpiConversationLabel = period === 'today'
    ? (stats?.metric_kind_conversations === 'billable_conversation_windows'
        ? ov.kpiConversationsToday
        : ov.kpiMessagesToday)
    : ov.kpiConversations
  const kpiConversationValue = period === 'today' && stats?.metric_kind_conversations !== 'billable_conversation_windows'
    ? kpiMessagesToday
    : kpiConversations
  const kpiOrders        = stats?.orders        ?? stats?.orders_today        ?? 0
  const kpiMessagesSent  = stats?.messages_sent ?? 0
  const periodLabelDisplay = lang === 'en'
    ? periodLabel(period)
    : (stats?.period_label_ar ?? periodLabel(period))

  const statusLabel = (s: string) => {
    if (s === 'paid')    return ov.statusPaid
    if (s === 'pending') return ov.statusPending
    if (s === 'failed')  return ov.statusFailed
    return ov.statusCancelled
  }

  return (
    <div className="space-y-6">
      {paymentSuccess && (
        <div className="flex items-center gap-3 bg-emerald-50 border-2 border-emerald-300 rounded-xl px-4 py-3">
          <CheckCircle className="w-5 h-5 text-emerald-600 shrink-0" />
          <p className="text-sm font-semibold text-emerald-900 leading-relaxed flex-1">
            تم تجديد اشتراكك بنجاح — مرحباً بعودتك إلى لوحة التحكم.
          </p>
          <button
            type="button"
            onClick={() => setPaymentSuccess(false)}
            className="text-xs text-emerald-700 hover:text-emerald-900 underline shrink-0"
          >
            إغلاق
          </button>
        </div>
      )}

      {/* Nahla Impact Banner — "موظف مبيعات يعمل 24/7" */}
      <div className="rounded-2xl overflow-hidden bg-gradient-to-l from-brand-600 to-amber-500 p-px">
        <div className="bg-gradient-to-l from-brand-600/10 to-amber-500/10 rounded-2xl px-5 py-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-brand-500/20 flex items-center justify-center shrink-0">
              <Sparkles className="w-5 h-5 text-brand-600" />
            </div>
            <div>
              <p className="text-xs text-white/80 font-medium">{ov.aiSalesLabel}</p>
              <p className="text-2xl font-black text-white leading-none mt-0.5">
                {(stats?.ai_revenue ?? 0).toLocaleString(locale)} <span className="text-sm font-bold text-white/90">{ov.currency}</span>
              </p>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className="text-center hidden sm:block">
              <p className="text-xs text-white/80 font-medium">{ov.aiOrdersLabel}</p>
              <p className="text-lg font-bold text-white">{stats?.ai_orders ?? 0}</p>
            </div>
            <div className="h-8 w-px bg-slate-200 hidden sm:block" />
            <div className="flex items-center gap-1.5 text-xs text-slate-500 bg-white rounded-xl px-3 py-2 border border-slate-200">
              <Clock className="w-3.5 h-3.5 text-brand-500" />
              <span>{ov.salesBot.replace('24/7', '')} <strong className="text-slate-700">24/7</strong></span>
            </div>
            <Link
              to="/app/pricing"
              className="flex items-center gap-1.5 text-xs font-bold bg-brand-500 hover:bg-brand-600 text-white rounded-xl px-3 py-2 transition-colors shrink-0"
            >
              <Sparkles className="w-3.5 h-3.5" />
              {ov.viewPlans}
            </Link>
          </div>
        </div>
      </div>

      {/* WhatsApp Conversation Usage Widget */}
      {waUsage && (() => {
        const isWarning90 = waUsage.warning_90
        const isWarning70 = waUsage.warning_70 && !waUsage.warning_90
        const alertLevel = waUsage.emergency_stop ? 'emergency'
          : waUsage.marketing_blocked ? 'blocked'
          : isWarning90 ? 'warning90'
          : isWarning70 ? 'warning70'
          : 'normal'

        const containerClass = {
          emergency: 'bg-red-50 border-red-300',
          blocked:   'bg-orange-50 border-orange-200',
          warning90: 'bg-red-50 border-red-200',
          warning70: 'bg-amber-50 border-amber-200',
          normal:    'bg-white border-slate-200',
        }[alertLevel]

        const iconColor = {
          emergency: 'text-red-500',
          blocked:   'text-orange-500',
          warning90: 'text-red-500',
          warning70: 'text-amber-500',
          normal:    'text-emerald-500',
        }[alertLevel]

        const barColor = {
          emergency: 'bg-red-500',
          blocked:   'bg-orange-500',
          warning90: 'bg-red-400',
          warning70: 'bg-amber-400',
          normal:    'bg-emerald-500',
        }[alertLevel]

        const pctColor = {
          emergency: 'text-red-600',
          blocked:   'text-orange-600',
          warning90: 'text-red-600',
          warning70: 'text-amber-600',
          normal:    'text-slate-400',
        }[alertLevel]

        return (
        <div className={`rounded-2xl border p-4 ${containerClass}`}>
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-2 flex-wrap">
                <MessageSquare className={`w-4 h-4 shrink-0 ${iconColor}`} />
                <span className="text-sm font-semibold text-slate-700">
                  {wu.periodUsageTitle}
                </span>
                {waUsage.emergency_stop && (
                  <span className="flex items-center gap-1 text-xs font-bold text-red-700 bg-red-100 px-2 py-0.5 rounded-full">
                    <AlertTriangle className="w-3 h-3" /> {wu.emergencyStop}
                  </span>
                )}
                {waUsage.marketing_blocked && !waUsage.emergency_stop && (
                  <span className="flex items-center gap-1 text-xs font-bold text-orange-700 bg-orange-100 px-2 py-0.5 rounded-full">
                    <AlertTriangle className="w-3 h-3" /> {wu.campaignsStopped}
                  </span>
                )}
                {isWarning90 && (
                  <span className="flex items-center gap-1 text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded-full">
                    <AlertTriangle className="w-3 h-3" /> {wu.nearLimit90}
                  </span>
                )}
                {isWarning70 && (
                  <span className="flex items-center gap-1 text-xs font-bold text-amber-600 bg-amber-100 px-2 py-0.5 rounded-full">
                    <AlertTriangle className="w-3 h-3" /> {wu.used70}
                  </span>
                )}
              </div>

              {/* Progress bar */}
              <div className="w-full h-2.5 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all duration-500 ${barColor}`}
                  style={{ width: `${Math.min(waUsage.usage_pct, 100)}%` }}
                />
              </div>

              <div className="flex items-center justify-between mt-1.5">
                <span className="text-xs text-slate-500">
                  {`${(waUsage.current_period_conversations_used ?? waUsage.conversations_used).toLocaleString(locale)} / ${(waUsage.current_period_conversations_limit ?? waUsage.conversations_limit).toLocaleString(locale)} ${wu.conversationsUnit}`}
                </span>
                <span className={`text-xs font-bold ${pctColor}`}>
                  {waUsage.usage_pct}%
                </span>
              </div>
              {typeof waUsage.today_billable_conversations_count === 'number' && (
                <p className="text-xs text-slate-500 mt-1" title={wu.todayConversationsHint}>
                  {wu.todayConversations}:{' '}
                  <span className="font-semibold text-slate-700">
                    {waUsage.today_billable_conversations_count.toLocaleString(locale)}
                  </span>
                  {typeof waUsage.today_in_period_conversations_count === 'number'
                    && waUsage.today_in_period_conversations_count !== waUsage.today_billable_conversations_count && (
                    <span className="text-slate-400">
                      {' '}
                      ({wu.todayInPeriod}: {waUsage.today_in_period_conversations_count.toLocaleString(locale)})
                    </span>
                  )}
                </p>
              )}
              {(waUsage.today_pre_renewal_conversations_count ?? 0) > 0 && (
                <p className="text-[11px] text-slate-400 mt-0.5 leading-relaxed">
                  {wu.preRenewalNote.replace(
                    '{count}',
                    String(waUsage.today_pre_renewal_conversations_count),
                  )}
                </p>
              )}
              <p className="text-[11px] text-slate-400 mt-0.5 leading-relaxed" title={wu.periodUsageHint}>
                {wu.periodUsageHint}
              </p>
            </div>

            {/* Right: details + upgrade CTA */}
            <div className="flex flex-col gap-2 shrink-0">
              <Link
                to="/wa-usage"
                onClick={() => trackOverviewCta('/wa-usage')}
                className="flex items-center gap-1.5 text-xs font-medium text-slate-500 border border-slate-200 hover:border-slate-300 bg-white px-3 py-1.5 rounded-xl transition-colors"
              >
                <ExternalLink className="w-3 h-3" />
                {wu.details}
              </Link>
              {(waUsage.marketing_blocked || isWarning90 || isWarning70) && (
                <Link
                  to="/billing"
                  onClick={() => trackOverviewCta('/billing')}
                  className={`flex items-center gap-1.5 text-xs font-bold px-3 py-1.5 rounded-xl transition-all ${
                    waUsage.marketing_blocked
                      ? 'bg-orange-600 text-white hover:bg-orange-500'
                      : isWarning90
                        ? 'bg-red-500 text-white hover:bg-red-400'
                        : 'bg-amber-500 text-white hover:bg-amber-400'
                  }`}
                >
                  <ArrowUp className="w-3.5 h-3.5" />
                  {wu.upgrade}
                </Link>
              )}
            </div>
          </div>

          {/* Alert messages */}
          {waUsage.emergency_stop && (
            <p className="text-xs text-red-700 mt-2 font-medium bg-red-100 rounded-lg px-3 py-2">
              {wu.emergencyBanner}
            </p>
          )}
          {waUsage.marketing_blocked && !waUsage.emergency_stop && (
            <p className="text-xs text-orange-700 mt-2 font-medium bg-orange-50 rounded-lg px-3 py-2">
              {wu.campaignsBanner}{' '}
              {wu.campaignsBannerNote}{' '}
              <Link to="/billing" onClick={() => trackOverviewCta('/billing')} className="underline mr-1 font-bold">{wu.upgradeLink}</Link>
            </p>
          )}
          {isWarning90 && (
            <p className="text-xs text-red-700 mt-2 font-medium bg-red-50 rounded-lg px-3 py-2">
              {wu.nearLimitBanner}{' '}
              <Link to="/billing" onClick={() => trackOverviewCta('/billing')} className="underline mr-1 font-bold">{wu.upgradeNowLink}</Link>
            </p>
          )}

          {/* Meta Tier Card — source-of-truth + last-synced + force-refresh.
              We deliberately surface ``meta_tier_source`` + ``last_synced_at``
              + ``is_stale`` so the merchant can tell whether the cached
              value is fresh OR a stale read from when the connection was
              first wired. Hides the "stale" badge for unlimited tiers
              where the exact value doesn't matter as much. */}
          {waUsage.meta_tier_label && (
            <div className={`mt-3 rounded-xl p-3 border ${
              waUsage.meta_tier_is_stale
                ? 'bg-amber-50 border-amber-200'
                : 'bg-slate-50 border-slate-200'
            }`}>
              <div className="flex items-start justify-between gap-3 flex-wrap">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <p className="text-xs font-semibold text-slate-600">{mt.title}</p>
                    {waUsage.meta_tier_is_stale && (
                      <span className="text-[10px] font-bold text-amber-700 bg-amber-100 px-1.5 py-0.5 rounded-full">
                        {mt.staleValue}
                      </span>
                    )}
                  </div>
                  <p className="text-sm font-bold text-slate-800 mt-0.5">{waUsage.meta_tier_label}</p>
                  <p className="text-[11px] text-slate-500 mt-0.5 leading-relaxed">
                    {mt.hint}
                  </p>
                  <a
                    href="https://business.facebook.com/wa/manage/phone-numbers/"
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 mt-1 text-[11px] font-medium text-brand-600 hover:text-brand-700"
                  >
                    <ExternalLink className="w-3 h-3" />
                    {mt.verifyInMeta}
                  </a>
                  <div className="flex items-center gap-3 mt-1 text-[11px] text-slate-500 flex-wrap">
                    {waUsage.meta_tier_source && (
                      <span>
                        {mt.source}: <strong className="text-slate-700">
                          {TIER_SOURCE_LABEL[waUsage.meta_tier_source] || waUsage.meta_tier_source}
                        </strong>
                      </span>
                    )}
                    <span>
                      {mt.lastSynced}: <strong className="text-slate-700">{formatSyncedAt(waUsage.meta_tier_last_synced_at, sync, locale)}</strong>
                    </span>
                  </div>
                </div>

                <div className="flex items-center gap-2 shrink-0">
                  {waUsage.meta_quality_rating && (
                    <div className="text-center">
                      <p className="text-[10px] text-slate-500">{mt.numberQuality}</p>
                      <span className={`inline-block mt-0.5 text-xs font-bold px-2 py-0.5 rounded-full ${
                        waUsage.meta_quality_rating === 'GREEN'  ? 'bg-emerald-100 text-emerald-700'
                        : waUsage.meta_quality_rating === 'YELLOW' ? 'bg-amber-100 text-amber-700'
                        : 'bg-red-100 text-red-700'
                      }`}>
                        {waUsage.meta_quality_rating === 'GREEN' ? mt.qualityExcellent : waUsage.meta_quality_rating === 'YELLOW' ? mt.qualityMedium : mt.qualityLow}
                      </span>
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={refreshMetaTier}
                    disabled={tierRefreshing}
                    className="flex items-center gap-1 text-xs font-medium text-slate-600 bg-white hover:bg-slate-50 border border-slate-200 rounded-lg px-2.5 py-1.5 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    aria-label={mt.refreshAriaLabel}
                  >
                    <RefreshCw className={`w-3.5 h-3.5 ${tierRefreshing ? 'animate-spin' : ''}`} />
                    {tierRefreshing ? mt.refreshing : mt.refreshNow}
                  </button>
                </div>
              </div>

              {tierDiagnostics && (
                <div className="mt-3 border-t border-slate-200 pt-2.5">
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <div className="flex items-center gap-2 flex-wrap text-[11px]">
                      {tierDiagnostics.updated ? (
                        <span className="font-bold text-emerald-700 bg-emerald-100 px-2 py-0.5 rounded-full">
                          {diag.updatedFromProvider}
                        </span>
                      ) : (
                        <span className="font-bold text-amber-700 bg-amber-100 px-2 py-0.5 rounded-full">
                          {diag.notFromProvider}
                        </span>
                      )}
                      {tierDiagnostics.provider && (
                        <span className="text-slate-500">
                          {diag.provider}: <strong className="text-slate-700">{tierDiagnostics.provider}</strong>
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => setShowTierDiag(v => !v)}
                      className="text-[11px] font-medium text-brand-600 hover:text-brand-700"
                    >
                      {showTierDiag ? diag.hideDetails : diag.technicalDetails}
                    </button>
                  </div>
                  {showTierDiag && (
                    <div className="mt-2 space-y-1.5">
                      {(tierDiagnostics.diagnostics || []).map((entry, idx) => (
                        <div
                          key={idx}
                          className="text-[11px] bg-slate-900/95 text-slate-100 rounded-lg p-2 font-mono leading-relaxed overflow-x-auto"
                        >
                          <div className="text-emerald-300 break-all">{entry.path}</div>
                          {entry.error ? (
                            <div className="text-red-300 mt-0.5">{diag.errorPrefix}: {entry.error}</div>
                          ) : (
                            <div className="text-slate-300 mt-0.5">
                              <pre className="whitespace-pre-wrap break-all">{JSON.stringify(entry.body, null, 2)}</pre>
                            </div>
                          )}
                        </div>
                      ))}
                      <p className="text-[11px] text-slate-500 leading-relaxed">
                        {diag.tierHint}
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
        )
      })()}

      {/* Unified Timeframe Selector — single source of truth for every KPI
          below + the revenue chart + the "recent" lists. Switching the
          pill re-fetches /store-sync/status with the new ``period`` query
          param; the cards re-render with the response in place (no full
          spinner) to keep scanning smooth. */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-sm font-bold text-slate-700">{ov.sectionTitle}</h2>
        <div
          role="tablist"
          aria-label={ov.sectionTitle}
          className="inline-flex bg-slate-100 dark:bg-slate-800 rounded-xl p-1 text-xs font-medium"
        >
          {(['today', 'last_7_days', 'this_month'] as const).map((p) => (
            <button
              key={p}
              type="button"
              role="tab"
              aria-selected={period === p}
              onClick={() => {
                if (p !== period) {
                  trackPlatformEvent('overview_period_changed', { period: p })
                  setPeriod(p)
                }
              }}
              className={`px-3 py-1.5 rounded-lg transition-colors ${
                period === p
                  ? 'bg-white dark:bg-slate-700 text-slate-900 dark:text-slate-100 shadow-sm'
                  : 'text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200'
              }`}
            >
              {periodLabel(p)}
            </button>
          ))}
        </div>
      </div>

      {/* KPI Cards.
          Layout note: we promote ``رسائل مُرسلة`` (campaign throughput)
          to a top-level card only when the merchant actually ran a
          campaign in the window. Showing a permanent zero-stat to a
          merchant who never sent a campaign would just add visual
          noise; gating it on ``kpiMessagesSent > 0`` keeps the grid
          tidy and turns the 5th column into a "live signal" that a
          blast is going out. */}
      <div className={`grid grid-cols-2 gap-4 ${
        kpiMessagesSent > 0 ? 'lg:grid-cols-5' : 'lg:grid-cols-4'
      }`}>
        <StatCard
          label={ov.kpiRevenue}
          subLabel={periodLabelDisplay}
          value={loading ? '—' : `${kpiRevenue.toLocaleString(locale)} ${ov.currency}`}
          icon={DollarSign}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
        <StatCard
          label={kpiConversationLabel}
          subLabel={periodLabelDisplay}
          value={loading ? '—' : kpiConversationValue.toLocaleString(locale)}
          icon={MessageSquare}
          iconColor="text-blue-600"
          iconBg="bg-blue-50"
        />
        {kpiMessagesSent > 0 && (
          <StatCard
            label={ov.messagesSent}
            subLabel={`${periodLabelDisplay} • Meta`}
            value={loading ? '—' : kpiMessagesSent.toLocaleString(locale)}
            icon={MessageSquare}
            iconColor="text-amber-600"
            iconBg="bg-amber-50"
          />
        )}
        <StatCard
          label={ov.kpiOrders}
          subLabel={periodLabelDisplay}
          value={loading ? '—' : String(kpiOrders)}
          icon={ShoppingCart}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
        <StatCard
          label={ov.kpiAiRate}
          subLabel={periodLabelDisplay}
          value={loading ? '—' : `${(stats?.ai_rate ?? 0).toFixed(1)}%`}
          icon={TrendingUp}
          iconColor="text-purple-600"
          iconBg="bg-purple-50"
        />
      </div>

      {/* Revenue Chart — always 7-day rolling regardless of the KPI
          timeframe above. The framing here is intentionally fixed: the
          area chart's job is "how did the last week feel", not "answer
          the same question the KPI cards do". A separate selector here
          would invite two timeframes drifting out of sync. */}
      <div className="card p-5">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">{ov.chartTitle}</h2>
            <p className="text-xs text-slate-400 mt-0.5">{ov.chartSubtitle}</p>
          </div>
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <AreaChart data={revenueData} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
            <defs>
              <linearGradient id="colorRevenue" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%"  stopColor="#f59e0b" stopOpacity={0.15} />
                <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
              </linearGradient>
            </defs>
            {/* Grid stroke uses a class on the CartesianGrid line so the
                global dark layer (`html.dark .recharts-cartesian-grid line`)
                can flip it to slate-700 without prop tweaks per chart. */}
            <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
            <XAxis dataKey="day" tick={{ fontSize: 11, fill: 'currentColor' }} axisLine={false} tickLine={false} className="text-slate-400 dark:text-slate-500" />
            <YAxis tick={{ fontSize: 11, fill: 'currentColor' }} axisLine={false} tickLine={false} className="text-slate-400 dark:text-slate-500" />
            <Tooltip
              contentStyle={{ fontSize: 12, border: '1px solid #e2e8f0', borderRadius: 8, boxShadow: '0 4px 6px -1px rgb(0 0 0 / .1)' }}
              formatter={(v: number) => [`${v.toLocaleString(locale)} ${ov.currency}`, ov.chartRevenueLabel]}
            />
            <Area type="monotone" dataKey="revenue" stroke="#f59e0b" strokeWidth={2} fill="url(#colorRevenue)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Two-column: Conversations + Orders */}
      <div className="grid lg:grid-cols-2 gap-4">
        {/* Recent Conversations */}
        <div className="card">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900">{ov.recentConvTitle}</h2>
            <a href="/conversations" className="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
              {t(tr => tr.actions.viewAll)} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          {recentConversations.length === 0 ? (
            <div className="py-10 text-center text-xs text-slate-400">
              {loading ? t(tr => tr.common.loading) : ov.noConversationsYet}
            </div>
          ) : (
            <ul className="divide-y divide-slate-100">
              {recentConversations.map((c: any) => (
                <li key={c.id} className="flex items-start gap-3 px-5 py-3 hover:bg-slate-50 transition-colors">
                  <div className="w-8 h-8 bg-slate-100 rounded-full flex items-center justify-center shrink-0 mt-0.5">
                    <span className="text-slate-600 text-xs font-semibold">
                      {String(c.customer ?? '?').split(' ').map((n: string) => n[0]).join('').slice(0, 2)}
                    </span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <p className="text-xs font-medium text-slate-900 truncate">{c.customer}</p>
                      {c.isAI
                        ? <Bot  className="w-3 h-3 text-brand-500 shrink-0" />
                        : <User className="w-3 h-3 text-slate-400 shrink-0" />}
                    </div>
                    <p className="text-xs text-slate-500 truncate mt-0.5">{c.lastMsg}</p>
                  </div>
                  <div className="flex flex-col items-end gap-1 shrink-0">
                    <span className="text-xs text-slate-400">{c.time}</span>
                    <Badge
                      label={c.status === 'active' ? ov.convStatusActive : c.status === 'human' ? ov.convStatusHuman : ov.convStatusClosed}
                      variant={c.status === 'active' ? 'green' : c.status === 'human' ? 'amber' : 'slate'}
                      dot
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Recent Orders */}
        <div className="card">
          <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <h2 className="text-sm font-semibold text-slate-900">{ov.recentOrdTitle}</h2>
            <a href="/orders" className="text-xs text-brand-600 hover:text-brand-700 font-medium flex items-center gap-1">
              {t(tr => tr.actions.viewAll)} <ExternalLink className="w-3 h-3" />
            </a>
          </div>
          {recentOrders.length === 0 ? (
            <div className="py-10 text-center text-xs text-slate-400">
              {loading ? t(tr => tr.common.loading) : ov.noOrdersYet}
            </div>
          ) : (
          <ul className="divide-y divide-slate-100">
            {recentOrders.map((o: any) => (
              <li key={o.id} className="flex items-center gap-3 px-5 py-3 hover:bg-slate-50 transition-colors">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-mono font-medium text-slate-700">{o.id}</span>
                    <Badge label={o.source === 'AI' ? ov.aiBadge : ov.sourceManual} variant={o.source === 'AI' ? 'purple' : 'slate'} />
                  </div>
                  <p className="text-xs text-slate-500 mt-0.5">{o.customer}</p>
                </div>
                <div className="text-end shrink-0">
                  <p className="text-xs font-semibold text-slate-900">{o.amount}</p>
                  <div className="mt-0.5">
                    <Badge label={statusLabel(o.status)} variant={statusVariant(o.status) as 'green' | 'amber' | 'red' | 'slate'} />
                  </div>
                </div>
              </li>
            ))}
          </ul>
          )}
        </div>
      </div>
    </div>
  )
}
