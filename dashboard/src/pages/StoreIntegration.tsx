import { useCallback, useEffect, useState } from 'react'
import {
  Store,
  CheckCircle,
  XCircle,
  AlertCircle,
  Loader2,
  Plug,
  RefreshCw,
} from 'lucide-react'
import {
  storeIntegrationApi,
  type StoreIntegrationStatus,
  type StoreIntegrationTestResult,
} from '../api/storeIntegration'
import { featureRealityApi } from '../api/featureReality'
import { API_BASE } from '../api/client'
import { getToken } from '../auth'
import StoreSyncPanel from '../components/StoreSyncPanel'
import {
  SALLA_MERCHANT_COPY,
  deriveSallaMerchantIntegrationView,
  type SallaMerchantStatusLine,
} from '../utils/sallaMerchantIntegration'

function resolveSessionToken(): string {
  try {
    const t1 = getToken()
    if (t1) return t1
  } catch { /* localStorage blocked */ }
  try {
    const t2 = sessionStorage.getItem('nahla_token') || sessionStorage.getItem('token')
    if (t2) return t2
  } catch { /* sessionStorage blocked */ }
  try {
    const m = document.cookie.match(/(?:^|;\s*)nahla_token=([^;]+)/)
    if (m && m[1]) return decodeURIComponent(m[1])
  } catch { /* cookies blocked */ }
  return ''
}

function relativeSyncTime(iso: string | null | undefined): string | null {
  if (!iso) return null
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return 'منذ ثوانٍ'
  if (diff < 3600) return `منذ ${Math.floor(diff / 60)} دقيقة`
  if (diff < 86400) return `منذ ${Math.floor(diff / 3600)} ساعة`
  return `منذ ${Math.floor(diff / 86400)} يوم`
}

function StatusToneClasses(tone: SallaMerchantStatusLine['tone']) {
  if (tone === 'ok') return 'bg-emerald-50 text-emerald-800 border-emerald-200'
  if (tone === 'warn') return 'bg-amber-50 text-amber-900 border-amber-200'
  return 'bg-slate-50 text-slate-600 border-slate-200'
}

function MerchantStatusCard({
  title,
  line,
}: {
  title: string
  line: SallaMerchantStatusLine
}) {
  return (
    <div className={`rounded-xl border p-4 ${StatusToneClasses(line.tone)}`}>
      <p className="text-xs font-semibold text-slate-500">{title}</p>
      <p className="text-sm font-bold mt-1">{line.label}</p>
      {line.hint ? (
        <p className="text-[11px] mt-1 opacity-80 leading-snug">{line.hint}</p>
      ) : null}
    </div>
  )
}

export default function StoreIntegration() {
  const [status, setStatus] = useState<StoreIntegrationStatus | null>(null)
  const [sallaStatus, setSallaStatus] = useState<{
    api_sync_enabled: boolean
    embedded_connected: boolean
    needs_reauth: boolean
    api_connected_at?: string
    sync_app_configured: boolean
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<StoreIntegrationTestResult | null>(null)
  const [oauthLoading, setOauthLoading] = useState(false)
  const [oauthMessage, setOauthMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [lastSyncAt, setLastSyncAt] = useState<string | null>(null)

  const loadSettings = useCallback(async () => {
    setLoading(true)
    try {
      const [settings, integration] = await Promise.all([
        storeIntegrationApi.getSettings(),
        featureRealityApi.sallaIntegrationStatus().catch(() => null),
      ])
      setStatus(settings)
      if (integration) {
        setSallaStatus({
          api_sync_enabled: integration.api_sync_enabled,
          embedded_connected: integration.embedded_connected ?? true,
          needs_reauth: integration.needs_reauth ?? settings.needs_reauth ?? false,
          api_connected_at: integration.api_connected_at,
          sync_app_configured: integration.sync_app_configured ?? true,
        })
      } else {
        setSallaStatus({
          api_sync_enabled: false,
          embedded_connected: settings.configured ?? false,
          needs_reauth: settings.needs_reauth ?? false,
          sync_app_configured: true,
        })
      }
      try {
        const { storeSyncApi } = await import('../api/storeSync')
        const sync = await storeSyncApi.getStatus()
        setLastSyncAt(sync.last_incremental_sync_at ?? sync.last_full_sync_at ?? null)
      } catch {
        setLastSyncAt(null)
      }
    } catch {
      setStatus(null)
      setSallaStatus(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSettings()
    const params = new URLSearchParams(window.location.search)
    if (params.get('salla_connected') === 'true') {
      const name = params.get('name') || ''
      setOauthMessage({
        type: 'success',
        text: name
          ? `تم ربط سلة بنجاح — ${name}`
          : SALLA_MERCHANT_COPY.linkCompleteMessage,
      })
      window.history.replaceState({}, '', window.location.pathname)
      void loadSettings()
    } else if (params.get('salla_oauth') === 'success') {
      setOauthMessage({ type: 'success', text: SALLA_MERCHANT_COPY.linkCompleteMessage })
      window.history.replaceState({}, '', window.location.pathname)
      void loadSettings()
    } else if (params.get('salla_error') || params.get('salla_oauth') === 'error') {
      setOauthMessage({
        type: 'error',
        text: 'تعذّر إكمال ربط سلة. أعد المحاولة من زر إكمال الربط.',
      })
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [loadSettings])

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await storeIntegrationApi.test()
      setTestResult(result)
    } catch {
      setTestResult({ status: 'error', error: 'تعذّر اختبار الاتصال. حاول مرة أخرى.' })
    } finally {
      setTesting(false)
    }
  }

  function handleOAuthConnect() {
    setOauthLoading(true)
    const token = resolveSessionToken()
    if (!token) {
      alert('انتهت الجلسة، الرجاء تسجيل الدخول مرة أخرى')
      setOauthLoading(false)
      return
    }
    const startUrl = `${API_BASE}/api/salla/oauth/start?token=${encodeURIComponent(token)}`
    if (!/[?&]token=/.test(startUrl)) {
      alert('انتهت الجلسة، الرجاء تسجيل الدخول مرة أخرى')
      setOauthLoading(false)
      return
    }
    window.location.href = startUrl
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
      </div>
    )
  }

  const view = deriveSallaMerchantIntegrationView({
    configured: status?.configured,
    enabled: status?.enabled,
    needsReauth: sallaStatus?.needs_reauth ?? status?.needs_reauth,
    apiSyncEnabled: sallaStatus?.api_sync_enabled,
    embeddedConnected: sallaStatus?.embedded_connected,
    storeName: status?.store_name,
    lastSyncAt,
  })

  const lastSyncLabel = relativeSyncTime(lastSyncAt ?? sallaStatus?.api_connected_at ?? status?.connected_at)

  return (
    <div className="max-w-2xl mx-auto space-y-6" data-merchant-salla-integration>

      <div>
        <h1 className="text-2xl font-bold text-slate-900">ربط المتجر</h1>
        <p className="text-slate-500 mt-1 text-sm leading-relaxed">
          اربط متجرك على سلة لتفعيل مزامنة المنتجات والطلبات والكوبونات مع نحلة.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <MerchantStatusCard title={SALLA_MERCHANT_COPY.storeTitle} line={view.store} />
        <MerchantStatusCard title={SALLA_MERCHANT_COPY.fullApiTitle} line={view.fullApi} />
        <MerchantStatusCard title={SALLA_MERCHANT_COPY.couponSyncTitle} line={view.couponSync} />
      </div>

      {view.bannerMessage && view.fullApi.tone === 'ok' ? (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 p-4 flex items-start gap-3">
          <CheckCircle className="w-5 h-5 text-emerald-600 shrink-0 mt-0.5" />
          <p className="text-sm text-emerald-900">{view.bannerMessage}</p>
        </div>
      ) : null}

      {(view.showCompleteCta || view.showReauthCta) && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-5 space-y-3">
          <div className="flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <p className="text-sm font-semibold text-amber-900">{view.ctaHelper}</p>
              <p className="text-xs text-amber-800 leading-relaxed">
                {SALLA_MERCHANT_COPY.couponSyncNeedsApiMessage}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleOAuthConnect}
            disabled={oauthLoading || sallaStatus?.sync_app_configured === false}
            className="flex items-center justify-center gap-2 w-full px-5 py-3 rounded-xl bg-[#1d2939] text-white text-sm font-semibold hover:bg-[#101828] transition-colors disabled:opacity-60"
          >
            {oauthLoading
              ? <Loader2 className="w-4 h-4 animate-spin" />
              : <Store className="w-4 h-4" />}
            {view.ctaLabel}
          </button>
          {sallaStatus?.sync_app_configured === false ? (
            <p className="text-[11px] text-amber-800">
              إعداد الربط غير مكتمل على المنصة. تواصل مع دعم نحلة.
            </p>
          ) : (
            <p className="text-[11px] text-amber-700">{SALLA_MERCHANT_COPY.openFromSallaHint}</p>
          )}
        </div>
      )}

      {view.fullApi.tone === 'ok' ? (
        <div className="rounded-xl border border-emerald-100 bg-emerald-50/60 p-4 text-sm text-emerald-900">
          {SALLA_MERCHANT_COPY.couponSyncReadyMessage}
        </div>
      ) : null}

      {oauthMessage ? (
        <div className={`rounded-xl p-4 text-sm flex items-start gap-3 ${
          oauthMessage.type === 'success'
            ? 'bg-green-50 border border-green-200 text-green-800'
            : 'bg-red-50 border border-red-200 text-red-800'
        }`}>
          {oauthMessage.type === 'success'
            ? <CheckCircle className="w-4 h-4 mt-0.5 shrink-0" />
            : <XCircle className="w-4 h-4 mt-0.5 shrink-0" />}
          <span>{oauthMessage.text}</span>
        </div>
      ) : null}

      <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h2 className="font-semibold text-slate-900 text-sm">حالة الاتصال</h2>
            {lastSyncLabel ? (
              <p className="text-xs text-slate-500 mt-0.5">
                {SALLA_MERCHANT_COPY.lastSyncLabel}: {lastSyncLabel}
              </p>
            ) : (
              <p className="text-xs text-slate-500 mt-0.5">لم تتم مزامنة بعد</p>
            )}
          </div>
          <button
            type="button"
            onClick={handleTest}
            disabled={testing || !view.storeConnectedForSync}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
          >
            {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            {SALLA_MERCHANT_COPY.testConnection}
          </button>
        </div>

        {!view.storeConnectedForSync ? (
          <p className="text-xs text-slate-500 flex items-center gap-2">
            <Plug className="w-3.5 h-3.5" />
            أكمل ربط سلة أولاً لاختبار الاتصال والمزامنة.
          </p>
        ) : null}
      </div>

      {testResult ? (
        <div className={`rounded-xl border p-5 ${
          testResult.status === 'ok'
            ? 'bg-emerald-50 border-emerald-200'
            : testResult.status === 'not_configured'
              ? 'bg-amber-50 border-amber-200'
              : 'bg-red-50 border-red-200'
        }`}>
          <div className="flex items-start gap-3">
            {testResult.status === 'ok'
              ? <CheckCircle className="w-5 h-5 text-emerald-500 shrink-0 mt-0.5" />
              : testResult.status === 'not_configured'
                ? <AlertCircle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
                : <XCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />}
            <div>
              {testResult.status === 'ok' && (
                <>
                  <p className="font-semibold text-emerald-800 text-sm">الاتصال ناجح</p>
                  <p className="text-emerald-700 text-xs mt-1">
                    تم التحقق من الاتصال بسلة بنجاح.
                    {typeof testResult.products_found === 'number'
                      ? ` (${testResult.products_found} منتج)`
                      : ''}
                  </p>
                </>
              )}
              {testResult.status === 'not_configured' && (
                <>
                  <p className="font-semibold text-amber-800 text-sm">الربط غير مكتمل</p>
                  <p className="text-amber-700 text-xs mt-1">أكمل ربط سلة ثم أعد الاختبار.</p>
                </>
              )}
              {testResult.status === 'error' && (
                <>
                  <p className="font-semibold text-red-800 text-sm">تعذّر اختبار الاتصال</p>
                  <p className="text-red-700 text-xs mt-1">
                    {testResult.error?.includes('{')
                      ? 'تحقق من الربط وحاول مرة أخرى.'
                      : (testResult.error || 'حاول مرة أخرى لاحقاً.')}
                  </p>
                </>
              )}
            </div>
          </div>
        </div>
      ) : null}

      <StoreSyncPanel isStoreConnected={view.storeConnectedForSync} />

      <div className="bg-blue-50 border border-blue-200 rounded-xl p-5">
        <div className="flex items-start gap-3">
          <Store className="w-5 h-5 text-blue-500 shrink-0 mt-0.5" />
          <div className="space-y-2 text-xs text-blue-700 leading-relaxed">
            <p className="font-semibold text-blue-900 text-sm">كيف أكمل الربط؟</p>
            <p>1. افتح تطبيق نحلة من لوحة سلة (تطبيقاتي).</p>
            <p>2. اضغط «{SALLA_MERCHANT_COPY.completeLinkCta}» لإتمام التفويض.</p>
            <p>3. بعد اكتمال الربط ستظهر حالة «{SALLA_MERCHANT_COPY.fullApiComplete}» ويمكنك مزامنة البيانات.</p>
          </div>
        </div>
      </div>
    </div>
  )
}
