import { useEffect, useState } from 'react'
import { CheckCircle, XCircle, ExternalLink, RefreshCw, AlertCircle, Plug, Smartphone, Copy, Loader2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import { apiCall } from '../api/client'

// ── Types ─────────────────────────────────────────────────────────────────────

interface SallaStatus {
  connected: boolean
  store_id?: string
  store_name?: string
  last_sync?: string
  needs_reauth?: boolean
}

interface WaStatus {
  connected: boolean
  phone_number?: string
  display_name?: string
  connected_at?: string
  merchant_channel_label?: string
}

interface ZidStatus {
  connected: boolean
  store_id?: string
  store_name?: string
  connected_at?: string
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function timeAgo(isoStr?: string): string {
  if (!isoStr) return '—'
  try {
    const diff = Date.now() - new Date(isoStr).getTime()
    const mins = Math.floor(diff / 60000)
    if (mins < 1)  return 'الآن'
    if (mins < 60) return `منذ ${mins} دقيقة`
    const hrs = Math.floor(mins / 60)
    if (hrs  < 24) return `منذ ${hrs} ساعة`
    return `منذ ${Math.floor(hrs / 24)} يوم`
  } catch { return '—' }
}

// ── Integration Card ──────────────────────────────────────────────────────────

interface CardProps {
  logo: string
  name: string
  description: string
  connected: boolean
  loading: boolean
  accountLabel?: string
  accountValue?: string
  syncLabel?: string
  syncValue?: string
  onConnect?: () => void
  onReconnect?: () => void
  reconnecting?: boolean
  onDisconnect?: () => void
  externalHref?: string
  externalLabel?: string
  hideExternal?: boolean
  footerNote?: string
}

function IntegrationCard({
  logo, name, description, connected, loading,
  accountLabel, accountValue, syncLabel, syncValue,
  onConnect, onReconnect, reconnecting, onDisconnect, externalHref, externalLabel, hideExternal,
  footerNote,
}: CardProps) {
  const [syncing, setSyncing] = useState(false)

  const handleSync = () => {
    setSyncing(true)
    setTimeout(() => setSyncing(false), 2000)
  }

  return (
    <div className="card p-5">
      <div className="flex items-start gap-4">
        <div className="w-12 h-12 bg-slate-50 border border-slate-200 rounded-xl flex items-center justify-center text-2xl shrink-0">
          {logo}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-slate-900">{name}</h3>
            {loading
              ? <Badge label="جاري التحقق..." variant="slate" />
              : connected
                ? <Badge label="متصل"       variant="green" dot />
                : <Badge label="غير متصل"  variant="slate" />}
          </div>
          <p className="text-xs text-slate-500 mt-1">{description}</p>

          {!loading && connected && footerNote && (
            <p className="text-xs text-slate-600 mt-2 leading-relaxed bg-slate-50 border border-slate-100 rounded-lg px-3 py-2">
              {footerNote}
            </p>
          )}

          {!loading && connected && (
            <div className="mt-3 grid sm:grid-cols-2 gap-3">
              <div className="bg-slate-50 rounded-lg px-3 py-2.5">
                <p className="text-xs text-slate-400">{accountLabel ?? 'الحساب'}</p>
                <p className="text-xs font-medium text-slate-800 mt-0.5 truncate">{accountValue ?? '—'}</p>
              </div>
              <div className="bg-slate-50 rounded-lg px-3 py-2.5">
                <div className="flex items-center gap-1.5">
                  {syncing
                    ? <RefreshCw className="w-3.5 h-3.5 text-brand-500 animate-spin" />
                    : <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />}
                  <p className="text-xs text-slate-400">{syncLabel ?? 'آخر مزامنة'}</p>
                </div>
                <p className="text-xs font-medium text-slate-800 mt-0.5">{syncValue ?? '—'}</p>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 mt-4 pt-4 border-t border-slate-100">
        {!loading && (connected ? (
          <>
            <button onClick={handleSync} className="btn-secondary text-xs py-1.5" disabled={syncing}>
              <RefreshCw className={`w-3.5 h-3.5 ${syncing ? 'animate-spin' : ''}`} />
              {syncing ? 'جارٍ…' : 'مزامنة'}
            </button>
            {onReconnect && (
              <button
                onClick={onReconnect}
                disabled={reconnecting}
                className="btn-secondary text-xs py-1.5 text-amber-600 border-amber-200 hover:bg-amber-50"
              >
                {reconnecting
                  ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                  : <RefreshCw className="w-3.5 h-3.5" />}
                إعادة الربط
              </button>
            )}
            {onDisconnect && (
              <button onClick={onDisconnect} className="btn-ghost text-xs py-1.5 text-red-500 hover:bg-red-50">
                فصل الاتصال
              </button>
            )}
          </>
        ) : (
          <button onClick={onConnect} disabled={reconnecting} className="btn-primary text-xs py-1.5">
            {reconnecting
              ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : <Plug className="w-3.5 h-3.5" />}
            {reconnecting ? 'جارٍ الربط…' : `ربط ${name}`}
          </button>
        ))}

        {!hideExternal && externalHref && (
          <a href={externalHref} target="_blank" rel="noreferrer"
            className="btn-ghost text-xs py-1.5 ms-auto text-slate-400">
            <ExternalLink className="w-3.5 h-3.5" /> {externalLabel ?? `فتح في ${name}`}
          </a>
        )}
      </div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Integrations() {
  const navigate = useNavigate()
  const [copied,  setCopied]  = useState<string | null>(null)
  const [reconnecting, setReconnecting] = useState(false)
  const [reconnectMsg, setReconnectMsg] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [easyReinstall, setEasyReinstall] = useState<{
    message: string
    salla_apps_url: string
    steps: string[]
    note?: string
  } | null>(null)

  const [sallaStatus, setSallaStatus] = useState<SallaStatus>({ connected: false })
  const [waStatus,    setWaStatus]    = useState<WaStatus>({ connected: false })
  const [zidStatus,   setZidStatus]   = useState<ZidStatus>({ connected: false })

  const [sallaLoading, setSallaLoading] = useState(true)
  const [waLoading,    setWaLoading]    = useState(true)
  const [zidLoading,   setZidLoading]   = useState(true)

  useEffect(() => {
    // Salla
    apiCall<any>('/salla/whoami')
      .then(d => {
        const si = d?.salla_integration ?? {}
        setSallaStatus({
          connected:    si.connected === true,
          store_id:     si.store_id,
          store_name:   si.store_name,
          needs_reauth: si.needs_reauth === true,
        })
      })
      .catch(() => setSallaStatus({ connected: false }))
      .finally(() => setSallaLoading(false))

    // WhatsApp — unified single source of truth
    apiCall<any>('/whatsapp/status')
      .then(d => setWaStatus({
        connected:    d?.connected === true,
        phone_number: d?.phone_number,
        display_name: d?.display_name,
        connected_at: d?.connected_at,
        merchant_channel_label: d?.merchant_channel_label,
      }))
      .catch(() => setWaStatus({ connected: false }))
      .finally(() => setWaLoading(false))

    // Zid
    apiCall<any>('/zid/status')
      .then(d => setZidStatus({
        connected:    d?.connected === true,
        store_id:     d?.store_id,
        store_name:   d?.store_name,
        connected_at: d?.connected_at,
      }))
      .catch(() => setZidStatus({ connected: false }))
      .finally(() => setZidLoading(false))
  }, [])

  function reloadSallaStatus() {
    setSallaLoading(true)
    apiCall<any>('/salla/whoami')
      .then(d => {
        const si = d?.salla_integration ?? {}
        setSallaStatus({
          connected:    si.connected === true,
          store_id:     si.store_id,
          store_name:   si.store_name,
          needs_reauth: si.needs_reauth === true,
        })
      })
      .catch(() => {})
      .finally(() => setSallaLoading(false))
  }

  async function handleReconnectSalla() {
    setReconnecting(true)
    setReconnectMsg(null)
    setEasyReinstall(null)
    try {
      const data = await apiCall<{
        action: string
        url?: string
        message: string
        note?: string
        salla_apps_url?: string
        steps?: string[]
      }>('/api/salla/reconnect', { method: 'POST' })

      if (data.action === 'refreshed' || data.action === 'reactivated') {
        setReconnectMsg({ type: 'success', text: data.message })
        reloadSallaStatus()
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
      setReconnectMsg({ type: 'error', text: 'تعذّر إعادة الربط — تحقق من الاتصال وحاول مجدداً' })
    } finally {
      setReconnecting(false)
    }
  }

  const handleCopy = (url: string, label: string) => {
    navigator.clipboard.writeText(url)
    setCopied(label)
    setTimeout(() => setCopied(null), 1500)
  }

  const connectedCount = [sallaStatus, waStatus, zidStatus].filter(s => s.connected).length
  const loadingAny     = sallaLoading || waLoading || zidLoading

  return (
    <div className="space-y-5">
      {/* Summary bar */}
      <div className="grid grid-cols-3 gap-4">
        <div className="card px-5 py-4 flex items-center gap-3">
          <CheckCircle className="w-5 h-5 text-emerald-500 shrink-0" />
          <div>
            <p className="text-xs text-slate-400">متصل</p>
            <p className="text-sm font-bold text-slate-900">
              {loadingAny
                ? <Loader2 className="w-4 h-4 animate-spin inline" />
                : `${connectedCount} / 3`}
            </p>
          </div>
        </div>
        <div className="card px-5 py-4 flex items-center gap-3">
          <RefreshCw className="w-5 h-5 text-brand-500 shrink-0" />
          <div>
            <p className="text-xs text-slate-400">واتساب</p>
            <p className="text-sm font-bold text-slate-900">
              {waLoading ? '...' : waStatus.connected ? 'مرتبط ✓' : 'غير مرتبط'}
            </p>
            {!waLoading && waStatus.merchant_channel_label && (
              <p className="text-xs text-slate-500 mt-0.5">{waStatus.merchant_channel_label}</p>
            )}
          </div>
        </div>
        <div className="card px-5 py-4 flex items-center gap-3">
          <Smartphone className="w-5 h-5 text-blue-500 shrink-0" />
          <div>
            <p className="text-xs text-slate-400">رقم واتساب</p>
            <p className="text-sm font-bold text-slate-900" dir="ltr">
              {waLoading ? '...' : waStatus.phone_number ? `+${waStatus.phone_number}` : '—'}
            </p>
          </div>
        </div>
      </div>

      {/* Reconnect feedback */}
      {reconnectMsg && (
        <div className={`rounded-xl border px-5 py-3 flex items-center gap-3 ${
          reconnectMsg.type === 'success'
            ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
            : 'border-red-200 bg-red-50 text-red-800'
        }`}>
          {reconnectMsg.type === 'success'
            ? <CheckCircle className="w-4 h-4 shrink-0" />
            : <XCircle className="w-4 h-4 shrink-0" />}
          <p className="text-sm font-medium">{reconnectMsg.text}</p>
          <button onClick={() => setReconnectMsg(null)} className="mr-auto text-xs opacity-60 hover:opacity-100">✕</button>
        </div>
      )}

      {/* Easy Mode reinstall instructions */}
      {easyReinstall && (
        <div className="rounded-xl border border-blue-200 bg-blue-50 px-5 py-4 space-y-3">
          <div className="flex items-start gap-3">
            <RefreshCw className="w-5 h-5 text-blue-500 shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-blue-900">إعادة الربط عبر سلة (Easy Mode)</p>
              <p className="text-xs text-blue-700 mt-1 leading-relaxed">{easyReinstall.message}</p>
            </div>
            <button onClick={() => setEasyReinstall(null)} className="text-xs text-blue-400 hover:text-blue-700">✕</button>
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

      {/* Salla needs-reauth urgent banner */}
      {!sallaLoading && sallaStatus.needs_reauth && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-5 py-4 flex items-start gap-3">
          <XCircle className="w-5 h-5 text-red-500 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-red-800">انتهت صلاحية ربط سلة — مطلوب إعادة الربط</p>
            <p className="text-xs text-red-600 mt-0.5">
              انتهت صلاحية توكن سلة. سيحاول النظام تجديده تلقائياً، أو يمكنك إعادة الربط يدوياً من سلة.
            </p>
          </div>
          <div className="flex gap-2 shrink-0">
            <button
              onClick={handleReconnectSalla}
              disabled={reconnecting}
              className="rounded-lg bg-amber-600 px-4 py-2 text-xs font-bold text-white hover:bg-amber-500 transition-colors disabled:opacity-60 flex items-center gap-1.5"
            >
              {reconnecting
                ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
                : <RefreshCw className="w-3.5 h-3.5" />}
              إعادة الربط
            </button>
            <a
              href="https://s.salla.sa/apps/nahla"
              target="_blank"
              rel="noreferrer"
              className="rounded-lg bg-red-600 px-4 py-2 text-xs font-bold text-white hover:bg-red-500 transition-colors"
            >
              فتح سلة
            </a>
          </div>
        </div>
      )}

      {/* Integration cards */}
      <div className="grid lg:grid-cols-2 gap-4">
        <IntegrationCard
          logo="🛒"
          name="Salla"
          description="اربط متجرك على سلة لمزامنة المنتجات والطلبات والعملاء في الوقت الفعلي."
          connected={sallaStatus.connected}
          loading={sallaLoading}
          accountLabel="اسم المتجر"
          accountValue={sallaStatus.store_name && sallaStatus.store_name !== 'not_connected' ? sallaStatus.store_name : sallaStatus.store_id}
          syncLabel="رقم المتجر"
          syncValue={sallaStatus.store_id && sallaStatus.store_id !== 'not_connected' ? sallaStatus.store_id : '—'}
          onConnect={handleReconnectSalla}
          onReconnect={handleReconnectSalla}
          reconnecting={reconnecting}
          externalHref="https://salla.sa/dashboard"
          externalLabel="فتح في Salla"
        />

        <IntegrationCard
          logo="🏪"
          name="Zid"
          description="اربط متجرك على زد لتفعيل التجارة عبر واتساب ومساعد الذكاء الاصطناعي."
          connected={zidStatus.connected}
          loading={zidLoading}
          accountLabel="اسم المتجر"
          accountValue={zidStatus.store_name ?? zidStatus.store_id}
          syncLabel="تاريخ الربط"
          syncValue={timeAgo(zidStatus.connected_at)}
          onConnect={() => navigate('/store-integration')}
          externalHref="https://web.zid.sa/dashboard"
          externalLabel="فتح في Zid"
        />

        <IntegrationCard
          logo="💬"
          name="WhatsApp Business API"
          description="اربط رقم واتساب للأعمال لاستقبال الرسائل والرد عليها."
          connected={waStatus.connected}
          loading={waLoading}
          accountLabel="الرقم المرتبط"
          accountValue={waStatus.phone_number ? `+${waStatus.phone_number}` : '—'}
          syncLabel="تاريخ الربط"
          syncValue={timeAgo(waStatus.connected_at)}
          onConnect={() => navigate('/whatsapp-connect')}
          onDisconnect={() => navigate('/whatsapp-connect')}
          hideExternal
          footerNote={
            waStatus.connected
              ? 'سيبدأ الذكاء بالرد على الرسائل الجديدة فقط من لحظة تفعيل الربط، ولن يرد على رسائل أرشيف واتساب التي سبقت ذلك.'
              : undefined
          }
        />
      </div>

      {/* Webhook info */}
      <div className="card p-5">
        <div className="flex items-start gap-3">
          <AlertCircle className="w-5 h-5 text-brand-500 shrink-0 mt-0.5" />
          <div className="flex-1 min-w-0">
            <h3 className="text-sm font-semibold text-slate-900">نقاط اتصال Webhook</h3>
            <p className="text-xs text-slate-500 mt-0.5 mb-3">
              أدخل هذه الروابط في لوحة مطوري سلة / زد لاستقبال الأحداث الفورية.
            </p>
            <div className="space-y-2">
              {[
                { label: 'Salla Webhooks',           url: 'https://api.nahlah.ai/integrations/salla/webhooks/{products|orders|customers}' },
                { label: 'Zid Webhooks',             url: 'https://api.nahlah.ai/integrations/zid/webhooks/{products|orders|customers}' },
                { label: 'WhatsApp Webhook (Meta)',  url: 'https://api.nahlah.ai/webhook/whatsapp' },
                { label: 'WhatsApp Channel (360dialog)',     url: 'https://api.nahlah.ai/webhook/whatsapp/360dialog' },
                { label: 'WhatsApp Coexistence (360dialog)', url: 'https://api.nahlah.ai/webhook/whatsapp/360dialog/coexistence' },
                { label: 'WhatsApp Status (360dialog)',      url: 'https://api.nahlah.ai/webhook/whatsapp/360dialog/status' },
              ].map(({ label, url }) => (
                <div key={label} className="flex items-center gap-3 bg-slate-50 rounded-lg px-3 py-2">
                  <span className="text-xs font-medium text-slate-500 w-36 shrink-0">{label}</span>
                  <code className="text-xs text-brand-700 font-mono truncate flex-1" dir="ltr">{url}</code>
                  <button
                    onClick={() => handleCopy(url, label)}
                    className="text-xs text-slate-400 hover:text-slate-600 shrink-0 flex items-center gap-1"
                  >
                    {copied === label
                      ? <CheckCircle className="w-3.5 h-3.5 text-emerald-500" />
                      : <Copy className="w-3.5 h-3.5" />}
                    {copied === label ? 'تم!' : 'نسخ'}
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
