import { useState, useEffect } from 'react'
import {
  Store,
  CheckCircle,
  XCircle,
  AlertCircle,
  Loader2,
  Eye,
  EyeOff,
  Save,
  Plug,
  RefreshCw,
} from 'lucide-react'
import {
  storeIntegrationApi,
  type StoreIntegrationStatus,
  type StoreIntegrationTestResult,
} from '../api/storeIntegration'
import { apiCall } from '../api/client'
import StoreSyncPanel from '../components/StoreSyncPanel'

export default function StoreIntegration() {
  const [status, setStatus] = useState<StoreIntegrationStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<StoreIntegrationTestResult | null>(null)
  const [showApiKey, setShowApiKey] = useState(false)
  const [saveSuccess, setSaveSuccess] = useState(false)
  const [oauthLoading, setOauthLoading] = useState(false)
  const [oauthMessage, setOauthMessage] = useState<{type: 'success'|'error', text: string} | null>(null)
  const [easyReinstall, setEasyReinstall] = useState<{
    message: string
    salla_apps_url: string
    steps: string[]
    note?: string
  } | null>(null)

  // Form state
  const [platform, setPlatform] = useState('salla')
  const [apiKey, setApiKey] = useState('')
  const [storeId, setStoreId] = useState('')
  const [webhookSecret, setWebhookSecret] = useState('')
  const [enabled, setEnabled] = useState(true)

  useEffect(() => {
    loadSettings()
    // Check if redirected back from Salla OAuth
    const params = new URLSearchParams(window.location.search)
    if (params.get('salla_connected') === 'true') {
      const name = params.get('name') || ''
      setOauthMessage({ type: 'success', text: `تم ربط المتجر بنجاح! ${name ? '(' + name + ')' : ''}` })
      window.history.replaceState({}, '', window.location.pathname)
      loadSettings()
    } else if (params.get('salla_error')) {
      const err = params.get('salla_error')
      const msgs: Record<string, string> = {
        invalid_state:        'رفضت سلة الطلب — تأكد أن التطبيق مفعّل في حساب شركاء سلة أو جرّب ربط متجرك كمتجر تجريبي.',
        token_exchange_failed:'فشل تبادل الرمز مع سلة — تحقق من Client Secret.',
        app_not_configured:   'لم يتم ضبط بيانات التطبيق في الخادم.',
        missing_code:         'لم يُرسل كود التفويض من سلة.',
        network_error:        'خطأ في الاتصال بسلة.',
        db_save_failed:       'تم الربط لكن فشل الحفظ في قاعدة البيانات.',
      }
      setOauthMessage({ type: 'error', text: msgs[err!] || `خطأ: ${err}` })
      window.history.replaceState({}, '', window.location.pathname)
    }
  }, [])

  async function loadSettings() {
    setLoading(true)
    try {
      const data = await storeIntegrationApi.getSettings()
      setStatus(data)
      if (data.configured) {
        setPlatform(data.platform ?? 'salla')
        setStoreId(data.store_id ?? '')
        setEnabled(data.enabled)
      }
    } catch {
      // silent — show not configured state
    } finally {
      setLoading(false)
    }
  }

  async function handleSave() {
    setSaving(true)
    setSaveSuccess(false)
    setTestResult(null)
    try {
      await storeIntegrationApi.saveSettings({
        platform,
        api_key: apiKey,
        store_id: storeId,
        webhook_secret: webhookSecret,
        enabled,
      })
      setSaveSuccess(true)
      await loadSettings()
      setTimeout(() => setSaveSuccess(false), 3000)
    } catch (err) {
      alert('فشل الحفظ. تحقق من الاتصال وحاول مرة أخرى.')
    } finally {
      setSaving(false)
    }
  }

  async function handleTest() {
    setTesting(true)
    setTestResult(null)
    try {
      const result = await storeIntegrationApi.test()
      setTestResult(result)
    } catch {
      setTestResult({ status: 'error', error: 'فشل الاتصال بالخادم' })
    } finally {
      setTesting(false)
    }
  }

  async function handleOAuthConnect() {
    setOauthLoading(true)
    try {
      const data = await apiCall<{ url: string }>('/api/salla/authorize')
      window.location.href = data.url
    } catch {
      alert('تعذّر الحصول على رابط التفويض. تأكد من تهيئة SALLA_CLIENT_ID في الخادم.')
    } finally {
      setOauthLoading(false)
    }
  }

  async function handleFixConnection() {
    setOauthLoading(true)
    setOauthMessage(null)
    try {
      const data = await apiCall<{
        action: string
        url?: string
        message: string
        salla_apps_url?: string
        steps?: string[]
        note?: string
      }>('/api/salla/reconnect', { method: 'POST' })

      if (data.action === 'refreshed' || data.action === 'reactivated') {
        setOauthMessage({ type: 'success', text: `✓ ${data.message}` })
        await loadSettings()
      } else if (data.action === 'easy_reinstall_required') {
        setEasyReinstall({
          message:        data.message,
          salla_apps_url: data.salla_apps_url || 'https://s.salla.sa/apps',
          steps:          data.steps || [],
          note:           data.note,
        })
      } else if (data.action === 'oauth_required' && data.url) {
        window.location.href = data.url
      }
    } catch {
      setOauthMessage({ type: 'error', text: 'تعذّر استعادة الاتصال — تحقق من الاتصال بالخادم.' })
    } finally {
      setOauthLoading(false)
    }
  }

  async function handleDisable() {
    if (!confirm('هل أنت متأكد من إيقاف تشغيل ربط المتجر؟')) return
    try {
      await storeIntegrationApi.disable()
      await loadSettings()
    } catch {
      alert('فشل إيقاف التشغيل.')
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
      </div>
    )
  }

  const isConfigured = status?.configured && status?.enabled

  return (
    <div className="max-w-2xl mx-auto space-y-6">

      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">ربط المتجر</h1>
        <p className="text-slate-500 mt-1 text-sm">
          اربط متجرك الإلكتروني لتمكين وكيل المبيعات من إنشاء الطلبات الحقيقية وجلب المنتجات مباشرة.
        </p>
      </div>

      {/* Status Card */}
      <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-5">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${isConfigured ? 'bg-emerald-50' : 'bg-slate-100'}`}>
            {isConfigured
              ? <CheckCircle className="w-5 h-5 text-emerald-500" />
              : <Plug className="w-5 h-5 text-slate-400" />
            }
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <p className="font-semibold text-slate-900 text-sm">
                {isConfigured
                  ? `متصل بـ ${status?.platform === 'salla' ? 'سلة' : status?.platform}`
                  : 'غير متصل'}
              </p>
              {isConfigured && status?.easy_mode && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-50 border border-blue-200 text-blue-700 text-[10px] font-bold">
                  Easy Mode (تطبيق سلة)
                </span>
              )}
              {isConfigured && status?.api_key_source === 'manual' && !status?.easy_mode && (
                <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-50 border border-amber-200 text-amber-700 text-[10px] font-bold">
                  ربط يدوي (Account Token)
                </span>
              )}
            </div>
            <p className="text-slate-500 text-xs mt-0.5">
              {isConfigured
                ? `معرّف المتجر: ${status?.store_id || '—'}${status?.store_name ? ` · ${status.store_name}` : ''}${status?.api_key_hint ? ` · المفتاح: ${status.api_key_hint}` : ''}`
                : 'أضف بيانات الاعتماد أدناه لربط متجرك'}
            </p>
          </div>
          {isConfigured && (
            <button
              onClick={handleDisable}
              className="ms-auto text-xs text-red-500 hover:text-red-700 transition-colors"
            >
              إيقاف التشغيل
            </button>
          )}
        </div>
      </div>

      {/* Superseded (legacy) integrations warning */}
      {(status?.superseded_integrations?.length ?? 0) > 0 && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
          <div className="flex items-start gap-3">
            <AlertCircle className="w-5 h-5 text-slate-500 shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-slate-800">
                ربط قديم غير مُستخدم ({status?.superseded_integrations?.length})
              </p>
              <p className="text-xs text-slate-600 mt-0.5">
                وُجد ربط سابق لنفس المتجر لم يعد قيد الاستخدام لأن ربط
                {' '}<strong className="text-blue-700">Easy Mode</strong>{' '} هو المصدر الأساسي حالياً.
                المزامنة الآن تستخدم رموز Easy Mode، وليس المفاتيح القديمة.
              </p>
              <ul className="mt-2 space-y-1 text-[11px] text-slate-500">
                {status?.superseded_integrations?.map(s => (
                  <li key={s.id} className="font-mono">
                    #{s.id} · مفتاح: {s.api_key_hint || '—'} · {s.easy_mode ? 'easy_mode' : (s.api_key_source || 'manual')}
                    {s.superseded_at && ` · أُلغي في ${new Date(s.superseded_at).toLocaleString('ar')}`}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </div>
      )}

      {/* Sync error / fix-connection banner */}
      {status?.sync_error && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-4 flex items-start gap-3">
          <AlertCircle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-amber-800">فشلت المزامنة الأخيرة</p>
            <p className="text-xs text-amber-700 mt-0.5 font-mono break-all">{status.sync_error}</p>
            {(status.sync_error.includes('invalid_grant') || status.sync_error.includes('revoked')) && (
              <div className="mt-2 space-y-1.5 text-xs text-amber-700">
                <p className="font-semibold text-amber-800">رمز التحديث (refresh_token) انتهت صلاحيته أو أُلغي من سلة.</p>
                <p>الحل الأسرع: أدخل <span className="font-bold">Account Token</span> من شركاء سلة:</p>
                <ol className="list-decimal list-inside space-y-0.5 text-amber-600 ms-1">
                  <li>اذهب إلى <span className="font-mono font-semibold">partners.salla.sa</span> → API credentials</li>
                  <li>اضغط «Reveal token once» ← انسخ <span className="font-semibold">Account token</span></li>
                  <li>الصقه في حقل «مفتاح API» أدناه واضغط «حفظ الإعدادات»</li>
                </ol>
                <p className="text-amber-500">أو اضغط «استعادة الاتصال» لإعادة تفعيل المفتاح الحالي إن كان لا يزال صالحاً.</p>
              </div>
            )}
          </div>
          <button
            onClick={handleFixConnection}
            disabled={oauthLoading}
            className="shrink-0 rounded-lg bg-amber-600 px-4 py-2 text-xs font-bold text-white hover:bg-amber-500 transition-colors disabled:opacity-60 flex items-center gap-1.5"
          >
            {oauthLoading ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            استعادة الاتصال
          </button>
        </div>
      )}

      {/* Easy Mode reinstall instructions */}
      {easyReinstall && (
        <div className="rounded-xl border border-blue-200 bg-blue-50 p-4 space-y-3">
          <div className="flex items-start gap-3">
            <Plug className="w-5 h-5 text-blue-500 shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-blue-900">إعادة الربط عبر سلة (Easy Mode)</p>
              <p className="text-xs text-blue-700 mt-1 leading-relaxed">{easyReinstall.message}</p>
            </div>
            <button
              onClick={() => setEasyReinstall(null)}
              className="shrink-0 text-blue-400 hover:text-blue-600 text-xs"
            >
              إغلاق
            </button>
          </div>
          {easyReinstall.steps.length > 0 && (
            <ol className="list-decimal list-inside space-y-1 text-xs text-blue-800 ms-1">
              {easyReinstall.steps.map((s, i) => <li key={i}>{s}</li>)}
            </ol>
          )}
          {easyReinstall.note && (
            <p className="text-[11px] text-blue-600 leading-relaxed">{easyReinstall.note}</p>
          )}
          <a
            href={easyReinstall.salla_apps_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-xs font-bold text-white hover:bg-blue-500 transition-colors"
          >
            فتح «تطبيقاتي» في سلة
          </a>
        </div>
      )}

      {/* Settings Form */}
      <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 space-y-5">
        <h2 className="font-semibold text-slate-900 text-sm">إعدادات الربط</h2>

        {/* Platform */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-slate-700">المنصة</label>
          <select
            value={platform}
            onChange={e => setPlatform(e.target.value)}
            className="w-full rounded-lg border border-slate-300 bg-slate-50 px-3 py-2.5 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          >
            <option value="salla">سلة (Salla)</option>
          </select>
        </div>

        {/* API Key */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-slate-700">
            مفتاح API
            <span className="text-slate-400 font-normal me-1"> (Account Token من Salla Partners)</span>
          </label>
          <div className="relative">
            <input
              type={showApiKey ? 'text' : 'password'}
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              placeholder={status?.api_key_hint ? `الحالي: ${status.api_key_hint} · اترك فارغاً للإبقاء عليه` : 'أدخل مفتاح API...'}
              className="w-full rounded-lg border border-slate-300 bg-slate-50 px-3 py-2.5 pe-10 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
            />
            <button
              type="button"
              onClick={() => setShowApiKey(v => !v)}
              className="absolute end-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
            >
              {showApiKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
          </div>
        </div>

        {/* Store ID */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-slate-700">معرّف المتجر (Store ID)</label>
          <input
            type="text"
            value={storeId}
            onChange={e => setStoreId(e.target.value)}
            placeholder="مثال: 12345"
            className="w-full rounded-lg border border-slate-300 bg-slate-50 px-3 py-2.5 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
        </div>

        {/* Webhook Secret */}
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-slate-700">
            Webhook Secret
            <span className="text-slate-400 font-normal me-1"> (اختياري)</span>
          </label>
          <input
            type="password"
            value={webhookSecret}
            onChange={e => setWebhookSecret(e.target.value)}
            placeholder="سر التحقق من الويب هوك..."
            className="w-full rounded-lg border border-slate-300 bg-slate-50 px-3 py-2.5 text-sm text-slate-900 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent"
          />
        </div>

        {/* Enabled Toggle */}
        <div className="flex items-center justify-between pt-1">
          <div>
            <p className="text-sm font-medium text-slate-900">تفعيل الربط</p>
            <p className="text-xs text-slate-500 mt-0.5">عند التعطيل، يعود وكيل المبيعات إلى قاعدة البيانات المحلية</p>
          </div>
          <button
            type="button"
            onClick={() => setEnabled(v => !v)}
            className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${enabled ? 'bg-brand-500' : 'bg-slate-300'}`}
          >
            <span
              className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-6' : 'translate-x-1'}`}
            />
          </button>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-3 pt-2 border-t border-slate-100">
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-brand-500 text-white text-sm font-medium hover:bg-brand-600 transition-colors disabled:opacity-60"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
            حفظ الإعدادات
          </button>

          <button
            onClick={handleTest}
            disabled={testing || (!status?.configured && !apiKey)}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg border border-slate-300 text-slate-700 text-sm font-medium hover:bg-slate-50 transition-colors disabled:opacity-50"
          >
            {testing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
            اختبار الاتصال
          </button>

          {saveSuccess && (
            <span className="flex items-center gap-1.5 text-emerald-600 text-sm font-medium">
              <CheckCircle className="w-4 h-4" />
              تم الحفظ
            </span>
          )}
        </div>
      </div>

      {/* Test Result */}
      {testResult && (
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
              : <XCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
            }
            <div>
              {testResult.status === 'ok' && (
                <>
                  <p className="font-semibold text-emerald-800 text-sm">الاتصال ناجح</p>
                  <p className="text-emerald-700 text-xs mt-1">
                    المنصة: {testResult.platform} · المنتجات: {testResult.products_found}
                  </p>
                  {testResult.sample && (
                    <p className="text-emerald-700 text-xs mt-1">
                      أول منتج: {String((testResult.sample as Record<string, unknown>)['title'] ?? '')}
                    </p>
                  )}
                </>
              )}
              {testResult.status === 'not_configured' && (
                <>
                  <p className="font-semibold text-amber-800 text-sm">غير مهيأ</p>
                  <p className="text-amber-700 text-xs mt-1">احفظ بيانات الاعتماد أولاً ثم اختبر الاتصال.</p>
                </>
              )}
              {testResult.status === 'error' && (
                <>
                  <p className="font-semibold text-red-800 text-sm">فشل الاتصال</p>
                  <p className="text-red-700 text-xs mt-1 font-mono break-all">{testResult.error}</p>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      {/* OAuth result banner */}
      {oauthMessage && (
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
      )}

      {/* OAuth Quick Connect */}
      <div className="bg-white rounded-xl shadow-sm border border-slate-200 p-6 space-y-4">
        <div>
          <h2 className="font-semibold text-slate-900 text-sm">ربط سريع عبر OAuth</h2>
          <p className="text-slate-500 text-xs mt-1">
            انقر على الزر أدناه لتفويض نحلة AI مباشرة من حساب سلة بدون نسخ مفاتيح يدوياً.
          </p>
        </div>
        <button
          onClick={handleOAuthConnect}
          disabled={oauthLoading}
          className="flex items-center gap-2.5 px-5 py-3 rounded-xl bg-[#1d2939] text-white text-sm font-semibold hover:bg-[#101828] transition-colors disabled:opacity-60 w-full justify-center"
        >
          {oauthLoading
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : <Store className="w-4 h-4" />}
          ربط المتجر عبر سلة (OAuth)
        </button>
      </div>

      {/* Store Knowledge Sync */}
      <StoreSyncPanel isStoreConnected={!!isConfigured} />

      {/* Info Box */}
      <div className="bg-blue-50 border border-blue-200 rounded-xl p-5">
        <div className="flex items-start gap-3">
          <Store className="w-5 h-5 text-blue-500 shrink-0 mt-0.5" />
          <div className="space-y-2 text-xs text-blue-700">
            <p className="font-semibold text-blue-900 text-sm">كيف تحصل على Account Token من Salla Partners؟</p>
            <div className="space-y-1">
              <p className="font-medium text-blue-800">الطريقة الموصى بها (Account Token — لا تنتهي صلاحيته):</p>
              <p>1. اذهب إلى <span className="font-mono font-bold">partners.salla.sa</span> وسجّل دخولك</p>
              <p>2. من القائمة اختر <span className="font-semibold">API credentials</span></p>
              <p>3. اضغط <span className="font-semibold">Reveal token once</span> ثم انسخ <span className="font-semibold">Account token</span></p>
              <p>4. الصقه في حقل «مفتاح API» أعلاه واضغط «حفظ الإعدادات»</p>
            </div>
            <div className="border-t border-blue-200 pt-2 space-y-1 text-blue-600">
              <p className="font-medium text-blue-700">بديل: استخدام OAuth (يحتاج تجديداً دورياً):</p>
              <p>اضغط «ربط المتجر عبر سلة (OAuth)» أعلاه — يصلح فقط عندما يكون التطبيق معتمداً في سلة.</p>
            </div>
          </div>
        </div>
      </div>

    </div>
  )
}
