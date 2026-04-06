/**
 * WhatsAppConnect.tsx
 * ────────────────────
 * Merchant-facing WhatsApp / Meta Embedded Signup flow.
 *
 * Flow:
 *  1. Load page → GET /whatsapp/connection for current state
 *  2. Merchant clicks "ربط واتساب"
 *  3. We POST /whatsapp/connection/start to mark as pending + get Meta config
 *  4. We open FB.login() popup using the Facebook JavaScript SDK
 *  5. On success, POST /whatsapp/connection/callback with the code
 *  6. Backend exchanges code with Meta, persists tokens, returns connected state
 *  7. UI updates to "مرتبط" with phone number
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  BadgeCheck,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  Loader2,
  MessageCircle,
  RefreshCw,
  ShieldCheck,
  Unplug,
  Wifi,
  WifiOff,
  XCircle,
  Zap,
} from 'lucide-react'
import { whatsappConnectApi, type WaConnection, type WaConnectionStatus, type WaHealthResult } from '../api/whatsappConnect'
import { useLanguage } from '../i18n/context'

// ── Types ─────────────────────────────────────────────────────────────────────

declare global {
  interface Window {
    FB?: {
      init: (options: Record<string, unknown>) => void
      login: (callback: (response: FBLoginResponse) => void, options: Record<string, unknown>) => void
    }
    fbAsyncInit?: () => void
  }
}

interface FBLoginResponse {
  status: 'connected' | 'not_authorized' | 'unknown'
  authResponse?: {
    code?: string
    accessToken?: string
    userID?: string
  }
}

// ── Status config map ─────────────────────────────────────────────────────────

const STATUS_STYLE: Record<WaConnectionStatus, {
  color: string
  bg: string
  border: string
  icon: React.ElementType
}> = {
  not_connected: { color: 'text-slate-500',   bg: 'bg-slate-100',  border: 'border-slate-200',  icon: WifiOff },
  pending:       { color: 'text-amber-600',   bg: 'bg-amber-50',   border: 'border-amber-200',  icon: Loader2 },
  connected:     { color: 'text-emerald-600', bg: 'bg-emerald-50', border: 'border-emerald-200',icon: Wifi },
  error:         { color: 'text-red-600',     bg: 'bg-red-50',     border: 'border-red-200',    icon: XCircle },
  disconnected:  { color: 'text-slate-500',   bg: 'bg-slate-100',  border: 'border-slate-200',  icon: WifiOff },
  needs_reauth:  { color: 'text-orange-600',  bg: 'bg-orange-50',  border: 'border-orange-200', icon: AlertTriangle },
}

// ── Small helper components ───────────────────────────────────────────────────

function StatusBadge({ status }: { status: WaConnectionStatus }) {
  const { t } = useLanguage()
  const cfg  = STATUS_STYLE[status] ?? STATUS_STYLE.not_connected
  const Icon = cfg.icon
  const label = t(tr => tr.whatsappConnect.status[status] ?? status)
  return (
    <span className={`inline-flex items-center gap-1.5 text-xs font-bold px-2.5 py-1 rounded-full border ${cfg.color} ${cfg.bg} ${cfg.border}`}>
      <Icon className={`w-3.5 h-3.5 ${status === 'pending' ? 'animate-spin' : ''}`} />
      {label}
    </span>
  )
}

function HealthCheck({ health }: { health: WaHealthResult }) {
  const [open, setOpen] = useState(false)
  const checks = health.checks

  return (
    <div className="rounded-xl border border-slate-200 overflow-hidden">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center justify-between px-4 py-3 bg-slate-50 hover:bg-slate-100 transition-colors"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <ShieldCheck className="w-4 h-4 text-slate-500" />
          فحص المتطلبات
        </span>
        <div className="flex items-center gap-2">
          {health.healthy
            ? <span className="text-xs text-emerald-600 font-bold">كل شيء يعمل</span>
            : <span className="text-xs text-red-500 font-bold">تحقق مطلوب</span>
          }
          {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
        </div>
      </button>

      {open && (
        <div className="divide-y divide-slate-100">
          {(Object.entries(checks) as [string, boolean][]).map(([key, val]) => {
            const labels: Record<string, string> = {
              has_connection:   'اتصال موجود',
              token_present:    'رمز التوثيق محفوظ',
              token_valid:      'رمز التوثيق سليم',
              webhook_verified: 'Webhook مفعّل',
              sending_enabled:  'الإرسال مفعّل',
            }
            return (
              <div key={key} className="flex items-center justify-between px-4 py-2.5">
                <span className="text-sm text-slate-600">{labels[key] ?? key}</span>
                {val
                  ? <CheckCircle2 className="w-4 h-4 text-emerald-500" />
                  : <XCircle className="w-4 h-4 text-slate-300" />
                }
              </div>
            )
          })}
          {health.last_error && (
            <div className="px-4 py-2.5 bg-red-50">
              <p className="text-xs text-red-600 font-mono break-all">{health.last_error}</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function WhatsAppConnect() {
  const { t } = useLanguage()
  const wt = t(tr => tr.whatsappConnect)
  const [conn, setConn]           = useState<WaConnection | null>(null)
  const [health, setHealth]       = useState<WaHealthResult | null>(null)
  const [loading, setLoading]     = useState(true)
  const [connecting, setConnecting] = useState(false)
  const [verifying, setVerifying] = useState(false)
  const [error, setError]         = useState<string | null>(null)
  const sdkReady                  = useRef(false)

  // ── Load current state ─────────────────────────────────────────────────────

  const loadStatus = useCallback(async () => {
    try {
      const [status, h] = await Promise.all([
        whatsappConnectApi.getStatus(),
        whatsappConnectApi.health(),
      ])
      setConn(status)
      setHealth(h)
    } catch {
      // silent
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { loadStatus() }, [loadStatus])

  // ── Load Facebook JS SDK ───────────────────────────────────────────────────

  const loadFbSdk = useCallback((appId: string, version: string) => {
    return new Promise<void>((resolve) => {
      if (window.FB) { resolve(); return }
      window.fbAsyncInit = () => {
        window.FB!.init({
          appId,
          autoLogAppEvents: true,
          xfbml:            true,
          version:          version || 'v20.0',
        })
        sdkReady.current = true
        resolve()
      }
      const script = document.createElement('script')
      script.src   = 'https://connect.facebook.net/en_US/sdk.js'
      script.async = true
      script.defer = true
      document.body.appendChild(script)
    })
  }, [])

  // ── Embedded Signup flow ───────────────────────────────────────────────────

  const handleConnect = useCallback(async () => {
    setConnecting(true)
    setError(null)

    try {
      // 1. Mark as pending + get Meta config from backend
      const startData = await whatsappConnectApi.start()

      if (!startData.meta_app_id || startData.meta_app_id === 'CONFIGURE_META_APP_ID') {
        setError('META_APP_ID غير مهيأ على الخادم. يرجى إضافة متغيرات البيئة أولاً.')
        setConnecting(false)
        return
      }

      // 2. Load FB SDK
      await loadFbSdk(startData.meta_app_id, startData.graph_version)

      // 3. Open Meta Embedded Signup popup
      // NOTE: FB.login callback must be a plain function (not async) — use void IIFE inside
      window.FB!.login(
        (response: FBLoginResponse) => {
          if (response.status === 'connected' && response.authResponse?.code) {
            void (async () => {
              try {
                // 4. Exchange code with our backend
                const result = await whatsappConnectApi.callback({
                  code: response.authResponse!.code!,
                })
                setConn(result as WaConnection)
                await loadStatus()
                setError(null)
              } catch (err) {
                const msg = err instanceof Error ? err.message : 'فشل في إتمام الربط'
                setError(msg)
                await loadStatus()
              } finally {
                setConnecting(false)
              }
            })()
          } else if (response.status === 'not_authorized') {
            setError('لم يتم منح الصلاحيات المطلوبة. يرجى قبول الأذونات عند الطلب.')
            void loadStatus()
            setConnecting(false)
          } else {
            setError('أُلغي الربط أو لم تكتمل العملية.')
            void loadStatus()
            setConnecting(false)
          }
        },
        {
          config_id:    '',   // Meta App config ID (optional)
          response_type: 'code',
          override_default_response_type: true,
          scope: startData.scope,
          extras: startData.extras,
        }
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'حدث خطأ أثناء التهيئة'
      setError(msg)
      setConnecting(false)
    }
  }, [loadFbSdk, loadStatus])

  // ── Verify ─────────────────────────────────────────────────────────────────

  const handleVerify = useCallback(async () => {
    setVerifying(true)
    try {
      await whatsappConnectApi.verify()
      await loadStatus()
    } finally {
      setVerifying(false)
    }
  }, [loadStatus])

  // ── Disconnect ─────────────────────────────────────────────────────────────

  const handleDisconnect = useCallback(async () => {
    if (!confirm('هل أنت متأكد من فصل واتساب؟ سيتوقف الرد التلقائي فوراً.')) return
    await whatsappConnectApi.disconnect()
    await loadStatus()
  }, [loadStatus])

  // ── Reconnect ──────────────────────────────────────────────────────────────

  const handleReconnect = useCallback(async () => {
    await whatsappConnectApi.reconnect()
    await handleConnect()
  }, [handleConnect])

  // ─────────────────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
      </div>
    )
  }

  const status   = conn?.status ?? 'not_connected'
  const cfg      = STATUS_STYLE[status]
  const isActive = status === 'connected'
  const needsAction = status === 'error' || status === 'needs_reauth' || status === 'disconnected'

  return (
    <div className="max-w-xl mx-auto space-y-5">

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2">
          <MessageCircle className="w-6 h-6 text-emerald-500" />
          {wt.title}
        </h1>
        <p className="text-slate-500 mt-1 text-sm">
          {wt.subtitle}
        </p>
      </div>

      {/* Status card */}
      <div className={`rounded-2xl border p-5 ${cfg.bg} ${cfg.border}`}>
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className={`w-11 h-11 rounded-xl flex items-center justify-center ${isActive ? 'bg-emerald-100' : 'bg-white/60'}`}>
              {isActive
                ? <BadgeCheck className="w-6 h-6 text-emerald-600" />
                : <cfg.icon className={`w-6 h-6 ${cfg.color} ${status === 'pending' ? 'animate-spin' : ''}`} />
              }
            </div>
            <div>
              <StatusBadge status={status} />
              {conn?.phone_number && (
                <p className="text-sm font-semibold text-slate-800 mt-1">
                  {conn.business_display_name ?? conn.phone_number}
                </p>
              )}
              {conn?.phone_number && (
                <p className="text-xs text-slate-500 mt-0.5 font-mono">{conn.phone_number}</p>
              )}
              {!isActive && !conn?.phone_number && (
                <p className="text-xs text-slate-500 mt-1">
                  {wt.statusHint}
                </p>
              )}
            </div>
          </div>

          {isActive && conn?.connected_at && (
            <p className="text-xs text-slate-400 shrink-0">
              {new Date(conn.connected_at).toLocaleDateString('ar-SA')}
            </p>
          )}
        </div>

        {/* Connected metadata */}
        {isActive && (
          <div className="mt-4 grid grid-cols-2 gap-3 text-xs">
            {conn?.whatsapp_business_account_id && (
              <div className="bg-white/60 rounded-lg p-2.5">
                <p className="text-slate-400 mb-0.5">WABA ID</p>
                <p className="font-mono text-slate-700 truncate">{conn.whatsapp_business_account_id}</p>
              </div>
            )}
            {conn?.phone_number_id && (
              <div className="bg-white/60 rounded-lg p-2.5">
                <p className="text-slate-400 mb-0.5">Phone Number ID</p>
                <p className="font-mono text-slate-700 truncate">{conn.phone_number_id}</p>
              </div>
            )}
          </div>
        )}

        {/* Error message */}
        {conn?.last_error && (status === 'error' || status === 'needs_reauth') && (
          <div className="mt-3 flex items-start gap-2 bg-white/60 rounded-lg p-3">
            <AlertCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
            <p className="text-xs text-red-600 font-mono break-all">{conn.last_error}</p>
          </div>
        )}
      </div>

      {/* Error from connect attempt */}
      {error && (
        <div className="flex items-start gap-2.5 bg-red-50 border border-red-200 rounded-xl p-4">
          <XCircle className="w-4 h-4 text-red-500 shrink-0 mt-0.5" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {/* Primary action */}
      <div className="space-y-3">
        {/* Connect button — for not_connected, disconnected, or stuck pending */}
        {(status === 'not_connected' || status === 'disconnected' || status === 'pending') && (
          <button
            onClick={handleConnect}
            disabled={connecting}
            className="w-full flex items-center justify-center gap-2.5 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-sm py-3.5 rounded-xl transition-all disabled:opacity-60 shadow-lg shadow-emerald-600/20"
          >
            {connecting
              ? <><Loader2 className="w-4 h-4 animate-spin" /> {t(tr => tr.common.loading)}</>
              : <><MessageCircle className="w-4 h-4" /> {wt.connectBtn}</>
            }
          </button>
        )}

        {/* Reconnect — for error or needs_reauth */}
        {needsAction && (
          <button
            onClick={handleReconnect}
            disabled={connecting}
            className="w-full flex items-center justify-center gap-2.5 bg-amber-500 hover:bg-amber-400 text-white font-bold text-sm py-3.5 rounded-xl transition-all disabled:opacity-60"
          >
            {connecting
              ? <><Loader2 className="w-4 h-4 animate-spin" /> {t(tr => tr.common.loading)}</>
              : <><RefreshCw className="w-4 h-4" /> {wt.reconnectBtn}</>
            }
          </button>
        )}

        {/* Actions for connected state */}
        {isActive && (
          <div className="flex gap-2.5">
            <button
              onClick={handleVerify}
              disabled={verifying}
              className="flex-1 flex items-center justify-center gap-2 border border-slate-300 hover:border-brand-400 text-slate-700 font-medium text-sm py-2.5 rounded-xl transition-all hover:bg-slate-50"
            >
              {verifying
                ? <Loader2 className="w-4 h-4 animate-spin" />
                : <Zap className="w-4 h-4" />
              }
              {t(tr => tr.common.test)}
            </button>
            <button
              onClick={handleDisconnect}
              className="flex items-center justify-center gap-2 border border-red-200 text-red-500 hover:bg-red-50 font-medium text-sm px-4 py-2.5 rounded-xl transition-all"
            >
              <Unplug className="w-4 h-4" />
              {wt.disconnectBtn}
            </button>
          </div>
        )}
      </div>

      {/* Health check panel */}
      {health && status !== 'not_connected' && (
        <HealthCheck health={health} />
      )}

      {/* Sending prerequisites notice */}
      {isActive && !conn?.sending_enabled && (
        <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-4">
          <AlertTriangle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
          <div className="text-sm">
            <p className="font-semibold text-amber-800">
              {t(tr => tr.meta.code === 'ar' ? 'الإرسال غير مفعّل بعد' : 'Sending not yet enabled')}
            </p>
            <p className="text-amber-700 mt-0.5 text-xs">
              {t(tr => tr.meta.code === 'ar'
                ? 'تحقق من إعدادات رقم الهاتف في Meta Business Manager وتأكد من اجتياز التحقق.'
                : 'Check your phone number settings in Meta Business Manager and ensure verification is complete.')}
            </p>
          </div>
        </div>
      )}

      {/* How it works */}
      <div className="bg-blue-50 border border-blue-200 rounded-xl p-5 space-y-3">
        <p className="font-semibold text-blue-900 text-sm flex items-center gap-2">
          <ExternalLink className="w-4 h-4" />
          {wt.howTitle}
        </p>
        <ol className="space-y-2 text-xs text-blue-700 list-none">
          {[wt.howStep1, wt.howStep2, wt.howStep3, wt.howStep4, wt.howStep5].map((step, i) => (
            <li key={i} className="flex gap-2">
              <span className="shrink-0 w-4 h-4 rounded-full bg-blue-200 text-blue-700 flex items-center justify-center font-bold text-[10px]">
                {i + 1}
              </span>
              {step}
            </li>
          ))}
        </ol>
      </div>

      {/* Meta prerequisites */}
      <div className="text-xs text-slate-400 space-y-1 border-t border-slate-100 pt-4">
        <p className="font-medium text-slate-500">{wt.prereqTitle}:</p>
        <ul className="list-disc list-inside space-y-0.5">
          <li>{wt.prereq1}</li>
          <li>{wt.prereq2}</li>
          <li>META_APP_ID / META_APP_SECRET (environment variables)</li>
          <li>{t(tr => tr.meta.code === 'ar' ? 'اتصال Webhook مُهيأ في Meta Developer Portal' : 'Webhook configured in Meta Developer Portal')}</li>
        </ul>
      </div>
    </div>
  )
}
