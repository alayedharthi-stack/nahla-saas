import { useEffect, useMemo, useState } from 'react'
import {
  Search, Store, ToggleLeft, ToggleRight,
  X, Hash, Calendar, Wifi, WifiOff, Phone, Copy,
  CheckCircle2, AlertCircle, Clock, Unplug,
} from 'lucide-react'
import { adminApi, type AdminTenantSummary } from '../api/admin'

// ── helpers ───────────────────────────────────────────────────────────────────

const SUB_STATUS_AR: Record<string, string> = {
  active: 'نشط', trialing: 'تجربة', canceled: 'ملغى',
  past_due: 'متأخر', none: 'بلا باقة', incomplete: 'غير مكتمل',
}
const WA_STATUS_AR: Record<string, { label: string; cls: string }> = {
  connected:          { label: 'مربوط',          cls: 'bg-green-100 text-green-700'   },
  not_connected:      { label: 'غير مربوط',       cls: 'bg-slate-100 text-slate-400'  },
  pending:            { label: 'معلق',            cls: 'bg-yellow-100 text-yellow-700' },
  disconnected:       { label: 'مقطوع',           cls: 'bg-red-100 text-red-600'      },
  request_submitted:  { label: 'طلب إرسال',       cls: 'bg-blue-100 text-blue-600'    },
  pending_activation: { label: 'ينتظر التفعيل',   cls: 'bg-violet-100 text-violet-600'},
  action_required:    { label: 'يحتاج إجراء',     cls: 'bg-orange-100 text-orange-600'},
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  return d.toLocaleDateString('ar-SA', { year: 'numeric', month: 'short', day: 'numeric' })
}

function CopyBtn({ value }: { value: string | null | undefined }) {
  const [copied, setCopied] = useState(false)
  if (!value) return <span className="text-slate-300">—</span>
  const copy = () => {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <button
      onClick={copy}
      className="inline-flex items-center gap-1 font-mono text-xs text-slate-600 hover:text-slate-900 bg-slate-100 hover:bg-slate-200 rounded px-1.5 py-0.5 transition max-w-[140px] truncate"
      title={value}
    >
      {copied ? <CheckCircle2 className="w-3 h-3 text-green-500 shrink-0" /> : <Copy className="w-3 h-3 shrink-0" />}
      <span className="truncate">{value}</span>
    </button>
  )
}

function WaBadge({ status }: { status: string }) {
  const s = WA_STATUS_AR[status] ?? { label: status, cls: 'bg-slate-100 text-slate-500' }
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s.cls}`}>{s.label}</span>
}

function ActiveBadge({ active }: { active: boolean }) {
  return (
    <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${active ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
      {active ? 'نشط' : 'موقوف'}
    </span>
  )
}

// ── Detail Drawer ─────────────────────────────────────────────────────────────

function DetailRow({ label, value, mono = false }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2 border-b border-slate-50 last:border-0">
      <span className="text-xs text-slate-400 shrink-0 w-40">{label}</span>
      <span className={`text-xs text-slate-700 text-left break-all ${mono ? 'font-mono' : 'font-medium'}`}>
        {value ?? '—'}
      </span>
    </div>
  )
}

function Section({ title, icon: Icon, children }: { title: string; icon: React.ElementType; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Icon className="w-4 h-4 text-amber-500" />
        <p className="text-xs font-black text-slate-700 uppercase tracking-wide">{title}</p>
      </div>
      <div className="bg-slate-50 rounded-xl p-3">{children}</div>
    </div>
  )
}

function TenantDrawer({
  tenant,
  onClose,
  onToggle,
  toggling,
}: {
  tenant: AdminTenantSummary
  onClose: () => void
  onToggle: () => void
  toggling: boolean
}) {
  const wa = tenant.whatsapp
  const integ = tenant.integration

  return (
    <div className="fixed inset-0 z-50 flex justify-start" dir="rtl">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <aside className="relative w-full max-w-md bg-white shadow-2xl h-full overflow-y-auto flex flex-col">
        {/* Header */}
        <div className="sticky top-0 bg-white border-b border-slate-100 px-5 py-4 flex items-center justify-between z-10">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-amber-100 flex items-center justify-center">
              <Store className="w-5 h-5 text-amber-600" />
            </div>
            <div>
              <p className="font-black text-slate-800 text-sm">{tenant.name}</p>
              <p className="text-xs text-slate-400">{tenant.domain || 'بدون دومين'}</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="flex-1 px-5 py-4 space-y-5">
          {/* Store Identity */}
          <Section title="هوية المتجر" icon={Store}>
            <DetailRow label="Tenant ID" value={<CopyBtn value={String(tenant.id)} />} />
            <DetailRow label="اسم المتجر" value={tenant.name} />
            <DetailRow label="الدومين" value={tenant.domain} mono />
            <DetailRow label="تاريخ التسجيل" value={fmtDate(tenant.created_at)} />
            <DetailRow label="الحالة" value={<ActiveBadge active={tenant.is_active} />} />
            <DetailRow label="الخطة" value={tenant.subscription.plan || '—'} />
            <DetailRow label="حالة الاشتراك" value={SUB_STATUS_AR[tenant.subscription.status] ?? tenant.subscription.status} />
          </Section>

          {/* Integration Identity */}
          <Section title="هوية التكامل (سلة)" icon={Hash}>
            <DetailRow label="Integration ID" value={<CopyBtn value={integ.integration_id ? String(integ.integration_id) : null} />} />
            <DetailRow label="External Store ID" value={<CopyBtn value={integ.external_store_id} />} />
            <DetailRow label="المزود" value={integ.provider || '—'} />
            <DetailRow label="مفعّل" value={integ.enabled == null ? '—' : integ.enabled ? 'نعم' : 'لا'} />
          </Section>

          {/* WhatsApp Identity */}
          <Section title="هوية واتساب" icon={wa.status === 'connected' ? Wifi : WifiOff}>
            <DetailRow label="حالة واتساب" value={<WaBadge status={wa.status} />} />
            <DetailRow label="Phone Number" value={wa.phone_number} mono />
            <DetailRow label="Phone Number ID" value={<CopyBtn value={wa.phone_number_id} />} />
            <DetailRow label="WABA ID" value={<CopyBtn value={wa.whatsapp_business_account_id} />} />
            <DetailRow label="الاسم التجاري" value={wa.business_display_name} />
            <DetailRow label="نوع الربط" value={wa.connection_type} />
            <DetailRow label="المزود" value={wa.provider} />
            <DetailRow label="الإرسال مفعّل" value={wa.sending_enabled ? 'نعم' : 'لا'} />
            <DetailRow label="Webhook مُحقَّق" value={wa.webhook_verified ? 'نعم' : 'لا'} />
            <DetailRow label="تاريخ الربط" value={fmtDate(wa.connected_at)} />
            {wa.disconnect_reason && (
              <>
                <DetailRow label="سبب الفصل" value={wa.disconnect_reason} />
                <DetailRow label="تاريخ الفصل" value={fmtDate(wa.disconnected_at)} />
              </>
            )}
          </Section>
        </div>

        {/* Footer actions */}
        <div className="sticky bottom-0 bg-white border-t border-slate-100 px-5 py-3">
          <button
            onClick={onToggle}
            disabled={toggling}
            className={`w-full py-2.5 rounded-xl text-sm font-semibold transition flex items-center justify-center gap-2 ${
              tenant.is_active
                ? 'bg-red-50 text-red-600 hover:bg-red-100'
                : 'bg-green-50 text-green-700 hover:bg-green-100'
            }`}
          >
            {toggling
              ? <><Clock className="w-4 h-4 animate-spin" /> جارٍ التحديث...</>
              : tenant.is_active
                ? <><Unplug className="w-4 h-4" /> إيقاف المتجر</>
                : <><CheckCircle2 className="w-4 h-4" /> تفعيل المتجر</>
            }
          </button>
        </div>
      </aside>
    </div>
  )
}

// ── Main Component ────────────────────────────────────────────────────────────

export default function AdminTenants() {
  const [tenants, setTenants] = useState<AdminTenantSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [statusFilter, setStatusFilter] = useState<'' | 'active' | 'inactive'>('')
  const [waFilter, setWaFilter] = useState('')
  const [selected, setSelected] = useState<AdminTenantSummary | null>(null)
  const [togglingId, setTogglingId] = useState<number | null>(null)

  useEffect(() => {
    setLoading(true)
    adminApi.tenants({ search, status: statusFilter, limit: 200 })
      .then(data => setTenants(data.tenants))
      .catch(() => setTenants([]))
      .finally(() => setLoading(false))
  }, [search, statusFilter])

  const rows = useMemo(() => {
    if (!waFilter) return tenants
    return tenants.filter(t => t.whatsapp.status === waFilter)
  }, [tenants, waFilter])

  const toggle = async (tenant: AdminTenantSummary) => {
    setTogglingId(tenant.id)
    try {
      const updated = await adminApi.updateTenantStatus(tenant.id, !tenant.is_active)
      setTenants(prev => prev.map(r => r.id === tenant.id ? updated : r))
      if (selected?.id === tenant.id) setSelected(updated)
    } finally {
      setTogglingId(null)
    }
  }

  return (
    <div className="p-6 space-y-5" dir="rtl">
      {/* Header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-amber-500 flex items-center justify-center shadow-lg shadow-amber-500/30">
            <Store className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-lg font-black text-slate-800">المتاجر</h1>
            <p className="text-slate-400 text-xs">
              {loading ? 'جارٍ التحميل...' : `${rows.length} متجر`} — إدارة تشخيصية شاملة
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative">
            <Search className="w-4 h-4 text-slate-400 absolute right-3 top-1/2 -translate-y-1/2" />
            <input
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder="اسم، دومين..."
              className="pr-9 pl-4 py-2 text-sm border border-slate-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-amber-400 w-56"
            />
          </div>
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value as '' | 'active' | 'inactive')}
            className="px-3 py-2 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-amber-400"
          >
            <option value="">كل الحالات</option>
            <option value="active">نشط</option>
            <option value="inactive">موقوف</option>
          </select>
          <select
            value={waFilter}
            onChange={e => setWaFilter(e.target.value)}
            className="px-3 py-2 text-sm border border-slate-200 rounded-xl bg-white focus:outline-none focus:ring-2 focus:ring-amber-400"
          >
            <option value="">كل واتساب</option>
            <option value="connected">مربوط</option>
            <option value="not_connected">غير مربوط</option>
            <option value="disconnected">مقطوع</option>
            <option value="pending">معلق</option>
          </select>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-2xl border border-slate-100 shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-48">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-amber-500" />
          </div>
        ) : rows.length === 0 ? (
          <div className="text-center py-20 text-slate-400">
            <AlertCircle className="w-8 h-8 mx-auto mb-2 text-slate-300" />
            لا توجد متاجر مطابقة
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-slate-50 border-b border-slate-100">
                  {[
                    'المتجر',
                    'Tenant ID',
                    'External Store ID',
                    'تاريخ التسجيل',
                    'واتساب',
                    'Phone Number ID',
                    'WABA ID',
                    'الخطة',
                    'الحالة',
                    'إجراء',
                  ].map(h => (
                    <th key={h} className="text-right px-3 py-3 text-xs font-semibold text-slate-500 whitespace-nowrap">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-50">
                {rows.map(tenant => (
                  <tr
                    key={tenant.id}
                    onClick={() => setSelected(tenant)}
                    className="hover:bg-amber-50/40 cursor-pointer transition"
                  >
                    {/* Store */}
                    <td className="px-3 py-3 min-w-[160px]">
                      <p className="font-semibold text-slate-800 truncate max-w-[180px]">{tenant.name}</p>
                      <p className="text-xs text-slate-400 font-mono truncate max-w-[180px]">{tenant.domain || '—'}</p>
                    </td>

                    {/* Tenant ID */}
                    <td className="px-3 py-3">
                      <span className="font-mono text-xs text-slate-600 bg-slate-100 px-1.5 py-0.5 rounded">
                        #{tenant.id}
                      </span>
                    </td>

                    {/* External Store ID */}
                    <td className="px-3 py-3">
                      {tenant.integration.external_store_id
                        ? <span className="font-mono text-xs text-blue-600">{tenant.integration.external_store_id}</span>
                        : <span className="text-xs text-slate-300">—</span>
                      }
                    </td>

                    {/* Created At */}
                    <td className="px-3 py-3 whitespace-nowrap">
                      <div className="flex items-center gap-1 text-xs text-slate-500">
                        <Calendar className="w-3 h-3 shrink-0" />
                        {fmtDate(tenant.created_at)}
                      </div>
                    </td>

                    {/* WA Status */}
                    <td className="px-3 py-3">
                      <WaBadge status={tenant.whatsapp.status} />
                    </td>

                    {/* Phone Number ID */}
                    <td className="px-3 py-3">
                      {tenant.whatsapp.phone_number_id
                        ? <span className="font-mono text-xs text-slate-600 truncate block max-w-[120px]">{tenant.whatsapp.phone_number_id}</span>
                        : <span className="text-xs text-slate-300">—</span>
                      }
                    </td>

                    {/* WABA ID */}
                    <td className="px-3 py-3">
                      {tenant.whatsapp.whatsapp_business_account_id
                        ? <span className="font-mono text-xs text-slate-600 truncate block max-w-[120px]">{tenant.whatsapp.whatsapp_business_account_id}</span>
                        : <span className="text-xs text-slate-300">—</span>
                      }
                    </td>

                    {/* Plan */}
                    <td className="px-3 py-3">
                      <p className="text-xs text-slate-700">{tenant.subscription.plan || '—'}</p>
                      <p className="text-xs text-slate-400">{SUB_STATUS_AR[tenant.subscription.status] ?? tenant.subscription.status}</p>
                    </td>

                    {/* Active */}
                    <td className="px-3 py-3">
                      <ActiveBadge active={tenant.is_active} />
                    </td>

                    {/* Toggle */}
                    <td className="px-3 py-3" onClick={e => e.stopPropagation()}>
                      <button
                        onClick={() => toggle(tenant)}
                        disabled={togglingId === tenant.id}
                        className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition disabled:opacity-50"
                        title={tenant.is_active ? 'إيقاف المتجر' : 'تفعيل المتجر'}
                      >
                        {tenant.is_active
                          ? <ToggleRight className="w-4 h-4 text-green-500" />
                          : <ToggleLeft className="w-4 h-4 text-red-500" />
                        }
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Detail Drawer */}
      {selected && (
        <TenantDrawer
          tenant={selected}
          onClose={() => setSelected(null)}
          onToggle={() => toggle(selected)}
          toggling={togglingId === selected.id}
        />
      )}
    </div>
  )
}
