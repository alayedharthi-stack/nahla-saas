/**
 * SallaSetup.tsx — /app/salla/setup
 * ─────────────────────────────────
 * Quick Setup — shown whenever the merchant has NOT fully completed setup.
 *
 * Guard logic (skip to /app/entry only if ALL true):
 *   ✓ nahla_salla_setup_done = '1'
 *   ✓ nahla_salla_wa_connected = '1'   (refreshed live from API)
 *
 * This means a returning merchant who never linked WhatsApp still sees
 * the setup screen — not the entry screen.
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_BASE } from '../api/client'

// ── Constants ─────────────────────────────────────────────────────────────────

const SETUP_DONE_KEY  = 'nahla_salla_setup_done'
const ADVANCED_URL    = 'https://app.nahlah.ai/settings'

const TONES = [
  { value: 'ودي',     label: '😊 ودي',     desc: 'مناسب لمتاجر الأزياء والتجزئة العامة' },
  { value: 'رسمي',    label: '👔 رسمي',    desc: 'مناسب للخدمات والمنتجات المتخصصة'  },
  { value: 'تسويقي',  label: '🔥 تسويقي', desc: 'مناسب للعروض والحملات الترويجية'   },
]

// ── API helpers ───────────────────────────────────────────────────────────────

function getToken(): string {
  try { return localStorage.getItem('nahla_token') || '' } catch { return '' }
}

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...opts,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
      ...opts?.headers,
    },
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error((err as any)?.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface SetupState {
  nahla_enabled:          boolean
  whatsapp_number:        string
  reply_tone:             string
  abandoned_cart_enabled: boolean
  discount_percentage:    number
  autopilot_enabled:      boolean
}

const DEFAULTS: SetupState = {
  nahla_enabled:          true,
  whatsapp_number:        '',
  reply_tone:             'ودي',
  abandoned_cart_enabled: true,
  discount_percentage:    10,
  autopilot_enabled:      true,
}

// ── Toggle component ──────────────────────────────────────────────────────────

function Toggle({
  checked,
  onChange,
  id,
}: {
  checked: boolean
  onChange: (v: boolean) => void
  id: string
}) {
  return (
    <button
      type="button"
      id={id}
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="relative inline-flex h-6 w-11 shrink-0 rounded-full transition-colors duration-200 focus:outline-none"
      style={{
        background: checked
          ? 'linear-gradient(135deg,#f59e0b,#d97706)'
          : 'rgba(255,255,255,0.1)',
      }}
    >
      <span
        className="pointer-events-none inline-block h-5 w-5 rounded-full bg-white shadow-sm transition-transform duration-200 mt-0.5"
        style={{ transform: checked ? 'translateX(-1.25rem)' : 'translateX(-0.125rem)' }}
      />
    </button>
  )
}

// ── Field wrapper ─────────────────────────────────────────────────────────────

function Field({
  label,
  desc,
  children,
  htmlFor,
}: {
  label: string
  desc?: string
  children: React.ReactNode
  htmlFor?: string
}) {
  return (
    <div
      className="rounded-xl p-4"
      style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex-1 min-w-0">
          <label htmlFor={htmlFor} className="text-slate-200 text-sm font-semibold block">
            {label}
          </label>
          {desc && <p className="text-slate-500 text-xs mt-0.5 leading-relaxed">{desc}</p>}
        </div>
        <div className="shrink-0">{children}</div>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function SallaSetup() {
  const navigate = useNavigate()
  const bootedRef = useRef(false)

  const [form,         setForm]         = useState<SetupState>(DEFAULTS)
  const [loading,      setLoading]      = useState(true)
  const [saving,       setSaving]       = useState(false)
  const [saveError,    setSaveError]    = useState<string | null>(null)
  const [waConnected,  setWaConnected]  = useState<boolean | null>(null)
  const [showToneMenu, setShowToneMenu] = useState(false)

  // ── Guard: skip if already done ───────────────────────────────────────────

  useEffect(() => {
    if (bootedRef.current) return
    bootedRef.current = true
    loadSettings()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const loadSettings = useCallback(async () => {
    setLoading(true)
    try {
      // Load current quick-setup values and WA status in parallel
      const [setupRes, sessionRes] = await Promise.allSettled([
        apiFetch<{ settings: SetupState }>('/salla/app-settings'),
        apiFetch<{ whatsapp_connected: boolean }>('/api/salla/session'),
      ])

      if (setupRes.status === 'fulfilled') {
        const s = setupRes.value.settings
        setForm(prev => ({
          ...prev,
          nahla_enabled:          s.nahla_enabled          ?? prev.nahla_enabled,
          whatsapp_number:        s.whatsapp_number         || prev.whatsapp_number,
          reply_tone:             s.reply_tone              || prev.reply_tone,
          abandoned_cart_enabled: s.abandoned_cart_enabled  ?? prev.abandoned_cart_enabled,
          discount_percentage:    s.discount_percentage      ?? prev.discount_percentage,
          autopilot_enabled:      s.autopilot_enabled        ?? prev.autopilot_enabled,
        }))
      }

      const waOk = sessionRes.status === 'fulfilled'
        ? sessionRes.value.whatsapp_connected
        : false
      setWaConnected(waOk)

      // Guard: if setup is done AND wa is now connected → skip to /app/entry
      const setupDone = localStorage.getItem(SETUP_DONE_KEY) === '1'
      if (setupDone && waOk) {
        navigate('/app/entry', { replace: true })
        return
      }
      // Update cached wa status so enterDashboard uses fresh value next time
      localStorage.setItem('nahla_salla_wa_connected', waOk ? '1' : '0')
    } catch {
      // Silent — defaults are usable
    } finally {
      setLoading(false)
    }
  }, [navigate])

  // ── Save ──────────────────────────────────────────────────────────────────

  const handleSave = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      await apiFetch('/salla/app-settings', {
        method: 'PUT',
        body:   JSON.stringify(form),
      })

      // Mark setup as done
      localStorage.setItem(SETUP_DONE_KEY, '1')
      localStorage.removeItem('nahla_salla_is_new')

      navigate('/app/entry', { replace: true })
    } catch (err: any) {
      setSaveError(err?.message || 'تعذّر الحفظ. تحقق من اتصالك وأعد المحاولة.')
      setSaving(false)
    }
  }

  const handleSkip = () => {
    localStorage.setItem(SETUP_DONE_KEY, '1')
    localStorage.removeItem('nahla_salla_is_new')
    navigate('/app/entry', { replace: true })
  }

  const update = <K extends keyof SetupState>(key: K, value: SetupState[K]) =>
    setForm(prev => ({ ...prev, [key]: value }))

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div
      dir="rtl"
      className="min-h-dvh flex flex-col px-4 py-7"
      style={{
        fontFamily:      "'Cairo', system-ui, sans-serif",
        background:      '#0f172a',
        backgroundImage: 'radial-gradient(ellipse 80% 50% at 50% -5%, rgba(245,158,11,0.07) 0%, transparent 65%)',
      }}
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex items-center gap-2 mb-6">
        <img
          src="https://app.nahlah.ai/logo.png"
          alt="نحلة"
          className="w-7 h-7 object-contain"
          style={{ filter: 'drop-shadow(0 0 8px rgba(245,158,11,0.5))' }}
          onError={e => { (e.target as HTMLImageElement).style.display = 'none' }}
        />
        <span className="text-white font-black text-base">نحلة AI</span>
      </div>

      {/* ── Content ─────────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col max-w-sm mx-auto w-full">

        {/* Title */}
        <div className="mb-5">
          <span
            className="inline-flex items-center gap-1.5 text-xs font-bold px-3 py-1 rounded-full mb-3"
            style={{
              background: 'rgba(245,158,11,0.12)',
              color:      '#f59e0b',
              border:     '1px solid rgba(245,158,11,0.25)',
            }}
          >
            🚀 إعداد سريع
          </span>
          <h1 className="text-2xl font-black text-white leading-tight">
            إعداد نحلة الذكية
          </h1>
          <p className="text-slate-400 text-sm mt-1.5 leading-relaxed">
            فعّل نحلة خلال دقائق وابدأ الرد على عملائك عبر واتساب.
          </p>
        </div>

        {loading ? (
          /* Skeleton */
          <div className="space-y-3 animate-pulse">
            {[1, 2, 3, 4].map(i => (
              <div
                key={i}
                className="h-16 rounded-xl"
                style={{ background: 'rgba(255,255,255,0.03)' }}
              />
            ))}
            <div className="h-14 rounded-2xl" style={{ background: 'rgba(245,158,11,0.07)' }} />
          </div>
        ) : (
          <div className="space-y-3">

            {/* WhatsApp status card */}
            <div
              className="rounded-xl p-4 flex items-center gap-3"
              style={{
                background: waConnected
                  ? 'rgba(37,211,102,0.06)'
                  : 'rgba(245,158,11,0.06)',
                border: waConnected
                  ? '1px solid rgba(37,211,102,0.2)'
                  : '1px solid rgba(245,158,11,0.2)',
              }}
            >
              <div
                className="w-9 h-9 rounded-full flex items-center justify-center text-lg shrink-0"
                style={{
                  background: waConnected
                    ? 'rgba(37,211,102,0.12)'
                    : 'rgba(245,158,11,0.12)',
                }}
              >
                💬
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-bold" style={{ color: waConnected ? '#4ade80' : '#f59e0b' }}>
                  واتساب البيزنس
                </p>
                <p className="text-xs mt-0.5" style={{ color: waConnected ? '#86efac' : '#fcd34d' }}>
                  {waConnected ? '✓ متصل — جاهز للردود التلقائية' : 'غير متصل — الربط من الإعدادات المتقدمة'}
                </p>
              </div>
              {waConnected && (
                <span
                  className="w-2 h-2 rounded-full bg-green-400 shrink-0"
                  style={{ animation: 'pulse 2s cubic-bezier(0.4,0,0.6,1) infinite' }}
                />
              )}
            </div>

            {/* Warning if WA not connected */}
            {!waConnected && (
              <div
                className="rounded-xl px-4 py-3 text-xs leading-relaxed"
                style={{
                  background: 'rgba(245,158,11,0.06)',
                  border:     '1px solid rgba(245,158,11,0.15)',
                  color:      '#fbbf24',
                }}
              >
                ⚠️ لن يعمل الرد التلقائي حتى يتم ربط واتساب من الإعدادات المتقدمة. يمكنك الحفظ الآن والربط لاحقاً.
              </div>
            )}

            {/* رقم واتساب */}
            <div
              className="rounded-xl p-4"
              style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
            >
              <label htmlFor="wa-number" className="text-slate-200 text-sm font-semibold block mb-2">
                رقم واتساب المتجر
              </label>
              <p className="text-slate-500 text-xs mb-3">أدخل الرقم بصيغة دولية — مثال: 966512345678</p>
              <input
                id="wa-number"
                type="tel"
                dir="ltr"
                placeholder="966512345678"
                value={form.whatsapp_number}
                onChange={e => update('whatsapp_number', e.target.value.replace(/\s/g, ''))}
                className="w-full rounded-lg px-3 py-2.5 text-sm font-mono focus:outline-none"
                style={{
                  background:  'rgba(255,255,255,0.06)',
                  border:      '1px solid rgba(255,255,255,0.12)',
                  color:       '#e2e8f0',
                  caretColor:  '#f59e0b',
                }}
              />
            </div>

            {/* أسلوب الرد */}
            <div
              className="rounded-xl p-4"
              style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
            >
              <p className="text-slate-200 text-sm font-semibold mb-1">أسلوب الرد</p>
              <p className="text-slate-500 text-xs mb-3">اختر طريقة رد نحلة على عملائك</p>
              <div className="flex flex-col gap-2">
                {TONES.map(t => (
                  <button
                    key={t.value}
                    type="button"
                    onClick={() => update('reply_tone', t.value)}
                    className="flex items-center gap-3 w-full rounded-lg px-3 py-2.5 text-right transition-all"
                    style={{
                      background: form.reply_tone === t.value
                        ? 'rgba(245,158,11,0.12)'
                        : 'rgba(255,255,255,0.03)',
                      border: form.reply_tone === t.value
                        ? '1px solid rgba(245,158,11,0.35)'
                        : '1px solid rgba(255,255,255,0.06)',
                    }}
                  >
                    <span className="text-base shrink-0">{t.label.split(' ')[0]}</span>
                    <div className="flex-1 min-w-0 text-right">
                      <p
                        className="text-sm font-semibold"
                        style={{ color: form.reply_tone === t.value ? '#f59e0b' : '#cbd5e1' }}
                      >
                        {t.label.split(' ').slice(1).join(' ')}
                      </p>
                      <p className="text-xs text-slate-500 truncate">{t.desc}</p>
                    </div>
                    {form.reply_tone === t.value && (
                      <span className="text-amber-400 shrink-0">✓</span>
                    )}
                  </button>
                ))}
              </div>
            </div>

            {/* تفعيل الرد التلقائي */}
            <Field
              label="تفعيل الرد التلقائي"
              desc="تشغيل الردود الذكية على رسائل واتساب"
              htmlFor="toggle-auto"
            >
              <Toggle
                id="toggle-auto"
                checked={form.nahla_enabled}
                onChange={v => update('nahla_enabled', v)}
              />
            </Field>

            {/* تفعيل استرجاع السلة */}
            <Field
              label="تفعيل استرجاع السلة المتروكة"
              desc="إرسال رسائل تلقائية للعملاء الذين لم يكملوا الطلب"
              htmlFor="toggle-cart"
            >
              <Toggle
                id="toggle-cart"
                checked={form.abandoned_cart_enabled}
                onChange={v => update('abandoned_cart_enabled', v)}
              />
            </Field>

            {/* نسبة الخصم */}
            {form.abandoned_cart_enabled && (
              <div
                className="rounded-xl p-4"
                style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.07)' }}
              >
                <label htmlFor="discount" className="text-slate-200 text-sm font-semibold block mb-1">
                  نسبة الخصم التلقائي
                </label>
                <p className="text-slate-500 text-xs mb-3">
                  الخصم المستخدم في رسائل استرجاع السلة — 0 لتعطيل الخصم
                </p>
                <div className="flex items-center gap-3">
                  <input
                    id="discount"
                    type="range"
                    min="0"
                    max="30"
                    step="5"
                    value={form.discount_percentage}
                    onChange={e => update('discount_percentage', Number(e.target.value))}
                    className="flex-1 accent-amber-500"
                  />
                  <span
                    className="w-14 text-center rounded-lg py-1 text-sm font-black"
                    style={{
                      background: 'rgba(245,158,11,0.12)',
                      color:      '#f59e0b',
                      border:     '1px solid rgba(245,158,11,0.25)',
                    }}
                  >
                    {form.discount_percentage}%
                  </span>
                </div>
              </div>
            )}

            {/* تفعيل الطيار الآلي */}
            <Field
              label="تفعيل الطيار الآلي"
              desc="تشغيل الردود التلقائية وإدارة المحادثات بالكامل"
              htmlFor="toggle-pilot"
            >
              <Toggle
                id="toggle-pilot"
                checked={form.autopilot_enabled}
                onChange={v => update('autopilot_enabled', v)}
              />
            </Field>

            {/* Save error */}
            {saveError && (
              <div
                className="rounded-xl px-4 py-3 text-xs"
                style={{
                  background: 'rgba(239,68,68,0.08)',
                  border:     '1px solid rgba(239,68,68,0.2)',
                  color:      '#fca5a5',
                }}
              >
                ⚠️ {saveError}
              </div>
            )}

            {/* Primary CTA */}
            <button
              type="button"
              onClick={handleSave}
              disabled={saving}
              className="w-full py-4 rounded-2xl font-black text-base flex items-center justify-center gap-2 transition-opacity"
              style={{
                background: '#f59e0b',
                color:      '#0f172a',
                boxShadow:  '0 6px 24px rgba(245,158,11,0.35)',
                opacity:    saving ? 0.7 : 1,
              }}
            >
              {saving ? (
                <>
                  <span className="w-4 h-4 border-2 border-current border-t-transparent rounded-full animate-spin" />
                  جارٍ الحفظ...
                </>
              ) : (
                <>✅ حفظ ومتابعة</>
              )}
            </button>

            {/* Advanced settings link */}
            <button
              type="button"
              onClick={() => window.open(ADVANCED_URL, '_blank')}
              className="w-full py-3 rounded-2xl text-sm font-semibold flex items-center justify-center gap-2"
              style={{
                background: 'rgba(255,255,255,0.04)',
                color:      '#64748b',
                border:     '1px solid rgba(255,255,255,0.07)',
              }}
            >
              ⚙️ فتح الإعدادات المتقدمة في لوحة نحلة
            </button>

          </div>
        )}
      </div>

      {/* Footer — subtle skip */}
      <div className="pt-6 pb-2 text-center">
        <button
          onClick={handleSkip}
          className="text-[11px] transition-colors"
          style={{ color: '#334155' }}
          onMouseEnter={e => (e.currentTarget.style.color = '#475569')}
          onMouseLeave={e => (e.currentTarget.style.color = '#334155')}
        >
          تخطي الإعداد والدخول للوحة التحكم
        </button>
      </div>
    </div>
  )
}
