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
  onDisconnect?: () => void
  externalHref?: string
  externalLabel?: string
  hideExternal?: boolean
}

function IntegrationCard({
  logo, name, description, connected, loading,
  accountLabel, accountValue, syncLabel, syncValue,
  onConnect, onDisconnect, externalHref, externalLabel, hideExternal,
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
            {onDisconnect && (
              <button onClick={onDisconnect} className="btn-ghost text-xs py-1.5 text-red-500 hover:bg-red-50">
                فصل الاتصال
              </button>
            )}
          </>
        ) : (
          <button onClick={onConnect} className="btn-primary text-xs py-1.5">
            <Plug className="w-3.5 h-3.5" /> ربط {name}
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
          connected: si.store_id && si.store_id !== 'not_connected' && si.store_id !== '?',
          store_id:   si.store_id,
          store_name: si.store_name,
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
          onConnect={() => navigate('/store-integration')}
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
                { label: 'Salla Webhooks',   url: 'https://api.nahlah.ai/integrations/salla/webhooks/{products|orders|customers}' },
                { label: 'Zid Webhooks',     url: 'https://api.nahlah.ai/integrations/zid/webhooks/{products|orders|customers}' },
                { label: 'WhatsApp Webhook', url: 'https://api.nahlah.ai/webhook/whatsapp' },
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
