import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Search,
  UserPlus,
  RefreshCw,
  Users,
  Crown,
  AlertTriangle,
  ShoppingCart,
  X,
  Phone,
  Mail,
  User,
  Upload,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  customersApi,
  type CustomerRecord,
  type CustomerSegmentMeta,
} from '../api/customers'

function segmentVariant(
  seg: string,
): 'green' | 'amber' | 'red' | 'blue' | 'slate' {
  if (seg === 'lead') return 'blue'
  if (seg === 'active') return 'green'
  if (seg === 'vip') return 'amber'
  if (seg === 'at_risk') return 'red'
  if (seg === 'inactive') return 'slate'
  return 'blue'
}

function rfmVariant(score: number): 'green' | 'amber' | 'red' | 'blue' | 'slate' {
  if (score >= 12) return 'green'
  if (score >= 8) return 'amber'
  if (score >= 4) return 'blue'
  return 'slate'
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return new Date(dateStr).toLocaleDateString('ar-SA', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
    })
  } catch {
    return '—'
  }
}

export default function Customers() {
  useLanguage()
  const navigate = useNavigate()

  const [customers, setCustomers] = useState<CustomerRecord[]>([])
  const [metrics, setMetrics] = useState<{
    totalCustomers: number
    activeCustomers: number
    vipCustomers: number
    newCustomers: number
    atRiskCustomers: number
    inactiveCustomers: number
    leads: number
  } | null>(null)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [search, setSearch] = useState('')
  const [segmentKey, setSegmentKey] = useState<string>('all')
  const [segments, setSegments] = useState<CustomerSegmentMeta[]>([])
  const [segmentsLoading, setSegmentsLoading] = useState(true)
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [addName, setAddName] = useState('')
  const [addPhone, setAddPhone] = useState('')
  const [addEmail, setAddEmail] = useState('')
  const [addError, setAddError] = useState('')
  const [addLoading, setAddLoading] = useState(false)
  const [selectedCustomer, setSelectedCustomer] =
    useState<CustomerRecord | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [res, metricsRes] = await Promise.all([
        customersApi.list(search, page, 50, segmentKey),
        customersApi.metrics(),
      ])
      setCustomers(res.customers)
      setTotal(res.total)
      setPages(res.pages)
      setMetrics(metricsRes)
    } catch {
      setCustomers([])
      setMetrics(null)
    } finally {
      setLoading(false)
    }
  }, [search, page, segmentKey])

  const loadSegments = useCallback(async () => {
    setSegmentsLoading(true)
    try {
      const res = await customersApi.segments()
      setSegments(res.segments)
    } catch {
      setSegments([])
    } finally {
      setSegmentsLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  useEffect(() => {
    loadSegments()
  }, [loadSegments])

  useEffect(() => {
    setPage(1)
  }, [search, segmentKey])

  const handleAdd = async () => {
    if (!addName.trim() || !addPhone.trim()) {
      setAddError('الاسم ورقم الواتساب مطلوبان')
      return
    }
    setAddLoading(true)
    setAddError('')
    try {
      await customersApi.create({
        name: addName.trim(),
        phone: addPhone.trim(),
        email: addEmail.trim() || undefined,
      })
      setShowAdd(false)
      setAddName('')
      setAddPhone('')
      setAddEmail('')
      load()
    } catch (err: any) {
      const msg =
        err?.detail || err?.message || 'حدث خطأ أثناء إضافة العميل'
      setAddError(typeof msg === 'string' ? msg : JSON.stringify(msg))
    } finally {
      setAddLoading(false)
    }
  }

  const handleDelete = async (id: number) => {
    if (!confirm('هل أنت متأكد من حذف هذا العميل؟')) return
    try {
      await customersApi.delete(id)
      setSelectedCustomer(null)
      load()
    } catch {
      alert('حدث خطأ أثناء الحذف')
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="العملاء"
        subtitle="إدارة وتصنيف العملاء"
        action={
          <div className="flex items-center gap-2">
            <button
              onClick={() => navigate('/customers/import')}
              className="btn-secondary text-sm flex items-center gap-2"
            >
              <Upload className="w-4 h-4" />
              استيراد العملاء
            </button>
            <button
              onClick={() => setShowAdd(true)}
              className="btn-primary text-sm flex items-center gap-2"
            >
              <UserPlus className="w-4 h-4" />
              إضافة عميل
            </button>
          </div>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="إجمالي العملاء"
          value={String(metrics?.totalCustomers ?? total)}
          change={0}
          icon={Users}
          iconColor="text-brand-600"
          iconBg="bg-brand-50"
        />
        <StatCard
          label="عملاء VIP"
          value={String(metrics?.vipCustomers ?? 0)}
          change={0}
          icon={Crown}
          iconColor="text-amber-600"
          iconBg="bg-amber-50"
        />
        <StatCard
          label="في خطر المغادرة"
          value={String((metrics?.atRiskCustomers ?? 0) + (metrics?.inactiveCustomers ?? 0))}
          change={0}
          icon={AlertTriangle}
          iconColor="text-red-600"
          iconBg="bg-red-50"
        />
        <StatCard
          label="عملاء نشطون"
          value={String(metrics?.activeCustomers ?? 0)}
          change={0}
          icon={ShoppingCart}
          iconColor="text-emerald-600"
          iconBg="bg-emerald-50"
        />
      </div>

      {/* Nahla segment chips — same registry as the campaign wizard so a
          merchant who counts "12 VIPs" here sees exactly 12 reachable
          (or near-reachable) candidates when they open a campaign. */}
      <SegmentChips
        segments={segments}
        loading={segmentsLoading}
        active={segmentKey}
        onSelect={setSegmentKey}
      />

      {/* Search + Refresh */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-sm">
          <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="بحث بالاسم أو رقم الهاتف..."
            className="w-full ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
          />
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="btn-secondary text-sm flex items-center gap-2"
        >
          <RefreshCw
            className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`}
          />
          تحديث
        </button>
      </div>

      {/* Table */}
      <div className="card overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-16">
            <div className="w-8 h-8 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
          </div>
        ) : customers.length === 0 ? (
          <div className="text-center py-16 text-sm text-slate-400">
            لا يوجد عملاء
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50">
                  <th className="text-start px-5 py-3 font-medium text-slate-500">
                    الاسم
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    الهاتف
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    البريد
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    الحالة
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    RFM
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    القطاع الذكي
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    الطلبات
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    الإنفاق
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    آخر طلب
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">
                    المصدر
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {customers.map((c) => (
                  <tr
                    key={c.id}
                    className="hover:bg-slate-50 transition-colors cursor-pointer"
                    onClick={() => setSelectedCustomer(c)}
                  >
                    <td className="px-5 py-3">
                      <span className="font-medium text-slate-900">
                        {c.name || '—'}
                      </span>
                    </td>
                    <td
                      dir="ltr"
                      className="px-3 py-3 text-slate-600 font-mono"
                    >
                      {c.phone || '—'}
                    </td>
                    <td className="px-3 py-3 text-slate-500">
                      {c.email || '—'}
                    </td>
                    <td className="px-3 py-3">
                      <Badge
                        label={c.status_label}
                        variant={segmentVariant(c.status)}
                      />
                    </td>
                    <td className="px-3 py-3">
                      <Badge
                        label={String(c.rfm_scores?.total ?? c.rfm_total_score ?? 0)}
                        variant={rfmVariant(c.rfm_scores?.total ?? c.rfm_total_score ?? 0)}
                      />
                    </td>
                    <td className="px-3 py-3 text-slate-600 whitespace-nowrap">
                      {c.rfm_segment_label || '—'}
                    </td>
                    <td className="px-3 py-3 text-slate-700 font-semibold">
                      {c.orders_count ?? c.total_orders}
                    </td>
                    <td className="px-3 py-3 text-slate-700 whitespace-nowrap">
                      {(c.total_spent ?? c.total_spend).toLocaleString('ar-SA')} ر.س
                    </td>
                    <td className="px-3 py-3 text-slate-500 whitespace-nowrap">
                      {formatDate(c.last_order_date ?? c.last_order_at)}
                    </td>
                    <td className="px-3 py-3">
                      {c.source === 'manual' ? (
                        <span className="text-xs text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">
                          {c.source_label}
                        </span>
                      ) : (
                        <span className="text-xs text-slate-500">
                          {c.source_label}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* Pagination */}
        {pages > 1 && (
          <div className="flex items-center justify-between px-5 py-3 border-t border-slate-100">
            <span className="text-xs text-slate-500">
              صفحة {page} من {pages} ({total} عميل)
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage((p) => p - 1)}
                className="btn-secondary text-xs py-1 px-3 disabled:opacity-40"
              >
                السابق
              </button>
              <button
                disabled={page >= pages}
                onClick={() => setPage((p) => p + 1)}
                className="btn-secondary text-xs py-1 px-3 disabled:opacity-40"
              >
                التالي
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Add Customer Modal */}
      {showAdd && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md p-6 space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                إضافة عميل جديد
              </h3>
              <button
                onClick={() => {
                  setShowAdd(false)
                  setAddError('')
                }}
                className="text-slate-400 hover:text-slate-600"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <User className="w-3.5 h-3.5 inline me-1" />
                  الاسم *
                </label>
                <input
                  type="text"
                  value={addName}
                  onChange={(e) => setAddName(e.target.value)}
                  placeholder="اسم العميل"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <Phone className="w-3.5 h-3.5 inline me-1" />
                  رقم الواتساب *
                </label>
                <input
                  dir="ltr"
                  type="tel"
                  value={addPhone}
                  onChange={(e) => setAddPhone(e.target.value)}
                  placeholder="+966 5XXXXXXXX"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-slate-600 mb-1">
                  <Mail className="w-3.5 h-3.5 inline me-1" />
                  البريد الإلكتروني
                </label>
                <input
                  dir="ltr"
                  type="email"
                  value={addEmail}
                  onChange={(e) => setAddEmail(e.target.value)}
                  placeholder="email@example.com"
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
                />
              </div>
            </div>

            {addError && (
              <p className="text-xs text-red-600 bg-red-50 rounded-lg px-3 py-2">
                {addError}
              </p>
            )}

            <div className="flex gap-2 justify-end pt-2">
              <button
                onClick={() => {
                  setShowAdd(false)
                  setAddError('')
                }}
                className="btn-secondary text-sm"
              >
                إلغاء
              </button>
              <button
                onClick={handleAdd}
                disabled={addLoading}
                className="btn-primary text-sm flex items-center gap-2"
              >
                {addLoading && (
                  <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                )}
                إضافة
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Customer Detail Side Panel */}
      {selectedCustomer && (
        <div className="fixed inset-0 bg-black/40 flex justify-end z-50">
          <div
            className="absolute inset-0"
            onClick={() => setSelectedCustomer(null)}
          />
          <div className="relative bg-white w-full max-w-sm shadow-xl overflow-y-auto">
            <div className="sticky top-0 bg-white border-b border-slate-100 px-5 py-4 flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-900">
                تفاصيل العميل
              </h3>
              <button
                onClick={() => setSelectedCustomer(null)}
                className="text-slate-400 hover:text-slate-600"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-5 space-y-5">
              <div className="text-center space-y-2">
                <div className="w-16 h-16 bg-brand-50 rounded-full flex items-center justify-center mx-auto">
                  <User className="w-8 h-8 text-brand-500" />
                </div>
                <h4 className="text-base font-semibold text-slate-900">
                  {selectedCustomer.name}
                </h4>
                <Badge
                  label={selectedCustomer.status_label}
                  variant={segmentVariant(selectedCustomer.status)}
                />
                {selectedCustomer.source === 'manual' && (
                  <span className="block text-xs text-blue-600">
                    {selectedCustomer.source_label}
                  </span>
                )}
              </div>

              <div className="space-y-3">
                <div className="flex items-center gap-3 text-sm">
                  <Phone className="w-4 h-4 text-slate-400" />
                  <span dir="ltr" className="font-mono text-slate-700">
                    {selectedCustomer.phone || '—'}
                  </span>
                </div>
                <div className="flex items-center gap-3 text-sm">
                  <Mail className="w-4 h-4 text-slate-400" />
                  <span className="text-slate-700">
                    {selectedCustomer.email || '—'}
                  </span>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {selectedCustomer.orders_count ?? selectedCustomer.total_orders}
                  </p>
                  <p className="text-xs text-slate-500">الطلبات</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {(selectedCustomer.total_spent ?? selectedCustomer.total_spend).toLocaleString('ar-SA')}
                  </p>
                  <p className="text-xs text-slate-500">الإنفاق (ر.س)</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {(selectedCustomer.avg_order_value ?? selectedCustomer.average_order_value).toLocaleString(
                      'ar-SA',
                    )}
                  </p>
                  <p className="text-xs text-slate-500">
                    متوسط الطلب (ر.س)
                  </p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-lg font-bold text-slate-900">
                    {Math.round(selectedCustomer.churn_risk_score * 100)}%
                  </p>
                  <p className="text-xs text-slate-500">خطر المغادرة</p>
                </div>
              </div>

              <div className="space-y-2 text-xs">
                <div className="flex justify-between">
                  <span className="text-slate-500">قطاع RFM</span>
                  <span className="text-slate-700">
                    {selectedCustomer.rfm_segment_label || '—'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">درجة RFM</span>
                  <span className="text-slate-700 font-mono">
                    {selectedCustomer.rfm_scores?.code || selectedCustomer.rfm_code || '000'}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">أول طلب</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.first_order_date ?? selectedCustomer.first_order_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">آخر طلب</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.last_order_date ?? selectedCustomer.last_order_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">أول ظهور</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.first_seen_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">آخر إعادة حساب</span>
                  <span className="text-slate-700">
                    {formatDate(selectedCustomer.metrics_computed_at)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-500">عميل متكرر</span>
                  <span className="text-slate-700">
                    {selectedCustomer.is_returning ? 'نعم' : 'لا'}
                  </span>
                </div>
              </div>

              <button
                onClick={() => handleDelete(selectedCustomer.id)}
                className="w-full text-xs text-red-600 hover:text-red-700 hover:bg-red-50 rounded-lg py-2 transition-colors"
              >
                حذف العميل
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Sub-components ────────────────────────────────────────────────────────────

/**
 * Horizontal scrollable row of Nahla segment chips.
 *
 * Reuses the SAME registry as the campaign wizard
 * (`/customers/segments` + `/campaigns/wizard/segments`) so the chip
 * label "VIP عملاء" + count here always equals what the wizard's
 * Step 2 shows for the same tenant.
 *
 * The icon string from the backend is intentionally NOT rendered as an
 * arbitrary lucide-react component to avoid bundling the entire icon
 * library — we use a small, fixed visual treatment per chip instead.
 */
interface SegmentChipsProps {
  segments: CustomerSegmentMeta[]
  loading: boolean
  active: string
  onSelect: (key: string) => void
}

function SegmentChips({ segments, loading, active, onSelect }: SegmentChipsProps) {
  if (loading) {
    return (
      <div className="flex gap-2 overflow-hidden">
        {[0, 1, 2, 3, 4].map(i => (
          <div key={i} className="h-9 w-24 rounded-full bg-slate-100 animate-pulse" />
        ))}
      </div>
    )
  }
  if (!segments || segments.length === 0) return null

  return (
    <div className="flex items-center gap-2 overflow-x-auto pb-1 -mb-1">
      {segments.map(seg => {
        const isActive = active === seg.key
        return (
          <button
            key={seg.key}
            onClick={() => onSelect(seg.key)}
            title={seg.description_ar}
            className={
              'shrink-0 inline-flex items-center gap-2 px-3.5 py-1.5 rounded-full text-xs font-medium transition-colors border ' +
              (isActive
                ? 'bg-brand-500 text-white border-brand-500 shadow-sm'
                : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50 hover:border-slate-300')
            }
          >
            <span>{seg.label_ar}</span>
            <span
              className={
                'inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full text-[10px] font-semibold ' +
                (isActive ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-500')
              }
            >
              {seg.customer_count.toLocaleString('ar-SA')}
            </span>
          </button>
        )
      })}
    </div>
  )
}
