/**
 * SallaEntryScreen.tsx — /app/entry
 * ─────────────────────────────────
 * Mini-dashboard for "استخدام التطبيق" inside the Salla embedded iframe.
 *
 * Sections:
 *   1. Sticky header — Nahla logo + store name
 *   2. Welcome — greeting + description
 *   3. Status cards (2×2) — Salla / WhatsApp / Subscription / Nahla
 *   4. Onboarding steps — completed / current / locked states
 *   5. Metrics cards (2×2) — today's stats (graceful fallback to "--")
 *   6. Primary CTA  — فتح لوحة نحلة المتقدمة  (target="_top")
 *   7. Secondary CTA — ربط واتساب الآن         (target="_top")
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api/client'

// ── Immediate ready re-signal ─────────────────────────────────────────────────
// /app/entry is navigated to INSIDE the Salla iframe after auth. Re-signalling
// here ensures Salla's host frame doesn't time-out waiting for app.ready when
// React Router replaces the page via client-side navigation.
;(function immediateReady() {
  try { window.parent.postMessage({ type: 'app.ready' }, '*') } catch { /* cross-origin */ }
  try { window.parent.postMessage({ event: 'embedded::ready', payload: {}, source: 'embedded-app' }, '*') } catch { /* cross-origin */ }
})()

const NAHLA_DASHBOARD   = 'https://app.nahlah.ai'
const NAHLA_WA_SETTINGS = 'https://app.nahlah.ai/whatsapp-connect'

function getToken(): string {
  try { return localStorage.getItem('nahla_token') || '' } catch { return '' }
}

// ── Types ──────────────────────────────────────────────────────────────────────

interface EmbeddedStatus {
  whatsapp_connected: boolean
  auto_reply_enabled: boolean
  store_name:         string
}

interface Subscription {
  salla_plan_slug:  string | null
  salla_plan_name:  string | null
  billing_status:   string
  salla_valid_till: string | null
}

interface SyncStats {
  conversations_today: number
  orders_today:        number
  whatsapp_revenue_today: number
  ai_reply_rate:       number
}

type StepState = 'completed' | 'current' | 'locked'
interface MetricPresence {
  conversations: boolean
  orders:        boolean
  revenue:       boolean
  aiRate:        boolean
}

// ── Design tokens (inline styles for iframe compatibility) ─────────────────────

const C = {
  amber:       '#f59e0b',
  amberLight:  '#fff7ed',
  amberBorder: '#fed7aa',
  green:       '#22c55e',
  greenLight:  '#f0fdf4',
  greenText:   '#15803d',
  slate50:     '#f8fafc',
  slate100:    '#f1f5f9',
  slate200:    '#e2e8f0',
  slate300:    '#cbd5e1',
  slate400:    '#94a3b8',
  slate500:    '#64748b',
  slate900:    '#0f172a',
  white:       '#ffffff',
  red50:       '#fef2f2',
  redBorder:   '#fecaca',
  redText:     '#dc2626',
  bg:          '#f9fafb',
} as const

// ── Sub-components ─────────────────────────────────────────────────────────────

function MiniStatusCard({
  icon, label, active, activeText, inactiveText,
}: {
  icon: string; label: string; active: boolean; activeText: string; inactiveText: string
}) {
  return (
    <div
      style={{
        background:   C.white,
        border:       active ? `1.5px solid #bbf7d0` : `1.5px solid ${C.slate100}`,
        borderRadius: 16,
        padding:      '14px',
        boxShadow:    '0 1px 3px rgba(0,0,0,0.04)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
        <span style={{ fontSize: 18 }}>{icon}</span>
        <span
          style={{
            width:     8,
            height:    8,
            borderRadius: '50%',
            background: active ? C.green : C.slate200,
            boxShadow:  active ? `0 0 6px rgba(34,197,94,0.4)` : 'none',
            display:    'inline-block',
            flexShrink: 0,
          }}
        />
      </div>
      <p style={{ fontSize: 11, color: C.slate400, fontWeight: 600, margin: 0 }}>{label}</p>
      <p style={{ fontSize: 12, fontWeight: 700, color: active ? C.greenText : C.slate300, margin: '2px 0 0' }}>
        {active ? activeText : inactiveText}
      </p>
    </div>
  )
}

function OnboardingStep({
  num, title, description, state, isLast,
}: {
  num: number; title: string; description: string; state: StepState; isLast: boolean
}) {
  const isDone    = state === 'completed'
  const isCurrent = state === 'current'
  const isLocked  = state === 'locked'

  return (
    <div
      style={{
        display:       'flex',
        alignItems:    'flex-start',
        gap:           12,
        padding:       '12px 16px',
        borderBottom:  isLast ? 'none' : `1px solid ${C.slate50}`,
        opacity:       isLocked ? 0.45 : 1,
        transition:    'opacity 0.2s',
      }}
    >
      {/* Step bubble */}
      <div
        style={{
          width:          28,
          height:         28,
          borderRadius:   '50%',
          display:        'flex',
          alignItems:     'center',
          justifyContent: 'center',
          flexShrink:     0,
          fontSize:       12,
          fontWeight:     900,
          background:     isDone ? C.amber : isCurrent ? C.amberLight : C.slate50,
          border:         isDone
            ? `2px solid ${C.amber}`
            : isCurrent
            ? `2px solid ${C.amber}`
            : `2px solid ${C.slate200}`,
          color:          isDone ? C.slate900 : isCurrent ? C.amber : C.slate300,
        }}
      >
        {isDone ? '✓' : num}
      </div>

      {/* Text content */}
      <div style={{ flex: 1, paddingTop: 2 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <span
            style={{
              fontSize:   13,
              fontWeight: 700,
              color:      isDone ? C.slate400 : C.slate900,
            }}
          >
            {title}
          </span>

          {isCurrent && (
            <span
              style={{
                fontSize:     10,
                fontWeight:   900,
                padding:      '2px 8px',
                borderRadius: 99,
                background:   C.amberLight,
                color:        C.amber,
                border:       `1px solid ${C.amberBorder}`,
              }}
            >
              الخطوة التالية
            </span>
          )}

          {isDone && (
            <span
              style={{
                fontSize:     10,
                fontWeight:   900,
                padding:      '2px 8px',
                borderRadius: 99,
                background:   C.greenLight,
                color:        C.greenText,
              }}
            >
              مكتمل ✓
            </span>
          )}
        </div>
        <p style={{ fontSize: 11, color: C.slate400, margin: '3px 0 0', lineHeight: 1.5 }}>
          {description}
        </p>
      </div>
    </div>
  )
}

function MetricCard({
  icon, label, rawValue, hasData,
}: {
  icon: string; label: string; rawValue: string; hasData: boolean
}) {
  return (
    <div
      style={{
        background:   C.white,
        border:       `1.5px solid ${C.slate100}`,
        borderRadius: 16,
        padding:      '14px',
        boxShadow:    '0 1px 3px rgba(0,0,0,0.04)',
      }}
    >
      <span style={{ fontSize: 18 }}>{icon}</span>
      <p style={{ fontSize: 11, color: C.slate400, margin: '8px 0 2px', lineHeight: 1.4 }}>{label}</p>
      <p
        style={{
          fontSize:   20,
          fontWeight: 900,
          color:      hasData ? C.slate900 : C.slate300,
          margin:     0,
          direction:  'ltr',
          textAlign:  'right',
        }}
      >
        {hasData ? rawValue : '--'}
      </p>
    </div>
  )
}

function LoadingSkeleton() {
  const pulse: React.CSSProperties = {
    background: 'linear-gradient(90deg, #f1f5f9 25%, #e2e8f0 50%, #f1f5f9 75%)',
    backgroundSize: '200% 100%',
    animation: 'shimmer 1.4s infinite',
    borderRadius: 16,
  }
  return (
    <>
      <style>{`@keyframes shimmer { 0%{background-position:200% 0} 100%{background-position:-200% 0} }`}</style>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        {[1, 2, 3, 4].map(i => <div key={i} style={{ ...pulse, height: 84 }} />)}
      </div>
      <div style={{ ...pulse, height: 200 }} />
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
        {[1, 2, 3, 4].map(i => <div key={i} style={{ ...pulse, height: 84 }} />)}
      </div>
    </>
  )
}

// ── Main ───────────────────────────────────────────────────────────────────────

export default function SallaEntryScreen() {
  const navigate  = useNavigate()
  const bootedRef = useRef(false)
  const [logoErr, setLogoErr]   = useState(false)

  const [status,      setStatus]      = useState<EmbeddedStatus | null>(null)
  const [sub,         setSub]         = useState<Subscription | null>(null)
  const [appStoreUrl, setAppStoreUrl] = useState<string>('https://s.salla.sa/apps')
  const [metrics, setMetrics] = useState<SyncStats | null>(null)
  const [metricPresence, setMetricPresence] = useState<MetricPresence>({
    conversations: false,
    orders:        false,
    revenue:       false,
    aiRate:        false,
  })
  const [loading, setLoading] = useState(true)
  const [error,   setError]   = useState<string | null>(null)
  const [partialFallback, setPartialFallback] = useState(false)

  const storedName = (() => {
    try { return localStorage.getItem('nahla_salla_store_name') || '' } catch { return '' }
  })()

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    setPartialFallback(false)
    setMetricPresence({
      conversations: false,
      orders:        false,
      revenue:       false,
      aiRate:        false,
    })
    const token = getToken()
    if (!token) { navigate('/app/salla', { replace: true }); return }

    const headers = { Authorization: `Bearer ${token}` }
    const signal  = AbortSignal.timeout(9000)

    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const [sessionR, settingsR, subR, syncR] = await Promise.allSettled<any>([
        fetch(`${API_BASE}/api/salla/session`, { headers, signal }).then(async r => ({ ok: r.ok, data: r.ok ? await r.json() : null })),
        fetch(`${API_BASE}/salla/app-settings`, { headers, signal }).then(async r => ({ ok: r.ok, data: r.ok ? await r.json() : null })),
        fetch(`${API_BASE}/salla/subscription/status`, { headers, signal }).then(async r => ({ ok: r.ok, data: r.ok ? await r.json() : null })),
        fetch(`${API_BASE}/store-sync/status`, { headers, signal }).then(async r => ({ ok: r.ok, data: r.ok ? await r.json() : null })),
      ])

      const sessionOk  = sessionR.status  === 'fulfilled' && !!sessionR.value?.ok
      const settingsOk = settingsR.status === 'fulfilled' && !!settingsR.value?.ok
      const subOk      = subR.status      === 'fulfilled' && !!subR.value?.ok
      const syncOk     = syncR.status     === 'fulfilled' && !!syncR.value?.ok

      // Required APIs for accurate status in the mini dashboard.
      if (!sessionOk || !subOk) setPartialFallback(true)

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const wa:      any = sessionOk  ? (sessionR.value?.data ?? {}) : {}
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const cfg:     any = settingsOk ? (settingsR.value?.data?.settings ?? {}) : {}
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const subData: any = subOk      ? (subR.value?.data ?? {}) : {}
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const sync:    any = syncOk     ? (syncR.value?.data ?? null) : null

      localStorage.setItem('nahla_salla_wa_connected', wa.whatsapp_connected ? '1' : '0')

      setStatus({
        whatsapp_connected: !!wa.whatsapp_connected,
        auto_reply_enabled: cfg.nahla_enabled ?? true,
        store_name:         wa.store_name || storedName,
      })

      if (subData?.subscription) setSub(subData.subscription)
      if (subData?.app_store_url) setAppStoreUrl(subData.app_store_url)

      if (sync) {
        const pickNumber = (...vals: unknown[]): number | undefined => {
          for (const val of vals) {
            if (typeof val === 'number' && Number.isFinite(val)) return val
            if (typeof val === 'string' && val.trim() !== '' && Number.isFinite(Number(val))) return Number(val)
          }
          return undefined
        }
        const convValue    = pickNumber(sync.conversations_today)
        const ordersValue  = pickNumber(sync.orders_today, sync.ai_orders)
        const revenueValue = pickNumber(sync.whatsapp_revenue_today, sync.ai_revenue)
        const aiRateRaw    = pickNumber(sync.ai_reply_rate, sync.ai_rate)
        const aiRateValue  = aiRateRaw === undefined ? undefined : (aiRateRaw > 1 ? aiRateRaw / 100 : aiRateRaw)

        setMetricPresence({
          conversations: convValue !== undefined,
          orders:        ordersValue !== undefined,
          revenue:       revenueValue !== undefined,
          aiRate:        aiRateValue !== undefined,
        })

        setMetrics({
          conversations_today: convValue ?? 0,
          orders_today:        ordersValue ?? 0,
          whatsapp_revenue_today: revenueValue ?? 0,
          ai_reply_rate:       aiRateValue ?? 0,
        })
      }
    } catch {
      setError('تعذّر تحميل البيانات. تحقق من اتصالك وأعد المحاولة.')
    } finally {
      setLoading(false)
    }
  }, [navigate, storedName])

  useEffect(() => {
    // Re-signal Salla's host frame in case it missed the module-level signal.
    try { window.parent.postMessage({ type: 'app.ready' }, '*') } catch { /* cross-origin */ }

    if (bootedRef.current) return
    bootedRef.current = true
    localStorage.setItem('nahla_salla_embedded', '1')
    load()
  }, [load])

  // ── Derived state ────────────────────────────────────────────────────────────

  const waOk         = status?.whatsapp_connected ?? false
  const autoOk       = status?.auto_reply_enabled ?? false
  const nahlaOk      = waOk && autoOk
  const subStatus    = sub?.billing_status ?? 'none'
  const trialBlocked = subStatus === 'trial_blocked'
  const subActive    = subStatus === 'active' || subStatus === 'trial'
  const subLabel     = subStatus === 'active'        ? 'نشط'
                     : subStatus === 'trial'         ? 'تجريبي'
                     : subStatus === 'trial_blocked' ? 'تجربة مستخدمة'
                     : subStatus === 'cancelled'     ? 'ملغى'
                     : 'غير نشط'

  const m        = metrics
  const hasConv  = metricPresence.conversations
  const hasOrd   = metricPresence.orders
  const hasRev   = metricPresence.revenue
  const hasRate  = metricPresence.aiRate
  const noData   = !hasConv && !hasOrd && !hasRev && !hasRate

  const fmt    = (n: number) => n.toLocaleString('ar-SA')
  const fmtSAR = (n: number) => `${n.toLocaleString('ar-SA')} ر.س`
  const fmtPct = (n: number) => `${Math.round(n * 100)}%`

  // Onboarding steps: each entry is true when that step is done
  const stepDone = [waOk, waOk && autoOk, hasConv, hasConv && (hasOrd || hasRev)]
  const stepState = (i: number): StepState => {
    if (stepDone[i]) return 'completed'
    const first = stepDone.findIndex(d => !d)
    return first === i ? 'current' : 'locked'
  }

  // ── Render ───────────────────────────────────────────────────────────────────

  return (
    <div
      dir="rtl"
      style={{
        minHeight:   '100dvh',
        display:     'flex',
        flexDirection: 'column',
        fontFamily:  "'Cairo', system-ui, sans-serif",
        background:  C.bg,
        color:       C.slate900,
      }}
    >
      {/* ── Sticky header ─────────────────────────────────────────────────── */}
      <header
        style={{
          position:     'sticky',
          top:          0,
          zIndex:       10,
          background:   C.white,
          borderBottom: `1px solid ${C.slate100}`,
          boxShadow:    '0 1px 4px rgba(0,0,0,0.04)',
          padding:      '10px 16px',
          display:      'flex',
          alignItems:   'center',
          gap:          10,
        }}
      >
        {/* Logo */}
        {!logoErr ? (
          <img
            src="https://app.nahlah.ai/logo.png"
            alt="نحلة"
            style={{ width: 30, height: 30, objectFit: 'contain' }}
            onError={() => setLogoErr(true)}
          />
        ) : (
          <div
            style={{
              width:          30,
              height:         30,
              borderRadius:   '50%',
              background:     C.amberLight,
              border:         `1.5px solid ${C.amberBorder}`,
              display:        'flex',
              alignItems:     'center',
              justifyContent: 'center',
              fontSize:       16,
              flexShrink:     0,
            }}
          >
            🐝
          </div>
        )}

        {/* Brand + store */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <p style={{ fontSize: 14, fontWeight: 900, color: C.slate900, margin: 0 }}>نحلة AI</p>
          {status?.store_name && (
            <p style={{ fontSize: 10, color: C.slate400, margin: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {status.store_name}
            </p>
          )}
        </div>

        {/* Refresh */}
        <button
          type="button"
          onClick={load}
          disabled={loading}
          style={{
            fontSize:     11,
            color:        C.slate400,
            background:   'transparent',
            border:       'none',
            cursor:       loading ? 'default' : 'pointer',
            padding:      '4px 8px',
            borderRadius: 8,
            opacity:      loading ? 0.5 : 1,
          }}
        >
          {loading ? '...' : 'تحديث'}
        </button>
      </header>

      {/* ── Main content ──────────────────────────────────────────────────── */}
      <main
        style={{
          flex:      1,
          padding:   '20px 16px 32px',
          maxWidth:  520,
          margin:    '0 auto',
          width:     '100%',
          boxSizing: 'border-box',
          display:   'flex',
          flexDirection: 'column',
          gap:       20,
        }}
      >
        {/* ─ Welcome ─ */}
        <div style={{ textAlign: 'center', paddingTop: 4 }}>
          <h1 style={{ fontSize: 22, fontWeight: 900, color: C.slate900, margin: '0 0 6px' }}>
            مرحباً بك في نحلة 👋
          </h1>
          <p style={{ fontSize: 13, color: C.slate500, margin: 0, lineHeight: 1.6 }}>
            اربط واتساب وابدأ الرد الذكي لزيادة مبيعات متجرك
          </p>
        </div>

        {/* ─ Loading ─ */}
        {loading && <LoadingSkeleton />}

        {/* ─ Error ─ */}
        {error && !loading && (
          <div
            style={{
              display:      'flex',
              alignItems:   'center',
              gap:          12,
              background:   C.red50,
              border:       `1.5px solid ${C.redBorder}`,
              borderRadius: 16,
              padding:      '12px 16px',
            }}
          >
            <span style={{ fontSize: 18, flexShrink: 0 }}>⚠️</span>
            <p style={{ fontSize: 12, color: C.redText, flex: 1, margin: 0, lineHeight: 1.5 }}>{error}</p>
            <button
              onClick={load}
              style={{
                fontSize:   12,
                fontWeight: 700,
                color:      C.redText,
                background: 'transparent',
                border:     'none',
                cursor:     'pointer',
                flexShrink: 0,
                textDecoration: 'underline',
              }}
            >
              إعادة المحاولة
            </button>
          </div>
        )}

        {!loading && !error && status && (
          <>
            {/* ─ 1. Status cards (2×2) ─ */}
            <section>
              <p
                style={{
                  fontSize:      11,
                  fontWeight:    700,
                  color:         C.slate400,
                  textTransform: 'uppercase',
                  letterSpacing: '0.08em',
                  margin:        '0 0 10px',
                }}
              >
                الحالة
              </p>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <MiniStatusCard icon="🏪" label="سلة"       active={true}      activeText="متصل"        inactiveText="غير متصل" />
                <MiniStatusCard icon="💬" label="واتساب"    active={waOk}      activeText="متصل"        inactiveText="غير متصل" />
                <MiniStatusCard icon="💳" label="الاشتراك"  active={subActive} activeText={subLabel}    inactiveText="غير نشط"  />
                <MiniStatusCard icon="🤖" label="نحلة"      active={nahlaOk}   activeText="تعمل"        inactiveText="متوقفة"   />
              </div>
            </section>

            {/* ─ 2. Onboarding steps ─ */}
            <section
              style={{
                background:   C.white,
                border:       `1.5px solid ${C.slate100}`,
                borderRadius: 16,
                boxShadow:    '0 1px 3px rgba(0,0,0,0.04)',
                overflow:     'hidden',
              }}
            >
              <div
                style={{
                  padding:      '12px 16px',
                  borderBottom: `1px solid ${C.slate50}`,
                }}
              >
                <p
                  style={{
                    fontSize:      11,
                    fontWeight:    700,
                    color:         C.slate400,
                    textTransform: 'uppercase',
                    letterSpacing: '0.08em',
                    margin:        0,
                  }}
                >
                  خطوات البدء
                </p>
              </div>
              <OnboardingStep
                num={1}
                title="ربط واتساب"
                description="اربط حساب واتساب بزنس بمتجرك"
                state={stepState(0)}
                isLast={false}
              />
              <OnboardingStep
                num={2}
                title="تفعيل الرد الذكي"
                description="فعّل نحلة لترد على عملائك تلقائياً"
                state={stepState(1)}
                isLast={false}
              />
              <OnboardingStep
                num={3}
                title="تجربة أول محادثة"
                description="ابدأ محادثة واتساب مع عميل أول"
                state={stepState(2)}
                isLast={false}
              />
              <OnboardingStep
                num={4}
                title="متابعة النتائج"
                description="راقب الإحصائيات ومعدلات الرد الذكي"
                state={stepState(3)}
                isLast={true}
              />
            </section>

            {/* ─ 3. Metrics (2×2) ─ */}
            <section>
              <p
                style={{
                  fontSize:      11,
                  fontWeight:    700,
                  color:         C.slate400,
                  textTransform: 'uppercase',
                  letterSpacing: '0.08em',
                  margin:        '0 0 10px',
                }}
              >
                إحصائيات اليوم
              </p>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
                <MetricCard
                  icon="💬"
                  label="المحادثات اليوم"
                  rawValue={fmt(m?.conversations_today ?? 0)}
                  hasData={hasConv}
                />
                <MetricCard icon="🛍️" label="طلبات واتساب اليوم" rawValue={fmt(m?.orders_today ?? 0)} hasData={hasOrd} />
                <MetricCard
                  icon="💰"
                  label="إيرادات واتساب اليوم"
                  rawValue={fmtSAR(m?.whatsapp_revenue_today ?? 0)}
                  hasData={hasRev}
                />
                <MetricCard
                  icon="🤖"
                  label="معدل الرد بالذكاء"
                  rawValue={fmtPct(m?.ai_reply_rate ?? 0)}
                  hasData={hasRate}
                />
              </div>

              {partialFallback && (
                <p
                  style={{
                    fontSize:      12,
                    color:         C.slate500,
                    textAlign:     'center',
                    margin:        '10px 0 0',
                    border:        `1px dashed ${C.slate200}`,
                    borderRadius:  10,
                    padding:       '8px 10px',
                    background:    C.white,
                  }}
                >
                  تعذر تحميل بعض البيانات، اضغط تحديث.
                </p>
              )}

              {noData && (
                <p
                  style={{
                    fontSize:   12,
                    color:      C.slate400,
                    textAlign:  'center',
                    margin:     '12px 0 0',
                    lineHeight: 1.6,
                  }}
                >
                  ستظهر الإحصائيات بعد أول محادثات واتساب
                </p>
              )}
            </section>

            {/* ─ 4. CTAs ─ */}
            <section style={{ display: 'flex', flexDirection: 'column', gap: 10, paddingTop: 4 }}>
              {/* Primary */}
              <a
                href={NAHLA_DASHBOARD}
                target="_top"
                rel="noreferrer"
                style={{
                  display:        'flex',
                  alignItems:     'center',
                  justifyContent: 'center',
                  gap:            8,
                  padding:        '15px 20px',
                  borderRadius:   16,
                  fontSize:       15,
                  fontWeight:     900,
                  background:     C.amber,
                  color:          C.slate900,
                  textDecoration: 'none',
                  boxShadow:      '0 4px 20px rgba(245,158,11,0.28)',
                  border:         'none',
                }}
              >
                🚀 فتح لوحة نحلة المتقدمة
              </a>

              {/* Secondary */}
              <a
                href={NAHLA_WA_SETTINGS}
                target="_top"
                rel="noreferrer"
                style={{
                  display:        'flex',
                  alignItems:     'center',
                  justifyContent: 'center',
                  gap:            8,
                  padding:        '13px 20px',
                  borderRadius:   16,
                  fontSize:       14,
                  fontWeight:     700,
                  background:     C.white,
                  color:          C.amber,
                  textDecoration: 'none',
                  border:         `1.5px solid ${C.amber}`,
                }}
              >
                💬 ربط واتساب الآن
              </a>

              {/* Trial-blocked notice — soft banner below CTAs, not a wall */}
              {trialBlocked && (
                <div
                  style={{
                    background:    '#fff7ed',
                    border:        '1.5px solid #fed7aa',
                    borderRadius:  14,
                    padding:       '14px 16px',
                    display:       'flex',
                    flexDirection: 'column',
                    gap:           10,
                    marginTop:     4,
                  }}
                >
                  <p style={{ margin: 0, fontSize: 13, fontWeight: 700, color: '#9a3412' }}>
                    ⚠️ تم استخدام التجربة المجانية — الرد التلقائي متوقف
                  </p>
                  <p style={{ margin: 0, fontSize: 12, color: '#c2410c', lineHeight: 1.6 }}>
                    يمكنك الاطلاع على المحادثات الواردة، لكن نحلة لن ترد تلقائياً حتى تفعيل الاشتراك.
                  </p>
                  <a
                    href={appStoreUrl}
                    target="_top"
                    rel="noreferrer"
                    style={{
                      display:        'flex',
                      alignItems:     'center',
                      justifyContent: 'center',
                      gap:            6,
                      padding:        '11px 16px',
                      borderRadius:   10,
                      fontSize:       13,
                      fontWeight:     800,
                      background:     '#f97316',
                      color:          '#fff',
                      textDecoration: 'none',
                      border:         'none',
                    }}
                  >
                    💳 اشترك الآن من سلة
                  </a>
                </div>
              )}
            </section>
          </>
        )}
      </main>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <footer style={{ textAlign: 'center', padding: '12px 16px', borderTop: `1px solid ${C.slate100}` }}>
        <p style={{ fontSize: 10, color: C.slate300, margin: 0 }}>
          بأيدي سعودية 🇸🇦 · Nahla AI
        </p>
      </footer>
    </div>
  )
}
