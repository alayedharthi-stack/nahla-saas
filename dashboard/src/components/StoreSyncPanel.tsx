/**
 * StoreSyncPanel.tsx
 * ───────────────────
 * Shows the store knowledge sync status inside the Store Integration page.
 * Displays:
 *   - Last sync time + sync version
 *   - Entity counts (products / orders / coupons / categories)
 *   - Sync progress / running state
 *   - Manual "Sync Now" button
 *   - Error display
 */
import { useCallback, useEffect, useState } from 'react'
import {
  AlertCircle,
  BarChart3,
  BookOpen,
  CheckCircle2,
  Loader2,
  PackageSearch,
  RefreshCw,
  ShoppingCart,
  Tag,
  Zap,
} from 'lucide-react'
import { storeSyncApi, type KnowledgeOverview, type SyncStatus } from '../api/storeSync'

// ── Helper ────────────────────────────────────────────────────────────────────

function relativeTime(iso: string | null): string {
  if (!iso) return 'لم تتم بعد'
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60)   return 'منذ ثوانٍ'
  if (diff < 3600) return `منذ ${Math.floor(diff / 60)} دقيقة`
  if (diff < 86400) return `منذ ${Math.floor(diff / 3600)} ساعة`
  return `منذ ${Math.floor(diff / 86400)} يوم`
}

// ── Stat tile ─────────────────────────────────────────────────────────────────

function StatTile({
  icon: Icon, label, value, color = 'text-brand-500',
}: { icon: React.ElementType; label: string; value: number; color?: string }) {
  return (
    <div className="bg-slate-50 rounded-xl p-3.5 flex items-center gap-3">
      <div className={`w-9 h-9 rounded-lg bg-white border border-slate-200 flex items-center justify-center shrink-0`}>
        <Icon className={`w-4 h-4 ${color}`} />
      </div>
      <div>
        <p className="text-lg font-black text-slate-800 leading-none">{value.toLocaleString('ar-SA')}</p>
        <p className="text-xs text-slate-400 mt-0.5">{label}</p>
      </div>
    </div>
  )
}

// ── Progress strip ────────────────────────────────────────────────────────────

function SyncProgress() {
  const steps = ['المنتجات', 'الطلبات', 'الكوبونات', 'اللقطة النهائية']
  const [step, setStep] = useState(0)
  useEffect(() => {
    const id = setInterval(() => setStep(s => (s + 1) % steps.length), 900)
    return () => clearInterval(id)
  }, [])
  return (
    <div className="space-y-2.5">
      <div className="flex items-center gap-2 text-sm text-amber-700 font-medium">
        <Loader2 className="w-4 h-4 animate-spin text-amber-500" />
        جارٍ استيراد {steps[step]}…
      </div>
      <div className="flex gap-1.5">
        {steps.map((s, i) => (
          <div key={s} className={`flex-1 h-1.5 rounded-full transition-all duration-500 ${i <= step ? 'bg-amber-400' : 'bg-slate-200'}`} />
        ))}
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

interface StoreSyncPanelProps {
  isStoreConnected: boolean
}

// Max time in ms to wait for a sync to complete before giving up on the frontend
const SYNC_TIMEOUT_MS  = 3 * 60 * 1000   // 3 minutes
const POLL_INTERVAL_MS = 3_000            // check every 3 s
const MAX_POLL_ERRORS  = 4               // stop polling after 4 consecutive API errors

export default function StoreSyncPanel({ isStoreConnected }: StoreSyncPanelProps) {
  const [status, setStatus]     = useState<SyncStatus | null>(null)
  const [knowledge, setKnow]    = useState<KnowledgeOverview | null>(null)
  const [loading, setLoading]   = useState(true)
  const [syncing, setSyncing]   = useState(false)
  const [syncError, setSyncError] = useState<string | null>(null)
  const [pollTimer, setPollTimer] = useState<ReturnType<typeof setInterval> | null>(null)
  const [syncStartedAt, setSyncStartedAt] = useState<number | null>(null)
  const [pollErrors, setPollErrors]       = useState(0)

  const stopPolling = useCallback((timer: ReturnType<typeof setInterval> | null) => {
    if (timer) clearInterval(timer)
    setPollTimer(null)
  }, [])

  // ── Load ───────────────────────────────────────────────────────────────────

  const loadAll = useCallback(async (currentTimer?: ReturnType<typeof setInterval> | null) => {
    try {
      const [st, kn] = await Promise.all([
        storeSyncApi.getStatus(),
        storeSyncApi.getKnowledge(),
      ])
      setStatus(st)
      setKnow(kn)
      setPollErrors(0)   // reset error counter on success

      if (st.sync_running) {
        setSyncing(true)
        // Enforce a client-side timeout so the loader never runs forever
        if (syncStartedAt && Date.now() - syncStartedAt > SYNC_TIMEOUT_MS) {
          setSyncing(false)
          setSyncError('استغرقت المزامنة وقتاً طويلاً وتوقفت. يمكنك إعادة المحاولة.')
          stopPolling(currentTimer ?? pollTimer)
        }
      } else {
        // Sync finished (completed, failed, or timed_out on the backend)
        setSyncing(false)
        setSyncStartedAt(null)
        stopPolling(currentTimer ?? pollTimer)
        if (st.last_job_status === 'failed' || st.last_job_status === 'timed_out') {
          setSyncError(st.last_job_error ?? 'فشلت المزامنة — يمكنك إعادة المحاولة.')
        } else {
          setSyncError(null)
        }
      }
    } catch {
      const next = pollErrors + 1
      setPollErrors(next)
      if (next >= MAX_POLL_ERRORS) {
        setSyncing(false)
        setSyncStartedAt(null)
        stopPolling(currentTimer ?? pollTimer)
        setSyncError('تعذّر الوصول إلى الخادم. تحقق من الاتصال وأعد المحاولة.')
      }
    } finally {
      setLoading(false)
    }
  }, [pollTimer, pollErrors, syncStartedAt, stopPolling])

  useEffect(() => { loadAll() }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  // ── Trigger sync ───────────────────────────────────────────────────────────

  const handleSync = useCallback(async () => {
    if (syncing) return
    setSyncing(true)
    setSyncError(null)
    setSyncStartedAt(Date.now())
    setPollErrors(0)
    try {
      await storeSyncApi.trigger()
      // Poll for completion every POLL_INTERVAL_MS
      const timer = setInterval(() => loadAll(timer), POLL_INTERVAL_MS)
      setPollTimer(timer)
    } catch (err) {
      setSyncing(false)
      setSyncStartedAt(null)
      setSyncError(err instanceof Error ? err.message : 'فشل تشغيل المزامنة')
    }
  }, [syncing, loadAll])

  // ── Cleanup ────────────────────────────────────────────────────────────────

  useEffect(() => () => { if (pollTimer) clearInterval(pollTimer) }, [pollTimer])

  // ─────────────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-slate-200 p-5 flex items-center gap-2 text-slate-400 text-sm">
        <Loader2 className="w-4 h-4 animate-spin" /> جاري التحميل…
      </div>
    )
  }

  const ready    = knowledge?.ready ?? false
  const hasError = status?.last_job_status === 'failed' || status?.last_job_status === 'timed_out'

  return (
    <div className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
        <div className="flex items-center gap-2.5">
          <BookOpen className="w-4 h-4 text-brand-500" />
          <h2 className="font-semibold text-slate-900 text-sm">قاعدة معرفة نحلة</h2>
          {ready && (
            <span className="flex items-center gap-1 text-[10px] font-bold text-emerald-600 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">
              <CheckCircle2 className="w-2.5 h-2.5" />
              جاهزة
            </span>
          )}
        </div>

        <button
          onClick={handleSync}
          disabled={syncing || !isStoreConnected}
          title={!isStoreConnected ? 'ارتبط بالمتجر أولاً' : undefined}
          className="flex items-center gap-1.5 text-xs font-semibold px-3 py-1.5 rounded-lg border border-brand-300 text-brand-600 hover:bg-brand-50 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${syncing ? 'animate-spin' : ''}`} />
          {syncing ? 'جارٍ المزامنة' : 'مزامنة الآن'}
        </button>
      </div>

      <div className="p-5 space-y-4">
        {/* Store not connected */}
        {!isStoreConnected && (
          <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-4">
            <AlertCircle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-semibold text-amber-800">ارتبط بالمتجر أولاً</p>
              <p className="text-xs text-amber-700 mt-0.5">
                لمزامنة المنتجات والطلبات، قم بربط متجرك أعلاه ثم اضغط "مزامنة الآن".
              </p>
            </div>
          </div>
        )}

        {/* Sync progress */}
        {syncing && (
          <div className="bg-amber-50 border border-amber-200 rounded-xl p-4">
            <SyncProgress />
          </div>
        )}

        {/* Error state — from backend or frontend timeout */}
        {(syncError || (hasError && !syncing && status?.last_job_error)) && (
          <div className="flex items-start gap-2.5 bg-red-50 border border-red-200 rounded-xl p-3.5">
            <AlertCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
            <div>
              <p className="text-xs font-semibold text-red-700">فشلت المزامنة الأخيرة</p>
              <p className="text-xs text-red-600 mt-1 break-all">
                {syncError ?? status?.last_job_error}
              </p>
            </div>
          </div>
        )}

        {/* Not synced yet */}
        {!ready && !syncing && isStoreConnected && (
          <div className="text-center py-6">
            <PackageSearch className="w-10 h-10 text-slate-300 mx-auto mb-3" />
            <p className="text-sm text-slate-600 font-medium">لم تتم المزامنة بعد</p>
            <p className="text-xs text-slate-400 mt-1">
              اضغط "مزامنة الآن" لاستيراد منتجاتك وطلباتك وتجهيز نحلة لمتجرك.
            </p>
          </div>
        )}

        {/* Entity stats */}
        {ready && (
          <>
            <div className="grid grid-cols-2 gap-3">
              <StatTile icon={PackageSearch}  label="منتج"      value={status?.product_count  ?? 0} color="text-blue-500" />
              <StatTile icon={ShoppingCart}   label="طلب"       value={status?.order_count    ?? 0} color="text-purple-500" />
              <StatTile icon={Tag}            label="كوبون فعّال" value={status?.coupon_count  ?? 0} color="text-amber-500" />
              <StatTile icon={BarChart3}      label="تصنيف"     value={status?.category_count ?? 0} color="text-emerald-500" />
            </div>

            {/* Categories preview */}
            {knowledge?.categories && knowledge.categories.length > 0 && (
              <div className="flex flex-wrap gap-1.5">
                {knowledge.categories.slice(0, 8).map(cat => (
                  <span key={cat} className="text-[11px] bg-slate-100 text-slate-600 px-2.5 py-0.5 rounded-full">
                    {cat}
                  </span>
                ))}
              </div>
            )}
          </>
        )}

        {/* AI readiness badge */}
        {ready && (
          <div className="flex items-center gap-2.5 bg-gradient-to-r from-brand-50 to-emerald-50 border border-brand-200/50 rounded-xl p-3.5">
            <Zap className="w-4 h-4 text-brand-500 shrink-0" />
            <div>
              <p className="text-xs font-bold text-brand-700">نحلة جاهزة للإجابة عن منتجاتك</p>
              <p className="text-[11px] text-slate-500 mt-0.5">
                آخر مزامنة كاملة: {relativeTime(status?.last_full_sync_at ?? null)}
                {status?.sync_version ? ` · الإصدار ${status.sync_version}` : ''}
              </p>
            </div>
          </div>
        )}

        {/* Sync now reminder when data is stale */}
        {ready && status?.last_full_sync_at && (
          (() => {
            const ageH = (Date.now() - new Date(status.last_full_sync_at).getTime()) / 3_600_000
            return ageH > 12 ? (
              <p className="text-xs text-slate-400 flex items-center gap-1.5">
                <AlertCircle className="w-3.5 h-3.5 text-amber-400" />
                البيانات قديمة ({Math.round(ageH)} ساعة). يُنصح بالمزامنة.
              </p>
            ) : null
          })()
        )}
      </div>
    </div>
  )
}
