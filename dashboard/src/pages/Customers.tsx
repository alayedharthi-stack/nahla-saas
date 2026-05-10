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
  Info,
  Trash2,
  CheckSquare,
  Square,
  BellOff,
  Tag,
  Beaker,
  Plus,
  ShieldOff,
  Filter,
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
  // Manual segment filter — separate axis from `segmentKey` so a
  // merchant can combine "auto VIP" with "manually tagged unsubscribed"
  // (or "no manual tag at all" via the special 'none' value).
  const [manualSegmentKey, setManualSegmentKey] = useState<string>('')
  const [marketingOptOutFilter, setMarketingOptOutFilter] = useState<'all' | 'in' | 'out'>('all')
  const [segments, setSegments] = useState<CustomerSegmentMeta[]>([])
  const [segmentsLoading, setSegmentsLoading] = useState(true)
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [addName, setAddName] = useState('')
  const [addPhone, setAddPhone] = useState('')
  const [addEmail, setAddEmail] = useState('')
  const [addError, setAddError] = useState('')
  const [addLoading, setAddLoading] = useState(false)
  const [selectedCustomer, setSelectedCustomer] = useState<CustomerRecord | null>(null)

  // Selection & bulk delete
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [deleteModal, setDeleteModal] = useState<'selected' | 'all' | null>(null)
  const [deleteConfirmText, setDeleteConfirmText] = useState('')
  const [deleteLoading, setDeleteLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [res, metricsRes] = await Promise.all([
        customersApi.list({
          search, page, perPage: 50, segment: segmentKey,
          manualSegment: manualSegmentKey || undefined,
          marketingOptOut: marketingOptOutFilter === 'all'
            ? undefined
            : marketingOptOutFilter === 'out',
        }),
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
  }, [search, page, segmentKey, manualSegmentKey, marketingOptOutFilter])

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
  }, [search, segmentKey, manualSegmentKey, marketingOptOutFilter])

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
      setSelectedIds(prev => { const s = new Set(prev); s.delete(id); return s })
      load()
    } catch {
      alert('حدث خطأ أثناء الحذف')
    }
  }

  const toggleSelect = (id: number) => {
    setSelectedIds(prev => {
      const s = new Set(prev)
      s.has(id) ? s.delete(id) : s.add(id)
      return s
    })
  }

  const toggleSelectAll = () => {
    if (selectedIds.size === customers.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(customers.map(c => c.id)))
    }
  }

  const handleBulkDelete = async () => {
    setDeleteLoading(true)
    try {
      if (deleteModal === 'all') {
        await customersApi.deleteAll()
      } else {
        await customersApi.bulkDelete(Array.from(selectedIds))
      }
      setSelectedIds(new Set())
      setDeleteModal(null)
      setDeleteConfirmText('')
      setSelectedCustomer(null)
      load()
    } catch {
      alert('حدث خطأ أثناء الحذف')
    } finally {
      setDeleteLoading(false)
    }
  }

  const allCurrentSelected = customers.length > 0 && selectedIds.size === customers.length
  const someSelected = selectedIds.size > 0 && !allCurrentSelected

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

      {/* Unified segment chips — each chip shows EVERY customer in that
          cohort, whether the auto classifier put them there OR the
          merchant added them manually. The drawer surfaces the source
          per-customer ("VIP تلقائي" vs "VIP يدوي" vs "VIP يدوي + تلقائي").
          We label the strip explicitly so merchants don't read "RFM"
          into it — to them it's just "the segments". */}
      <div className="space-y-1">
        <div className="flex items-center gap-2 px-1">
          <Tag className="w-3.5 h-3.5 text-slate-400" />
          <span className="text-[11px] font-semibold text-slate-600">
            شرائح العملاء
          </span>
          <span className="text-[10px] text-slate-400">
            تشمل التصنيف الذكي والتصنيف اليدوي معاً
          </span>
        </div>
        <SegmentChips
          segments={segments}
          loading={segmentsLoading}
          active={segmentKey}
          onSelect={setSegmentKey}
        />
      </div>

      {/* Search + Actions */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="بحث بالاسم أو رقم الهاتف..."
            className="w-full ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none"
          />
        </div>

        {/* Manual-only filter — narrow axis for merchants who want to
            see ONLY their manually-tagged customers (e.g. for audit).
            The chip strip above already unions auto+manual so this
            dropdown is opt-in tooling, not the default path. */}
        <div className="relative">
          <Tag className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
          <select
            value={manualSegmentKey}
            onChange={(e) => setManualSegmentKey(e.target.value)}
            className="ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            title="فلترة حسب العملاء المُصنَّفين يدوياً فقط"
          >
            <option value="">عرض الكل</option>
            <option value="none">— بدون أي تصنيف يدوي —</option>
            {segments.filter(s => s.key !== 'all').map(s => (
              <option key={s.key} value={s.key}>يدوي فقط: {s.label_ar}</option>
            ))}
          </select>
        </div>

        {/* Marketing opt-out filter */}
        <div className="relative">
          <Filter className="absolute start-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400 pointer-events-none" />
          <select
            value={marketingOptOutFilter}
            onChange={(e) => setMarketingOptOutFilter(e.target.value as 'all' | 'in' | 'out')}
            className="ps-9 pe-3 py-2 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
            title="فلترة حسب الاستبعاد التسويقي اليدوي"
          >
            <option value="all">كل العملاء</option>
            <option value="in">المؤهلون للحملات</option>
            <option value="out">المستبعدون يدوياً</option>
          </select>
        </div>

        <button
          onClick={load}
          disabled={loading}
          className="btn-secondary text-sm flex items-center gap-2"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          تحديث
        </button>

        {/* Bulk actions — only visible when items are selected */}
        {selectedIds.size > 0 && (
          <button
            onClick={() => { setDeleteModal('selected'); setDeleteConfirmText('') }}
            className="flex items-center gap-2 text-sm text-red-600 bg-red-50 border border-red-200 hover:bg-red-100 px-3 py-2 rounded-lg transition-colors"
          >
            <Trash2 className="w-4 h-4" />
            حذف المحدد ({selectedIds.size})
          </button>
        )}

        {/* Delete all — always visible as secondary danger action */}
        <button
          onClick={() => { setDeleteModal('all'); setDeleteConfirmText('') }}
          className="flex items-center gap-2 text-sm text-red-500 hover:text-red-700 hover:bg-red-50 border border-transparent hover:border-red-200 px-3 py-2 rounded-lg transition-colors"
        >
          <Trash2 className="w-4 h-4" />
          حذف الكل
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
          <div className="overflow-x-auto overflow-y-auto max-h-[60vh]">
            <table className="w-full text-xs">
              <thead className="sticky top-0 z-10">
                <tr className="border-b border-slate-100 bg-slate-50">
                  {/* Select-all checkbox */}
                  <th className="px-3 py-3 w-10">
                    <button
                      onClick={toggleSelectAll}
                      className="text-slate-400 hover:text-brand-500 transition-colors"
                      title={allCurrentSelected ? 'إلغاء تحديد الكل' : 'تحديد الكل'}
                    >
                      {allCurrentSelected ? (
                        <CheckSquare className="w-4 h-4 text-brand-500" />
                      ) : someSelected ? (
                        <CheckSquare className="w-4 h-4 text-brand-300" />
                      ) : (
                        <Square className="w-4 h-4" />
                      )}
                    </button>
                  </th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الاسم</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الهاتف</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">البريد</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الحالة</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">RFM</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">القطاع الذكي</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الطلبات</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">الإنفاق</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">آخر طلب</th>
                  <th className="text-start px-3 py-3 font-medium text-slate-500">المصدر</th>
                  <th className="px-3 py-3 w-10" />
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {customers.map((c) => {
                  const isChecked = selectedIds.has(c.id)
                  return (
                    <tr
                      key={c.id}
                      className={`hover:bg-slate-50 transition-colors ${isChecked ? 'bg-brand-50/60' : ''}`}
                    >
                      {/* Checkbox */}
                      <td className="px-3 py-3">
                        <button
                          onClick={(e) => { e.stopPropagation(); toggleSelect(c.id) }}
                          className="text-slate-300 hover:text-brand-500 transition-colors"
                        >
                          {isChecked
                            ? <CheckSquare className="w-4 h-4 text-brand-500" />
                            : <Square className="w-4 h-4" />
                          }
                        </button>
                      </td>
                      <td
                        className="px-3 py-3 cursor-pointer"
                        onClick={() => setSelectedCustomer(c)}
                      >
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="font-medium text-slate-900">{c.name || '—'}</span>
                          {c.is_unsubscribed && (
                            <span className="inline-flex items-center gap-0.5 bg-red-100 text-red-700 text-[10px] font-semibold px-1.5 py-0.5 rounded-full border border-red-200">
                              <BellOff className="w-2.5 h-2.5" />
                              ألغى الاشتراك
                            </span>
                          )}
                          {!c.is_unsubscribed && c.pending_unsubscribe && (
                            <span className="inline-flex items-center gap-0.5 bg-amber-100 text-amber-700 text-[10px] font-semibold px-1.5 py-0.5 rounded-full border border-amber-200">
                              <BellOff className="w-2.5 h-2.5" />
                              بانتظار تأكيد الإلغاء
                            </span>
                          )}
                        </div>
                      </td>
                      <td dir="ltr" className="px-3 py-3 text-slate-600 font-mono cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.phone || '—'}
                      </td>
                      <td className="px-3 py-3 text-slate-500 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.email || '—'}
                      </td>
                      <td className="px-3 py-3 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        <Badge label={c.status_label} variant={segmentVariant(c.status)} />
                      </td>
                      <td className="px-3 py-3 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        <Badge
                          label={String(c.rfm_scores?.total ?? c.rfm_total_score ?? 0)}
                          variant={rfmVariant(c.rfm_scores?.total ?? c.rfm_total_score ?? 0)}
                        />
                      </td>
                      <td className="px-3 py-3 text-slate-600 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.rfm_segment_label || '—'}
                      </td>
                      <td className="px-3 py-3 text-slate-700 font-semibold cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.orders_count ?? c.total_orders}
                      </td>
                      <td className="px-3 py-3 text-slate-700 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {(c.total_spent ?? c.total_spend).toLocaleString('ar-SA')} ر.س
                      </td>
                      <td className="px-3 py-3 text-slate-500 whitespace-nowrap cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {formatDate(c.last_order_date ?? c.last_order_at)}
                      </td>
                      <td className="px-3 py-3 cursor-pointer" onClick={() => setSelectedCustomer(c)}>
                        {c.source === 'manual' ? (
                          <span className="text-xs text-blue-600 bg-blue-50 px-2 py-0.5 rounded-full">{c.source_label}</span>
                        ) : (
                          <span className="text-xs text-slate-500">{c.source_label}</span>
                        )}
                      </td>
                      {/* Quick delete */}
                      <td className="px-3 py-3">
                        <button
                          onClick={(e) => { e.stopPropagation(); handleDelete(c.id) }}
                          className="text-slate-300 hover:text-red-500 transition-colors"
                          title="حذف العميل"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </td>
                    </tr>
                  )
                })}
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

      {/* ── Bulk / Delete-All Confirmation Modal ─────────────────────────── */}
      {deleteModal && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
            {/* Danger header */}
            <div className="bg-red-600 rounded-t-2xl px-6 py-5 flex items-start gap-3">
              <div className="w-10 h-10 bg-white/20 rounded-full flex items-center justify-center shrink-0">
                <Trash2 className="w-5 h-5 text-white" />
              </div>
              <div>
                <h3 className="text-base font-bold text-white">
                  {deleteModal === 'all' ? '⚠️ حذف جميع العملاء' : `⚠️ حذف ${selectedIds.size} عميل`}
                </h3>
                <p className="text-red-100 text-xs mt-1">
                  هذا الإجراء لا يمكن التراجع عنه
                </p>
              </div>
            </div>

            <div className="px-6 py-5 space-y-4">
              {/* Warning message */}
              <div className="bg-red-50 border border-red-200 rounded-xl p-4 space-y-2">
                <p className="text-sm font-semibold text-red-800">
                  {deleteModal === 'all'
                    ? `سيتم حذف جميع العملاء (${total.toLocaleString('ar-SA')} عميل) بشكل دائم.`
                    : `سيتم حذف ${selectedIds.size} عميل محدد بشكل دائم.`
                  }
                </p>
                <ul className="text-xs text-red-700 space-y-1 list-disc list-inside">
                  <li>لن تتمكن من استعادة هذه البيانات</li>
                  <li>سيتم حذف جميع معلومات العملاء وبياناتهم</li>
                  <li>قد تتأثر الحملات والأتمتة المرتبطة بهم</li>
                </ul>
              </div>

              {/* Confirmation input */}
              <div>
                <label className="block text-xs font-medium text-slate-700 mb-2">
                  للتأكيد، اكتب كلمة{' '}
                  <span className="font-mono font-bold text-red-600 bg-red-50 px-1.5 py-0.5 rounded">
                    احذف
                  </span>{' '}
                  في الحقل أدناه:
                </label>
                <input
                  type="text"
                  value={deleteConfirmText}
                  onChange={e => setDeleteConfirmText(e.target.value)}
                  placeholder="اكتب: احذف"
                  className="w-full px-3 py-2 text-sm border-2 border-slate-200 rounded-lg focus:ring-0 focus:border-red-400 outline-none transition-colors"
                  autoFocus
                />
              </div>

              {/* Actions */}
              <div className="flex gap-3 pt-1">
                <button
                  onClick={() => { setDeleteModal(null); setDeleteConfirmText('') }}
                  className="flex-1 btn-secondary text-sm"
                  disabled={deleteLoading}
                >
                  إلغاء
                </button>
                <button
                  onClick={handleBulkDelete}
                  disabled={deleteConfirmText !== 'احذف' || deleteLoading}
                  className="flex-1 flex items-center justify-center gap-2 text-sm bg-red-600 hover:bg-red-700 text-white rounded-lg py-2 font-medium transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  {deleteLoading ? (
                    <div className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                  ) : (
                    <Trash2 className="w-4 h-4" />
                  )}
                  {deleteLoading ? 'جارٍ الحذف...' : 'حذف نهائي'}
                </button>
              </div>
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
                <div className={`w-16 h-16 rounded-full flex items-center justify-center mx-auto ${selectedCustomer.is_unsubscribed ? 'bg-red-50' : 'bg-brand-50'}`}>
                  {selectedCustomer.is_unsubscribed
                    ? <BellOff className="w-8 h-8 text-red-400" />
                    : <User className="w-8 h-8 text-brand-500" />
                  }
                </div>
                <h4 className="text-base font-semibold text-slate-900">
                  {selectedCustomer.name}
                </h4>
                {selectedCustomer.is_unsubscribed && (
                  <div className="bg-red-50 border border-red-200 rounded-lg px-3 py-2 text-xs text-red-700 text-center space-y-1">
                    <p className="font-semibold flex items-center justify-center gap-1">
                      <BellOff className="w-3.5 h-3.5" />
                      ألغى الاشتراك
                    </p>
                    <p className="text-red-500">مستثنى من الحملات والطيار الآلي والذكاء</p>
                    <p className="text-slate-500 text-[10px]">يعود تلقائياً عند إرساله أي رسالة</p>
                  </div>
                )}
                {!selectedCustomer.is_unsubscribed && selectedCustomer.pending_unsubscribe && (
                  <div className="bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 text-xs text-amber-800 text-center space-y-1">
                    <p className="font-semibold flex items-center justify-center gap-1">
                      <BellOff className="w-3.5 h-3.5" />
                      بانتظار تأكيد إلغاء الاشتراك
                    </p>
                    <p className="text-amber-700">طلب الإلغاء — أُرسلت له رسالة تأكيد بزرين</p>
                    <p className="text-slate-500 text-[10px]">يتم إيقاف الأتمتة والذكاء حتى يضغط "نعم متأكد" أو "تراجع"</p>
                  </div>
                )}
                <div className="flex flex-wrap items-center justify-center gap-1.5">
                  <Badge
                    label={selectedCustomer.status_label}
                    variant={segmentVariant(selectedCustomer.status)}
                  />
                  <span className="text-[10px] text-slate-400 px-1">تصنيف ذكي</span>
                </div>
                {selectedCustomer.source === 'manual' && (
                  <span className="block text-xs text-blue-600">
                    {selectedCustomer.source_label}
                  </span>
                )}
              </div>

              {/* Manual segments — merchant-curated, drawn from the same
                  Nahla registry as the auto chip strip. We do NOT allow
                  free-form tags by design (segment_key MUST validate
                  against `services.nahla_segments`). */}
              <ManualSegmentsSection
                customer={selectedCustomer}
                segments={segments}
                onChange={async (next) => {
                  setSelectedCustomer(next)
                  // Refresh the row inside the table so the new
                  // tags / opt-out icon update without a full page
                  // reload.
                  setCustomers(prev => prev.map(c => c.id === next.id ? next : c))
                }}
                onRequireListReload={load}
              />

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
 * Drawer block that lets the merchant pin / unpin Nahla *manual*
 * segment tags on this customer, plus toggle the two marketing
 * preference flags (opt-out + test-recipient).
 *
 * Hard rule: manual tags MUST come from the official Nahla segment
 * registry — we render a dropdown, never a free-form input. The
 * backend re-validates the key on every POST so even a stale UI
 * cannot inject arbitrary strings.
 */
interface ManualSegmentsSectionProps {
  customer: CustomerRecord
  segments: CustomerSegmentMeta[]
  onChange: (next: CustomerRecord) => void | Promise<void>
  onRequireListReload?: () => void
}

function ManualSegmentsSection({
  customer, segments, onChange, onRequireListReload,
}: ManualSegmentsSectionProps) {
  const [adding, setAdding] = useState<string>('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string>('')

  const manualKeys = customer.manual_segments || []
  const optedOut = customer.marketing_opt_out_manual
  const isTestRecipient = customer.is_campaign_test_recipient

  // Pool of segments the merchant can still add — exclude already
  // pinned ones + the meta "all" segment which would be meaningless
  // as a per-customer tag.
  const addableSegments = segments.filter(
    s => s.key !== 'all' && !manualKeys.includes(s.key),
  )

  const handleAdd = async (key: string) => {
    if (!key) return
    setBusy(key)
    setError('')
    try {
      const res = await customersApi.addManualSegment(customer.id, key)
      const labels = res.manual_segments.map(
        k => segments.find(s => s.key === k)?.label_ar || k,
      )
      await onChange({
        ...customer,
        manual_segments: res.manual_segments,
        manual_segments_labels: labels,
      })
      setAdding('')
    } catch (err: any) {
      setError(err?.detail || err?.message || 'تعذر إضافة التصنيف')
    } finally {
      setBusy(null)
    }
  }

  const handleRemove = async (key: string) => {
    setBusy(key)
    setError('')
    try {
      const res = await customersApi.removeManualSegment(customer.id, key)
      const labels = res.manual_segments.map(
        k => segments.find(s => s.key === k)?.label_ar || k,
      )
      await onChange({
        ...customer,
        manual_segments: res.manual_segments,
        manual_segments_labels: labels,
      })
    } catch (err: any) {
      setError(err?.detail || err?.message || 'تعذر حذف التصنيف')
    } finally {
      setBusy(null)
    }
  }

  const handleToggleOptOut = async () => {
    setBusy('__opt__')
    setError('')
    try {
      const res = await customersApi.updateMarketingPreferences(customer.id, {
        marketing_opt_out_manual: !optedOut,
      })
      await onChange({
        ...customer,
        marketing_opt_out_manual: res.marketing_opt_out_manual,
        marketing_opt_out_manual_at: res.marketing_opt_out_manual_at,
      })
      onRequireListReload?.()
    } catch (err: any) {
      setError(err?.detail || err?.message || 'تعذر تحديث التفضيل')
    } finally {
      setBusy(null)
    }
  }

  const handleToggleTest = async () => {
    setBusy('__test__')
    setError('')
    try {
      const res = await customersApi.updateMarketingPreferences(customer.id, {
        is_campaign_test_recipient: !isTestRecipient,
      })
      await onChange({
        ...customer,
        is_campaign_test_recipient: res.is_campaign_test_recipient,
      })
    } catch (err: any) {
      setError(err?.detail || err?.message || 'تعذر تحديث قائمة الاختبار')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-3 border border-slate-100 rounded-xl p-3 bg-slate-50/40">
      <div className="flex items-center justify-between">
        <h5 className="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
          <Tag className="w-3.5 h-3.5 text-slate-400" />
          شرائح هذا العميل
        </h5>
        <span className="text-[10px] text-slate-400">
          ذكي + يدوي
        </span>
      </div>

      {/* Unified per-segment chips. Each chip carries a source label
          ("VIP يدوي + تلقائي" / "VIP يدوي" / "VIP تلقائي" /
          "مستبعد يدويًا من VIP") and visually distinguishes excludes
          from positive memberships so the merchant can tell at a
          glance why this customer is (or isn't) in each segment. */}
      <div className="flex flex-wrap gap-1.5">
        {(() => {
          // Build a unified row set from segment_sources (server) so
          // we render one pill per segment with the right source.
          const sources = customer.segment_sources || {}
          const keys = Object.keys(sources)
          if (keys.length === 0) {
            return (
              <p className="text-[11px] text-slate-400 italic">
                لم يُصنَّف هذا العميل بعد في أي شريحة — أضف تصنيفاً يدوياً أدناه.
              </p>
            )
          }
          return keys.map(k => {
            const src = sources[k]
            const label = segments.find(s => s.key === k)?.label_ar || k
            const isExcluded = src.manual_exclude
            const sourceLabel = isExcluded
              ? `مستبعد يدويًا من ${label}`
              : src.manual_include && src.automatic
                ? `${label} — يدوي + تلقائي`
                : src.manual_include
                  ? `${label} — يدوي`
                  : `${label} — تلقائي`
            const cls = isExcluded
              ? 'text-slate-500 bg-slate-100 border-slate-200'
              : src.manual_include && src.automatic
                ? 'text-emerald-700 bg-emerald-50 border-emerald-200'
                : src.manual_include
                  ? 'text-amber-700 bg-amber-50 border-amber-200'
                  : 'text-blue-700 bg-blue-50 border-blue-200'
            return (
              <span
                key={k}
                className={`inline-flex items-center gap-1 text-[11px] font-semibold border px-2 py-1 rounded-full ${cls}`}
                title={
                  isExcluded
                    ? 'استبعدتَ هذا العميل من هذه الشريحة يدوياً.'
                    : src.manual_include && src.automatic
                      ? 'صنّفه التاجر يدوياً والذكاء التلقائي يطابقه أيضاً.'
                      : src.manual_include
                        ? 'صنّفه التاجر يدوياً.'
                        : 'تصنيف ذكي تلقائي بناءً على السلوك.'
                }
              >
                {sourceLabel}
                {/* Remove button only on manual-include or auto rows.
                    Exclude rows show a "restore" button instead. */}
                {!isExcluded && (
                  <button
                    type="button"
                    disabled={busy === k}
                    onClick={() => handleRemove(k)}
                    className="text-current opacity-60 hover:opacity-100 disabled:opacity-30"
                    title={
                      src.automatic && !src.manual_include
                        ? 'استبعِد هذا العميل من الشريحة'
                        : 'إزالة التصنيف اليدوي'
                    }
                  >
                    <X className="w-3 h-3" />
                  </button>
                )}
                {isExcluded && (
                  <button
                    type="button"
                    disabled={busy === k}
                    onClick={() => handleAdd(k)}
                    className="text-current opacity-60 hover:opacity-100 disabled:opacity-30"
                    title="إعادة إلى التصنيف"
                  >
                    <Plus className="w-3 h-3" />
                  </button>
                )}
              </span>
            )
          })
        })()}
      </div>

      {/* Add new tag — dropdown, never a free-form text field */}
      <div className="flex items-center gap-2">
        <select
          value={adding}
          onChange={(e) => setAdding(e.target.value)}
          className="flex-1 ps-2 pe-2 py-1.5 text-xs border border-slate-200 rounded-lg focus:ring-2 focus:ring-brand-500 focus:border-brand-500 outline-none bg-white"
          disabled={addableSegments.length === 0 || !!busy}
        >
          <option value="">
            {addableSegments.length === 0
              ? 'كل التصنيفات الرسمية مضافة'
              : 'اختر تصنيفاً لإضافته…'}
          </option>
          {addableSegments.map(s => (
            <option key={s.key} value={s.key}>{s.label_ar}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => handleAdd(adding)}
          disabled={!adding || !!busy}
          className="text-xs font-semibold text-brand-600 bg-brand-50 hover:bg-brand-100 border border-brand-200 px-2.5 py-1.5 rounded-lg disabled:opacity-50 flex items-center gap-1"
        >
          <Plus className="w-3.5 h-3.5" />
          إضافة
        </button>
      </div>

      {error && (
        <p className="text-[11px] text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">
          {error}
        </p>
      )}

      {/* Marketing opt-out (manual exclusion) */}
      <div className="border-t border-slate-100 pt-3 space-y-2">
        <label className="flex items-start gap-2.5 cursor-pointer select-none">
          <button
            type="button"
            role="switch"
            aria-checked={optedOut}
            onClick={handleToggleOptOut}
            disabled={busy === '__opt__'}
            className={`mt-0.5 relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
              optedOut ? 'bg-red-500' : 'bg-slate-200'
            } ${busy === '__opt__' ? 'opacity-50' : ''}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                optedOut ? 'translate-x-4' : 'translate-x-0.5'
              }`}
            />
          </button>
          <div className="flex-1 -mt-0.5">
            <p className="text-xs font-semibold text-slate-800 flex items-center gap-1">
              <ShieldOff className="w-3.5 h-3.5 text-slate-400" />
              استبعاد من الحملات التسويقية
            </p>
            <p className="text-[11px] text-slate-500 leading-snug">
              لن يدخل في أي حملة تسويقية يدوية. لا يؤثر على رسائل
              الطلبات أو الأتمتات الخدمية أو نافذة 24 ساعة.
            </p>
          </div>
        </label>

        {/* Quick "add to campaign test list" — internal flag, no
            merchant-visible tag is created. */}
        <label className="flex items-start gap-2.5 cursor-pointer select-none">
          <button
            type="button"
            role="switch"
            aria-checked={isTestRecipient}
            onClick={handleToggleTest}
            disabled={busy === '__test__'}
            className={`mt-0.5 relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
              isTestRecipient ? 'bg-emerald-500' : 'bg-slate-200'
            } ${busy === '__test__' ? 'opacity-50' : ''}`}
          >
            <span
              className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform ${
                isTestRecipient ? 'translate-x-4' : 'translate-x-0.5'
              }`}
            />
          </button>
          <div className="flex-1 -mt-0.5">
            <p className="text-xs font-semibold text-slate-800 flex items-center gap-1">
              <Beaker className="w-3.5 h-3.5 text-slate-400" />
              إضافة إلى قائمة اختبار الحملات
            </p>
            <p className="text-[11px] text-slate-500 leading-snug">
              مجموعة داخلية صغيرة لاختبار الحملة قبل الإطلاق الكامل.
              لا يُنشئ تصنيفاً ظاهراً للتاجر.
            </p>
          </div>
        </label>
      </div>
    </div>
  )
}


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
  const [openInfo, setOpenInfo] = useState<string | null>(null)

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

  const activeSeg = segments.find(s => s.key === active) ?? null
  const popoverSeg = openInfo ? segments.find(s => s.key === openInfo) : null

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2 overflow-x-auto pb-1 -mb-1">
        {segments.map(seg => {
          const isActive = active === seg.key
          return (
            <div key={seg.key} className="relative shrink-0">
              <button
                onClick={() => onSelect(seg.key)}
                title={seg.description_ar}
                className={
                  'inline-flex items-center gap-2 ps-3.5 pe-2 py-1.5 rounded-full text-xs font-medium transition-colors border ' +
                  (seg.key === 'unsubscribed'
                    ? (isActive
                        ? 'bg-red-500 text-white border-red-500 shadow-sm'
                        : 'bg-red-50 text-red-600 border-red-200 hover:bg-red-100 hover:border-red-300')
                    : (isActive
                        ? 'bg-brand-500 text-white border-brand-500 shadow-sm'
                        : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50 hover:border-slate-300'))
                }
              >
                {seg.key === 'unsubscribed' && <BellOff className="w-3 h-3 shrink-0" />}
                <span>{seg.label_ar}</span>
                <span
                  className={
                    'inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5 rounded-full text-[10px] font-semibold ' +
                    (seg.key === 'unsubscribed'
                      ? (isActive ? 'bg-white/20 text-white' : 'bg-red-100 text-red-600')
                      : (isActive ? 'bg-white/20 text-white' : 'bg-slate-100 text-slate-500'))
                  }
                >
                  {seg.customer_count.toLocaleString('ar-SA')}
                </span>
                <button
                  type="button"
                  aria-label={`تعريف شريحة ${seg.label_ar}`}
                  onClick={(e) => {
                    e.stopPropagation()
                    setOpenInfo(openInfo === seg.key ? null : seg.key)
                  }}
                  className={
                    'ms-0.5 inline-flex items-center justify-center w-5 h-5 rounded-full transition-colors ' +
                    (isActive
                      ? 'text-white/80 hover:bg-white/15'
                      : (seg.key === 'unsubscribed'
                          ? 'text-red-400 hover:bg-red-100 hover:text-red-600'
                          : 'text-slate-400 hover:bg-slate-100 hover:text-slate-600'))
                  }
                >
                  <Info className="w-3.5 h-3.5" />
                </button>
              </button>
            </div>
          )
        })}
      </div>

      {/* Lightweight inline info card — opens under the chips, NOT a modal,
          so the merchant can keep scanning the table while reading. */}
      {popoverSeg && (
        <div className="relative bg-white border border-slate-200 rounded-lg shadow-sm p-4">
          <button
            type="button"
            onClick={() => setOpenInfo(null)}
            className="absolute top-3 end-3 text-slate-400 hover:text-slate-600"
            aria-label="إغلاق التعريف"
          >
            <X className="w-4 h-4" />
          </button>
          <p className="text-sm font-semibold text-slate-800 mb-1">
            {popoverSeg.label_ar}
          </p>
          <p className="text-xs text-slate-500 leading-relaxed mb-3">
            {popoverSeg.criteria_ar || popoverSeg.description_ar}
          </p>
          <div className="flex flex-wrap gap-3 text-[11px] text-slate-400">
            <span>عدد الحالي: <span className="text-slate-600 font-medium">{popoverSeg.customer_count.toLocaleString('ar-SA')}</span></span>
            {popoverSeg.crm_statuses.length > 0 && (
              <span>حالات CRM: <span className="font-mono text-slate-600">{popoverSeg.crm_statuses.join(' · ')}</span></span>
            )}
            {popoverSeg.rfm_buckets.length > 0 && (
              <span>RFM: <span className="font-mono text-slate-600">{popoverSeg.rfm_buckets.join(' · ')}</span></span>
            )}
          </div>
        </div>
      )}

      {/* Active filter hint — visible even when no info popover is open. */}
      {activeSeg && active !== 'all' && active !== 'unsubscribed' && (
        <p className="text-[11px] text-slate-500 ps-1">
          عرض {activeSeg.customer_count.toLocaleString('ar-SA')} عميل ضمن «{activeSeg.label_ar}»
        </p>
      )}

      {/* Special notice when merchant is viewing the Unsubscribed segment */}
      {active === 'unsubscribed' && (
        <div className="flex items-start gap-2 bg-red-50 border border-red-200 rounded-lg px-3 py-2.5 text-xs text-red-700">
          <BellOff className="w-4 h-4 mt-0.5 shrink-0 text-red-400" />
          <div className="space-y-0.5">
            <p className="font-semibold">هؤلاء العملاء ألغوا الاشتراك في التواصل</p>
            <p className="text-red-600">مستثنون تلقائياً من جميع الحملات والطيار الآلي والذكاء الاصطناعي.</p>
            <p className="text-slate-500 mt-1">
              💡 إذا كنت تريد استعادتهم، ننصحك بالتواصل معهم <strong>شخصياً</strong> لمعرفة الأسباب ومحاولة استعادتهم. عند إرسالهم أي رسالة سيعودون تلقائياً للقوائم العادية.
            </p>
          </div>
        </div>
      )}
    </div>
  )
}
