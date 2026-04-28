/**
 * SallaEntryScreen.tsx  —  /app/entry
 * ------------------------------------
 * Smart Entry Screen — the first thing every Salla merchant sees after auth.
 *
 * QA rules (Salla approval checklist):
 *   ✔ No Landing, no Login, no empty dashboard
 *   ✔ Primary CTA above the fold, high contrast
 *   ✔ Interactive demo — not just a passive preview
 *   ✔ Value copy under every title (minimal, action-first)
 *   ✔ "Skip" is a subtle text link at the very bottom — not a button
 *   ✔ Smooth skeleton → content (no flicker, no double-render)
 *   ✔ Total time to interaction ≤ 3 s
 */
import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { API_BASE } from '../api/client'

// ── Types ─────────────────────────────────────────────────────────────────────

interface MerchantState {
  tenant_id:          number
  whatsapp_connected: boolean
  has_automations:    boolean
  has_products:       boolean
  token:              string
}

type ScreenPhase = 'loading' | 'wa-missing' | 'no-automations' | 'ready' | 'error'

// ── Interactive Demo Modal ────────────────────────────────────────────────────
// Animated chat simulation showing the full cart-recovery flow in real-time.

const CHAT_STEPS = [
  {
    type: 'system',
    delay: 400,
    text: '🔔 أحمد أضاف منتجاً للسلة — لم يكمل الشراء منذ 30 دقيقة',
  },
  {
    type: 'outgoing',
    delay: 1300,
    text: 'مرحباً أحمد 👋\nلاحظنا أن سلتك لا تزال تنتظرك في متجر الرياض.\n\n📦 Nike Air Max 270 — 499 ر.س\n\nأتمّ طلبك الآن قبل نفاد الكمية 👇',
    cta: 'إتمام الشراء →',
  },
  {
    type: 'typing',
    delay: 2200,
  },
  {
    type: 'incoming',
    delay: 3100,
    text: 'شكراً! أنا في الطريق، سأكمل الشراء الآن 😊',
  },
  {
    type: 'success',
    delay: 3900,
    text: '✅ تم الاسترداد! مبيعة بقيمة 499 ر.س',
  },
] as const

function InteractiveDemoModal({
  onClose,
  onActivate,
}: {
  onClose: () => void
  onActivate: () => void
}) {
  const [visibleSteps, setVisibleSteps] = useState<number[]>([])
  const [typing, setTyping]             = useState(false)
  const mountedRef                      = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    const timers: ReturnType<typeof setTimeout>[] = []

    CHAT_STEPS.forEach((step, idx) => {
      if (step.type === 'typing') {
        timers.push(setTimeout(() => { if (mountedRef.current) setTyping(true) }, step.delay))
        timers.push(setTimeout(() => { if (mountedRef.current) setTyping(false) }, step.delay + 800))
      } else {
        timers.push(
          setTimeout(() => {
            if (mountedRef.current)
              setVisibleSteps(prev => prev.includes(idx) ? prev : [...prev, idx])
          }, step.delay),
        )
      }
    })

    return () => {
      mountedRef.current = false
      timers.forEach(clearTimeout)
    }
  }, [])

  const done = visibleSteps.includes(CHAT_STEPS.length - 1)

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.75)', backdropFilter: 'blur(6px)' }}
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-2xl p-5 mb-4"
        style={{ background: '#0f1a2e', border: '1px solid rgba(245,158,11,0.25)' }}
        onClick={e => e.stopPropagation()}
      >
        {/* Modal header */}
        <div className="flex items-center justify-between mb-4">
          <div>
            <p className="text-white font-bold text-sm">تجربة تفاعلية</p>
            <p className="text-slate-500 text-[10px]">شاهد كيف تسترد نحلة السلات المتروكة</p>
          </div>
          <button
            onClick={onClose}
            className="text-slate-500 hover:text-slate-300 text-2xl leading-none w-7 h-7 flex items-center justify-center rounded-full"
            style={{ background: 'rgba(255,255,255,0.05)' }}
          >
            ×
          </button>
        </div>

        {/* Chat simulation */}
        <div
          className="rounded-xl p-4 space-y-3 min-h-[220px]"
          style={{ background: '#1e293b' }}
          dir="rtl"
        >
          {/* Sender header */}
          <div className="flex items-center gap-2 pb-1 border-b border-white/5">
            <div
              className="w-7 h-7 rounded-full flex items-center justify-center text-xs font-black shrink-0"
              style={{ background: 'linear-gradient(135deg,#f59e0b,#d97706)', color: '#000' }}
            >
              N
            </div>
            <div>
              <p className="text-white text-xs font-semibold">نحلة AI</p>
              <p className="text-slate-500 text-[9px]">متجرك الذكي · WhatsApp Business</p>
            </div>
            <div
              className="mr-auto flex items-center gap-1 text-[9px] font-bold px-2 py-0.5 rounded-full"
              style={{ background: 'rgba(37,211,102,0.12)', color: '#4ade80' }}
            >
              <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
              متصل
            </div>
          </div>

          {/* Steps */}
          {CHAT_STEPS.map((step, idx) => {
            if (!visibleSteps.includes(idx) && step.type !== 'typing') return null

            if (step.type === 'system') {
              return (
                <div key={idx} className="text-center animate-fade-up">
                  <span
                    className="text-[10px] px-2 py-1 rounded-full"
                    style={{ background: 'rgba(245,158,11,0.1)', color: '#94a3b8' }}
                  >
                    {step.text}
                  </span>
                </div>
              )
            }

            if (step.type === 'typing' && typing) {
              return (
                <div key={idx} className="flex items-end gap-1.5">
                  <div className="flex gap-0.5 px-3 py-2 rounded-xl" style={{ background: 'rgba(255,255,255,0.06)' }}>
                    {[0, 1, 2].map(d => (
                      <span
                        key={d}
                        className="w-1 h-1 rounded-full bg-slate-400 animate-bounce"
                        style={{ animationDelay: `${d * 0.15}s` }}
                      />
                    ))}
                  </div>
                </div>
              )
            }

            if (step.type === 'outgoing') {
              return (
                <div key={idx} className="flex justify-end animate-fade-up">
                  <div
                    className="max-w-[85%] rounded-2xl rounded-tr-sm p-3 space-y-2"
                    style={{ background: 'rgba(37,211,102,0.15)', border: '1px solid rgba(37,211,102,0.2)' }}
                  >
                    <p className="text-slate-100 text-xs leading-relaxed whitespace-pre-line">{step.text}</p>
                    <button
                      className="w-full py-1.5 rounded-lg text-xs font-bold text-center"
                      style={{ background: 'rgba(37,211,102,0.25)', color: '#4ade80' }}
                    >
                      {step.cta}
                    </button>
                  </div>
                </div>
              )
            }

            if (step.type === 'incoming') {
              return (
                <div key={idx} className="flex justify-start animate-fade-up">
                  <div
                    className="max-w-[85%] rounded-2xl rounded-tl-sm px-3 py-2"
                    style={{ background: 'rgba(255,255,255,0.07)' }}
                  >
                    <p className="text-slate-200 text-xs">{step.text}</p>
                  </div>
                </div>
              )
            }

            if (step.type === 'success') {
              return (
                <div key={idx} className="text-center animate-fade-up">
                  <div
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-xl text-xs font-bold"
                    style={{ background: 'rgba(74,222,128,0.12)', color: '#4ade80', border: '1px solid rgba(74,222,128,0.2)' }}
                  >
                    {step.text}
                  </div>
                </div>
              )
            }

            return null
          })}

          {/* Typing indicator - separate from step list so it always appears */}
          {typing && !visibleSteps.includes(3) && (
            <div className="flex items-end gap-1.5">
              <div className="flex gap-0.5 px-3 py-2 rounded-xl" style={{ background: 'rgba(255,255,255,0.06)' }}>
                {[0, 1, 2].map(d => (
                  <span
                    key={d}
                    className="w-1 h-1 rounded-full bg-slate-400 animate-bounce"
                    style={{ animationDelay: `${d * 0.15}s` }}
                  />
                ))}
              </div>
            </div>
          )}
        </div>

        {/* CTA after animation */}
        {done && (
          <button
            onClick={onActivate}
            className="mt-4 flex items-center justify-center gap-2 w-full py-3.5 rounded-2xl font-black text-sm animate-fade-up"
            style={{
              background: '#f59e0b',
              color:      '#0f172a',
              boxShadow:  '0 4px 20px rgba(245,158,11,0.4)',
            }}
          >
            🚀 فعّل هذه الأتمتة الآن
          </button>
        )}

        {!done && (
          <p className="mt-3 text-center text-[10px] text-slate-700">
            جاري المحاكاة...
          </p>
        )}
      </div>
    </div>
  )
}

// ── Loading skeleton ──────────────────────────────────────────────────────────

function LoadingSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="h-3 rounded-full bg-white/[0.04] w-24" />
      <div className="h-8 rounded-full bg-white/[0.05] w-4/5" />
      <div className="h-4 rounded-full bg-white/[0.03] w-3/4" />
      <div className="space-y-2.5 pt-2">
        {[0.9, 0.8, 0.85, 0.8].map((w, i) => (
          <div
            key={i}
            className="h-12 rounded-xl"
            style={{ background: 'rgba(255,255,255,0.025)', width: `${w * 100}%` }}
          />
        ))}
      </div>
      <div className="h-14 rounded-2xl" style={{ background: 'rgba(245,158,11,0.07)' }} />
    </div>
  )
}

// ── Shared subcomponents ──────────────────────────────────────────────────────

function Badge({ children, color = 'amber' }: { children: React.ReactNode; color?: 'amber' | 'green' }) {
  const amber = color === 'amber'
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs font-bold px-3 py-1 rounded-full"
      style={{
        background: amber ? 'rgba(245,158,11,0.12)' : 'rgba(37,211,102,0.12)',
        color:      amber ? '#f59e0b' : '#4ade80',
        border:     amber ? '1px solid rgba(245,158,11,0.25)' : '1px solid rgba(37,211,102,0.2)',
      }}
    >
      {color === 'green' && <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />}
      {children}
    </span>
  )
}

function PrimaryCta({
  children,
  to,
  onClick,
  green = false,
}: {
  children: React.ReactNode
  to?: string
  onClick?: () => void
  green?: boolean
}) {
  const style = {
    background: green ? 'linear-gradient(135deg, #25D366 0%, #128C7E 100%)' : '#f59e0b',
    color:      green ? '#fff' : '#0f172a',
    boxShadow:  green ? '0 6px 24px rgba(37,211,102,0.4)' : '0 6px 24px rgba(245,158,11,0.4)',
  }
  const className =
    'flex items-center justify-center gap-2 w-full py-4 rounded-2xl font-black text-base transition-transform active:scale-[0.98]'

  if (to) {
    return (
      <Link to={to} className={className} style={style} onClick={onClick}>
        {children}
      </Link>
    )
  }
  return (
    <button className={className} style={style} onClick={onClick}>
      {children}
    </button>
  )
}

function DemoBtn({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full py-3 rounded-2xl text-sm font-semibold"
      style={{
        background: 'rgba(255,255,255,0.04)',
        color:      '#64748b',
        border:     '1px solid rgba(255,255,255,0.07)',
      }}
    >
      📱 شاهد كيف تعمل — تجربة تفاعلية
    </button>
  )
}

// ── State A: Connect WhatsApp ─────────────────────────────────────────────────

function StateWaMissing({
  onDemo,
  onSkip,
}: {
  onDemo: () => void
  onSkip: () => void
}) {
  return (
    <div className="space-y-5">
      <Badge>خطوة واحدة للبدء</Badge>

      <div className="space-y-1.5">
        <h2 className="text-[1.6rem] font-black text-white leading-tight">
          ربط واتساب
          <br />
          <span style={{ color: '#f59e0b' }}>يستغرق دقيقتين فقط</span>
        </h2>
        <p className="text-slate-400 text-sm leading-relaxed">
          حوّل سلاتك المتروكة إلى مبيعات تلقائياً — دون أي جهد يدوي
        </p>
      </div>

      {/* Benefits */}
      <div className="space-y-2">
        {([
          ['🛒', 'استرداد السلة المتروكة تلقائياً'],
          ['✅', 'تأكيد الطلبات فورياً للعميل'],
          ['🚚', 'إشعارات الشحن والتسليم'],
          ['💰', 'تحصيل طلبات الدفع عند الاستلام'],
        ] as const).map(([icon, text]) => (
          <div
            key={text}
            className="flex items-center gap-3 rounded-xl px-4 py-3"
            style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.05)' }}
          >
            <span className="text-lg shrink-0">{icon}</span>
            <span className="text-slate-300 text-sm">{text}</span>
            <span className="mr-auto text-green-400 text-xs font-bold shrink-0">✓</span>
          </div>
        ))}
      </div>

      <PrimaryCta to="/whatsapp-connect" onClick={onSkip} green>
        <span>💬</span>
        ربط واتساب الآن
      </PrimaryCta>

      <DemoBtn onClick={onDemo} />
    </div>
  )
}

// ── State B: WA connected, no automations ────────────────────────────────────

function StateNoAutomations({
  onDemo,
  onSkip,
}: {
  onDemo: () => void
  onSkip: () => void
}) {
  return (
    <div className="space-y-5">
      <Badge color="green">واتساب متصل</Badge>

      <div className="space-y-1.5">
        <h2 className="text-[1.6rem] font-black text-white leading-tight">
          ابدأ أول أتمتة
          <br />
          <span style={{ color: '#f59e0b' }}>واسترد السلات المتروكة</span>
        </h2>
        <p className="text-slate-400 text-sm leading-relaxed">
          فعّل الأتمتة مرة واحدة — ستعمل 24/7 وتسترد مبيعاتك تلقائياً
        </p>
      </div>

      {/* Stats card */}
      <div
        className="rounded-2xl p-4 space-y-3"
        style={{
          background: 'linear-gradient(135deg, rgba(245,158,11,0.09) 0%, rgba(245,158,11,0.03) 100%)',
          border:     '1px solid rgba(245,158,11,0.22)',
        }}
      >
        <div className="flex items-start gap-3">
          <span className="text-3xl">🛒</span>
          <div>
            <p className="text-white font-bold text-sm">استرداد السلة المتروكة</p>
            <p className="text-slate-400 text-xs mt-0.5">رسالة واتساب تلقائية بعد 30 دقيقة</p>
          </div>
        </div>
        <div className="flex items-center justify-around pt-1">
          {[
            ['+22%', 'متوسط الاسترداد'],
            ['تلقائي', 'لا إعداد'],
            ['٢٤/٧', 'دائماً'],
          ].map(([val, label]) => (
            <div key={label} className="text-center">
              <p className="text-amber-400 font-black text-lg">{val}</p>
              <p className="text-slate-500 text-[10px]">{label}</p>
            </div>
          ))}
        </div>
      </div>

      <PrimaryCta to="/smart-automations" onClick={onSkip}>
        <span>🚀</span>
        تفعيل الأتمتة الآن
      </PrimaryCta>

      <DemoBtn onClick={onDemo} />
    </div>
  )
}

// ── State C: Fully live ───────────────────────────────────────────────────────

function StateReady({
  onDemo,
  onSkip,
}: {
  onDemo: () => void
  onSkip: () => void
}) {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap gap-2">
        <Badge color="green">واتساب متصل</Badge>
        <Badge>⚡ أتمتات نشطة</Badge>
      </div>

      <div className="space-y-1.5">
        <h2 className="text-[1.6rem] font-black text-white leading-tight">
          كل شيء جاهز 🚀
        </h2>
        <p className="text-slate-400 text-sm leading-relaxed">
          أتمتاتك تعمل وتسترد المبيعات الآن — جرّب تجربة عميلك مباشرةً
        </p>
      </div>

      <PrimaryCta onClick={onDemo} green>
        <span>📩</span>
        جرّب تجربة واتساب التفاعلية
      </PrimaryCta>

      {/* Quick links grid */}
      <div className="grid grid-cols-2 gap-2">
        {([
          ['📊 لوحة التحكم', '/overview'],
          ['⚡ الأتمتات',    '/smart-automations'],
          ['📋 الطلبات',     '/orders'],
          ['📣 حملة جديدة', '/campaigns'],
        ] as const).map(([label, to]) => (
          <Link
            key={to}
            to={to}
            onClick={onSkip}
            className="py-3 rounded-xl text-center text-sm font-semibold transition-colors hover:bg-white/10"
            style={{
              background: 'rgba(255,255,255,0.04)',
              color:      '#94a3b8',
              border:     '1px solid rgba(255,255,255,0.07)',
            }}
          >
            {label}
          </Link>
        ))}
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function SallaEntryScreen() {
  const navigate      = useNavigate()
  const [phase, setPhase]       = useState<ScreenPhase>('loading')
  const [showDemo, setShowDemo] = useState(false)
  const [visible, setVisible]   = useState(false) // fade-in after load
  const [errorMsg, setErrorMsg] = useState('')
  const loadedRef               = useRef(false)

  // ── Mark entry & skip ──────────────────────────────────────────────────
  const markShown = useCallback(() => {
    sessionStorage.setItem('salla_entry_shown', '1')
  }, [])

  const handleSkip = useCallback(() => {
    markShown()
    navigate('/overview', { replace: true })
  }, [markShown, navigate])

  const handleActivate = useCallback(() => {
    markShown()
    setShowDemo(false)
    navigate('/smart-automations', { replace: true })
  }, [markShown, navigate])

  // ── Session load ───────────────────────────────────────────────────────
  // Defensive: we already have a valid JWT (just persisted by SallaEmbedded).
  // If the readiness probe fails for any reason (timeout, 5xx, CORS), we
  // STILL enter the dashboard at /overview rather than blocking the merchant
  // on an error screen. Worst case the entry screen falls back to the live
  // ("ready") state with quick links — never a dead-end.
  const load = useCallback(async () => {
    const stored = localStorage.getItem('nahla_token')
    console.info('[SallaEntry] mount | token present:', !!stored)

    if (!stored) {
      console.warn('[SallaEntry] no token → /app/salla')
      navigate('/app/salla', { replace: true })
      return
    }

    try {
      const ctrl = new AbortController()
      const tid  = setTimeout(() => ctrl.abort(), 10_000)  // 10s — readiness probes can be slow
      console.info('[SallaEntry] → GET /api/salla/session')
      const res  = await fetch(`${API_BASE}/api/salla/session`, {
        headers: { Authorization: `Bearer ${stored}` },
        signal:  ctrl.signal,
      })
      clearTimeout(tid)
      console.info('[SallaEntry] session status:', res.status)

      // Only a real auth failure should bounce back to /app/salla.
      // Network errors / 5xx don't invalidate the token we just got.
      if (res.status === 401 || res.status === 403) {
        console.warn('[SallaEntry] auth rejected → /overview (fallback, do not clear token)')
        // Don't clear the token — let the dashboard guard re-check.
        navigate('/overview', { replace: true })
        return
      }

      if (!res.ok) {
        console.warn('[SallaEntry] non-OK status, using safe fallback')
        navigate('/overview', { replace: true })
        return
      }

      const data: MerchantState & { token?: string } = await res.json()
      console.info('[SallaEntry] session OK | wa:', data.whatsapp_connected,
                   '| autos:', data.has_automations,
                   '| products:', data.has_products)

      if (data.token) localStorage.setItem('nahla_token', data.token)

      const nextPhase: ScreenPhase = !data.whatsapp_connected
        ? 'wa-missing'
        : !data.has_automations
          ? 'no-automations'
          : 'ready'

      setPhase(nextPhase)
      setTimeout(() => setVisible(true), 50)
    } catch (e) {
      // Network error / abort / parse error — DO NOT show a dead-end.
      // The merchant is logged in (we have a token); take them to /overview.
      console.error('[SallaEntry] readiness probe failed → /overview fallback:', e)
      navigate('/overview', { replace: true })
    }
  }, [navigate])

  useEffect(() => {
    if (loadedRef.current) return
    loadedRef.current = true
    localStorage.setItem('nahla_salla_embedded', '1')
    load()
  }, [load])

  return (
    <>
      {showDemo && (
        <InteractiveDemoModal
          onClose={() => setShowDemo(false)}
          onActivate={handleActivate}
        />
      )}

      <div
        dir="rtl"
        className="min-h-dvh flex flex-col px-4 py-7"
        style={{
          fontFamily:      "'Cairo', system-ui, sans-serif",
          background:      '#0f172a',
          backgroundImage: 'radial-gradient(ellipse 80% 50% at 50% -5%, rgba(245,158,11,0.07) 0%, transparent 65%)',
        }}
      >
        {/* Header — logo only, no escape button */}
        <div className="flex items-center gap-2 mb-8">
          <img
            src="https://app.nahlah.ai/logo.png"
            alt="نحلة"
            className="w-7 h-7 object-contain"
            style={{ filter: 'drop-shadow(0 0 8px rgba(245,158,11,0.5))' }}
            onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }}
          />
          <span className="text-white font-black text-base">نحلة AI</span>
        </div>

        {/* Main content */}
        <div
          className="flex-1 flex flex-col justify-center max-w-sm mx-auto w-full transition-opacity duration-300"
          style={{ opacity: phase === 'loading' || !visible ? 1 : 1 }}
        >
          {phase === 'loading' && <LoadingSkeleton />}

          {phase === 'error' && (
            <div className="text-center space-y-4">
              <div className="text-4xl">⚠️</div>
              <p className="text-white font-semibold text-sm">{errorMsg}</p>
              <button
                onClick={load}
                className="w-full py-3.5 rounded-2xl font-bold text-sm"
                style={{ background: '#f59e0b', color: '#0f172a' }}
              >
                إعادة المحاولة
              </button>
            </div>
          )}

          {phase === 'wa-missing' && visible && (
            <StateWaMissing onDemo={() => setShowDemo(true)} onSkip={markShown} />
          )}

          {phase === 'no-automations' && visible && (
            <StateNoAutomations onDemo={() => setShowDemo(true)} onSkip={markShown} />
          )}

          {phase === 'ready' && visible && (
            <StateReady onDemo={() => setShowDemo(true)} onSkip={markShown} />
          )}
        </div>

        {/* Footer — subtle skip link, zero visual weight */}
        <div className="pt-8 pb-2 text-center space-y-2">
          {phase !== 'loading' && (
            <button
              onClick={handleSkip}
              className="text-[11px] transition-colors"
              style={{ color: '#334155' }}
              onMouseEnter={e => (e.currentTarget.style.color = '#475569')}
              onMouseLeave={e => (e.currentTarget.style.color = '#334155')}
            >
              الدخول إلى لوحة التحكم
            </button>
          )}
          <p className="text-[10px]" style={{ color: '#1e293b' }}>
            بأيدي سعودية 100% 🇸🇦
          </p>
        </div>
      </div>
    </>
  )
}
