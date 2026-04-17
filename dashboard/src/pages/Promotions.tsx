import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Gift,
  Plus,
  Pause,
  Play,
  Trash2,
  Calendar,
  Users,
  TrendingUp,
  Percent,
  Truck,
  Target,
  Coins,
  Bot,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import AiBanner from '../components/ui/AiBanner'
import PageHeader from '../components/ui/PageHeader'
import {
  promotionsApi,
  PROMOTION_TYPE_META,
  PROMOTION_STATUS_META,
  type Promotion,
  type PromotionStatus,
  type PromotionType,
  type PromotionsSummary,
} from '../api/promotions'
import {
  automationsApi,
  AUTOMATION_META,
  type AutomationRecord,
  type EngineKey,
} from '../api/automations'

// ── Display helpers ───────────────────────────────────────────────────────────

const TYPE_ICON: Record<PromotionType, typeof Percent> = {
  percentage:         Percent,
  fixed:              Coins,
  free_shipping:      Truck,
  threshold_discount: Target,
  buy_x_get_y:        Gift,
}

const STATUS_FILTERS: Array<{ key: 'all' | PromotionStatus; label: string }> = [
  { key: 'all',       label: 'الكل' },
  { key: 'active',    label: 'نشط' },
  { key: 'scheduled', label: 'مجدول' },
  { key: 'paused',    label: 'متوقف' },
  { key: 'draft',     label: 'مسودة' },
  { key: 'expired',   label: 'منتهي' },
]

function formatDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString('ar-SA', {
      year:  'numeric',
      month: 'short',
      day:   'numeric',
    })
  } catch {
    return iso
  }
}

function describeValue(promo: Promotion): string {
  if (promo.promotion_type === 'free_shipping') return 'شحن مجاني'
  if (promo.promotion_type === 'buy_x_get_y') {
    const x = promo.conditions.x_quantity ?? 1
    const y = promo.conditions.y_quantity ?? 1
    return `اشترِ ${x} واحصل على ${y} مجاناً`
  }
  if (promo.discount_value === null || promo.discount_value === undefined) return '—'
  if (promo.promotion_type === 'fixed') return `${promo.discount_value} ر.س`
  return `${promo.discount_value}%`
}

function describeConditions(promo: Promotion): string[] {
  const out: string[] = []
  const c = promo.conditions || {}
  if (c.min_order_amount) out.push(`الحد الأدنى ${c.min_order_amount} ر.س`)
  if (Array.isArray(c.customer_segments) && c.customer_segments.length > 0) {
    out.push(`الشريحة: ${c.customer_segments.join('، ')}`)
  }
  if (Array.isArray(c.applicable_categories) && c.applicable_categories.length > 0) {
    out.push(`فئات محددة (${c.applicable_categories.length})`)
  }
  if (Array.isArray(c.applicable_products) && c.applicable_products.length > 0) {
    out.push(`منتجات محددة (${c.applicable_products.length})`)
  }
  return out
}

// ── KPI strip ────────────────────────────────────────────────────────────────

function KpiCard({ label, value, accent = 'slate', icon: Icon }: {
  label: string
  value: string | number
  accent?: 'green' | 'blue' | 'amber' | 'slate' | 'purple'
  icon: typeof Percent
}) {
  const tone: Record<string, string> = {
    green:  'text-emerald-600 bg-emerald-50',
    blue:   'text-blue-600    bg-blue-50',
    amber:  'text-amber-600   bg-amber-50',
    slate:  'text-slate-600   bg-slate-100',
    purple: 'text-purple-600  bg-purple-50',
  }
  return (
    <div className="card p-4 flex items-center gap-3">
      <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${tone[accent]}`}>
        <Icon className="w-5 h-5" />
      </div>
      <div className="min-w-0">
        <p className="text-[11px] text-slate-500 truncate">{label}</p>
        <p className="text-lg font-bold text-slate-900 mt-0.5">{value}</p>
      </div>
    </div>
  )
}

// ── Create modal ─────────────────────────────────────────────────────────────

interface CreateModalProps {
  open: boolean
  onClose: () => void
  onCreated: () => void
}

function CreatePromotionModal({ open, onClose, onCreated }: CreateModalProps) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [type, setType] = useState<PromotionType>('percentage')
  const [discountValue, setDiscountValue] = useState<string>('15')
  const [minOrderAmount, setMinOrderAmount] = useState<string>('')
  const [startsAt, setStartsAt] = useState<string>('')
  const [endsAt, setEndsAt] = useState<string>('')
  const [usageLimit, setUsageLimit] = useState<string>('')
  const [activateNow, setActivateNow] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const meta = PROMOTION_TYPE_META[type]

  const reset = () => {
    setName(''); setDescription(''); setType('percentage'); setDiscountValue('15')
    setMinOrderAmount(''); setStartsAt(''); setEndsAt(''); setUsageLimit('')
    setActivateNow(true); setErr(null)
  }

  const handleClose = () => { reset(); onClose() }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr(null)
    if (!name.trim()) { setErr('الاسم مطلوب'); return }
    if (meta.needsValue && !discountValue) { setErr('قيمة الخصم مطلوبة'); return }

    const payload = {
      name: name.trim(),
      description: description.trim() || null,
      promotion_type: type,
      discount_value: meta.needsValue ? Number(discountValue) : null,
      conditions: minOrderAmount
        ? { min_order_amount: Number(minOrderAmount) }
        : {},
      starts_at: startsAt || null,
      ends_at:   endsAt   || null,
      status: activateNow ? ('active' as const) : ('draft' as const),
      usage_limit: usageLimit ? Number(usageLimit) : null,
    }

    setSubmitting(true)
    try {
      await promotionsApi.create(payload)
      handleClose()
      onCreated()
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'تعذّر إنشاء العرض')
    } finally {
      setSubmitting(false)
    }
  }

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40 backdrop-blur-sm">
      <div className="card w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <h2 className="text-base font-bold text-slate-900">عرض جديد</h2>
          <button onClick={handleClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-5 h-5" />
          </button>
        </div>
        <form onSubmit={submit} className="p-5 space-y-4">
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">اسم العرض *</label>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="مثال: خصم رمضان 15%"
              className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">الوصف</label>
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={2}
              placeholder="ملاحظات داخلية للتاجر فقط"
              className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none resize-none"
            />
          </div>

          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">نوع العرض *</label>
            <div className="grid grid-cols-2 gap-2">
              {(Object.keys(PROMOTION_TYPE_META) as PromotionType[]).map(k => {
                const m = PROMOTION_TYPE_META[k]
                const Icon = TYPE_ICON[k]
                const active = type === k
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setType(k)}
                    className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition ${
                      active
                        ? 'border-brand-400 bg-brand-50 text-brand-700'
                        : 'border-slate-200 hover:border-slate-300 text-slate-700'
                    }`}
                  >
                    <Icon className="w-4 h-4 shrink-0" />
                    <span className="truncate">{m.label}</span>
                  </button>
                )
              })}
            </div>
            <p className="text-[11px] text-slate-500 mt-1.5">{meta.description}</p>
          </div>

          {meta.needsValue && (
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                قيمة الخصم * {type === 'percentage' || type === 'threshold_discount' ? '(%)' : '(ر.س)'}
              </label>
              <input
                type="number"
                step="0.01"
                value={discountValue}
                onChange={e => setDiscountValue(e.target.value)}
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">الحد الأدنى للطلب (ر.س)</label>
              <input
                type="number"
                step="0.01"
                value={minOrderAmount}
                onChange={e => setMinOrderAmount(e.target.value)}
                placeholder="اختياري"
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">حد الاستخدام الإجمالي</label>
              <input
                type="number"
                value={usageLimit}
                onChange={e => setUsageLimit(e.target.value)}
                placeholder="غير محدود"
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">تاريخ البدء</label>
              <input
                type="datetime-local"
                value={startsAt}
                onChange={e => setStartsAt(e.target.value)}
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">تاريخ الانتهاء</label>
              <input
                type="datetime-local"
                value={endsAt}
                onChange={e => setEndsAt(e.target.value)}
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={activateNow}
              onChange={e => setActivateNow(e.target.checked)}
              className="rounded border-slate-300"
            />
            تفعيل العرض فوراً
          </label>

          {err && <p className="text-xs text-red-600">{err}</p>}

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={handleClose}
              className="text-sm px-4 py-2 rounded-lg text-slate-600 hover:bg-slate-100"
            >
              إلغاء
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="btn-primary text-sm disabled:opacity-50"
            >
              {submitting ? 'جارٍ الحفظ…' : 'حفظ العرض'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ── Engine display ───────────────────────────────────────────────────────────
//
// Maps each Autopilot engine to a colour + label. Used by the
// "تستخدم في …" badge so the merchant sees which engine of the
// brain is consuming this promotion.

const ENGINE_META: Record<EngineKey, { label: string; variant: 'green' | 'blue' | 'purple' | 'amber' }> = {
  recovery:     { label: 'محرك الاسترجاع', variant: 'green'  },
  growth:       { label: 'محرك النمو',     variant: 'blue'   },
  experience:   { label: 'محرك التجربة',   variant: 'purple' },
  intelligence: { label: 'محرك الذكاء',    variant: 'amber'  },
}

interface PromotionUsage {
  automation_id:   number
  automation_type: string
  automation_name: string
  engine:          EngineKey
  enabled:         boolean
}

/**
 * Walk every automation and return the ones whose `config.promotion_id`
 * (top-level or per-step) matches the given promotion. The merchant
 * sees this on each card as "يُستخدم في حملة X (محرك Y)".
 */
function usagesForPromotion(promotionId: number, automations: AutomationRecord[]): PromotionUsage[] {
  const out: PromotionUsage[] = []
  for (const a of automations) {
    const cfg = (a.config || {}) as Record<string, unknown>
    const direct = cfg.promotion_id === promotionId
    const steps  = Array.isArray(cfg.steps) ? (cfg.steps as Array<Record<string, unknown>>) : []
    const stepHit = steps.some(s => s.promotion_id === promotionId)
    if (direct || stepHit) {
      out.push({
        automation_id:   a.id,
        automation_type: a.automation_type,
        automation_name: a.name || AUTOMATION_META[a.automation_type]?.label || a.automation_type,
        engine:          a.engine,
        enabled:         a.enabled,
      })
    }
  }
  return out
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function Promotions() {
  const [promotions, setPromotions] = useState<Promotion[]>([])
  const [summary, setSummary] = useState<PromotionsSummary | null>(null)
  const [automations, setAutomations] = useState<AutomationRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<typeof STATUS_FILTERS[number]['key']>('all')
  const [createOpen, setCreateOpen] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [list, sum, autos] = await Promise.all([
        promotionsApi.list(),
        promotionsApi.summary(),
        automationsApi.list().catch(() => ({ automations: [], autopilot_enabled: false })),
      ])
      setPromotions(list.promotions)
      setSummary(sum)
      setAutomations(autos.automations || [])
    } catch (e) {
      setError(e instanceof Error ? e.message : 'تعذّر تحميل العروض')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const filtered = useMemo(() => {
    if (filter === 'all') return promotions
    return promotions.filter(p => p.effective_status === filter)
  }, [promotions, filter])

  const handleToggle = async (promo: Promotion) => {
    const isActive = promo.effective_status === 'active'
    setPromotions(prev => prev.map(p =>
      p.id === promo.id
        ? { ...p, status: isActive ? 'paused' : 'active', effective_status: isActive ? 'paused' : 'active', is_live: !isActive }
        : p,
    ))
    try {
      const updated = isActive
        ? await promotionsApi.pause(promo.id)
        : await promotionsApi.activate(promo.id)
      setPromotions(prev => prev.map(p => (p.id === promo.id ? updated : p)))
    } catch (e) {
      load()
      alert(e instanceof Error ? e.message : 'تعذّر تحديث العرض')
    }
  }

  const handleDelete = async (promo: Promotion) => {
    if (!window.confirm(`حذف العرض "${promo.name}"؟ لا يمكن التراجع.`)) return
    try {
      await promotionsApi.remove(promo.id)
      load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر حذف العرض')
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="العروض الذكية"
        subtitle="تعريفات الحوافز التي يستخدمها الطيار الآلي عند إطلاق الحملات — أنت تضع الإطار، نحلة تختار التوقيت والعميل"
        action={
          <button className="btn-primary text-sm" onClick={() => setCreateOpen(true)}>
            <Plus className="w-4 h-4" /> عرض جديد
          </button>
        }
      />

      {/* AI banner — sets the tone */}
      <AiBanner
        title="العروض هي وقود الطيار الآلي للحملات"
        body="عرّف العرض هنا (نوع، نسبة، شروط، نافذة زمنية)، ثم اربطه بأتمتة في الطيار الآلي. عند إطلاق الحملة، يولّد الطيار الآلي كوداً شخصياً (NHxxx) لكل عميل يحقّق الشروط ويرسله عبر واتساب."
        bullets={[
          'العرض = قاعدة، ليس كوداً فردياً',
          'الطيار الآلي يختار التوقيت والعميل المناسب',
          'كل تفعيل يُسجَّل ككود شخصي قابل للتتبع',
          'يدعم: نسبة، مبلغ ثابت، شحن مجاني، اشترِ X واحصل على Y',
        ]}
      />

      {/* KPI strip */}
      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <KpiCard label="عروض نشطة"        value={summary.active}             accent="green"  icon={TrendingUp} />
          <KpiCard label="عروض مجدولة"      value={summary.scheduled}          accent="blue"   icon={Calendar} />
          <KpiCard label="إجمالي العروض"    value={summary.total}              accent="slate"  icon={Gift} />
          <KpiCard label="كوبونات مولّدة"   value={summary.codes_materialised} accent="purple" icon={Users} />
        </div>
      )}

      {/* Filters */}
      <div className="card p-2 flex flex-wrap gap-1">
        {STATUS_FILTERS.map(f => {
          const active = filter === f.key
          return (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`text-xs px-3 py-1.5 rounded-lg transition ${
                active
                  ? 'bg-brand-50 text-brand-700 font-semibold'
                  : 'text-slate-600 hover:bg-slate-100'
              }`}
            >
              {f.label}
            </button>
          )
        })}
      </div>

      {error && (
        <div className="card p-4 text-sm text-red-600 bg-red-50 border border-red-200">
          {error}
        </div>
      )}

      {loading ? (
        <div className="card p-8 text-center text-sm text-slate-500">جارٍ التحميل…</div>
      ) : filtered.length === 0 ? (
        <div className="card p-12 text-center">
          <Gift className="w-10 h-10 text-slate-300 mx-auto mb-3" />
          <p className="text-sm text-slate-600 font-medium">لا توجد عروض في هذا التصنيف</p>
          <p className="text-xs text-slate-400 mt-1">أنشئ أول عرض ليبدأ النظام بتوليد كوبونات شخصية تلقائياً</p>
        </div>
      ) : (
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map(promo => {
            const meta = PROMOTION_TYPE_META[promo.promotion_type]
            const status = PROMOTION_STATUS_META[promo.effective_status]
            const Icon = TYPE_ICON[promo.promotion_type]
            const conditions = describeConditions(promo)
            const usages = usagesForPromotion(promo.id, automations)
            return (
              <div key={promo.id} className="card p-4 flex flex-col">
                <div className="flex items-start justify-between gap-2 mb-2">
                  <div className="flex items-center gap-2 min-w-0">
                    <div className="w-9 h-9 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center shrink-0">
                      <Icon className="w-4.5 h-4.5" />
                    </div>
                    <div className="min-w-0">
                      <h3 className="text-sm font-semibold text-slate-900 truncate">{promo.name}</h3>
                      <p className="text-[11px] text-slate-500 truncate">{meta.label}</p>
                    </div>
                  </div>
                  <Badge label={status.label} variant={status.variant} dot />
                </div>

                <div className="text-2xl font-bold text-slate-900 my-2">{describeValue(promo)}</div>

                {promo.description && (
                  <p className="text-xs text-slate-500 line-clamp-2 mb-3">{promo.description}</p>
                )}

                {conditions.length > 0 && (
                  <ul className="text-[11px] text-slate-500 space-y-0.5 mb-3">
                    {conditions.map((c, i) => (<li key={i}>• {c}</li>))}
                  </ul>
                )}

                <div className="flex items-center gap-3 text-[11px] text-slate-500 mb-3">
                  {promo.starts_at && (<span>من {formatDate(promo.starts_at)}</span>)}
                  {promo.ends_at && (<span>حتى {formatDate(promo.ends_at)}</span>)}
                </div>

                {/* "Used by" — connects this promotion to the Autopilot brain */}
                {usages.length > 0 ? (
                  <div className="rounded-lg bg-amber-50/60 border border-amber-200/70 px-2.5 py-2 mb-3">
                    <div className="flex items-center gap-1.5 mb-1.5">
                      <Bot className="w-3 h-3 text-amber-600" />
                      <span className="text-[10px] font-semibold text-amber-800 uppercase tracking-wide">
                        يستخدمه الطيار الآلي
                      </span>
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {usages.map(u => {
                        const em = ENGINE_META[u.engine]
                        return (
                          <Link
                            key={u.automation_id}
                            to="/smart-automations"
                            title={`فتح الأتمتة في الطيار الآلي${u.enabled ? '' : ' (متوقفة)'}`}
                            className="inline-flex items-center gap-1 hover:opacity-80"
                          >
                            <Badge label={`${u.automation_name} · ${em.label}`} variant={em.variant} />
                          </Link>
                        )
                      })}
                    </div>
                  </div>
                ) : (
                  <div className="rounded-lg bg-slate-50 border border-slate-200/60 px-2.5 py-2 mb-3">
                    <p className="text-[11px] text-slate-500">
                      <span className="font-semibold">غير مرتبط بأي حملة بعد.</span>{' '}
                      اربطه من{' '}
                      <Link to="/smart-automations" className="text-brand-600 hover:underline">الطيار الآلي</Link>
                      {' '}ليبدأ توليد الأكواد تلقائياً.
                    </p>
                  </div>
                )}

                <div className="flex items-center justify-between mt-auto pt-3 border-t border-slate-100">
                  <span className="text-[11px] text-slate-500">
                    استُخدم {promo.usage_count}{promo.usage_limit ? ` / ${promo.usage_limit}` : ''}
                  </span>
                  <div className="flex items-center gap-1.5">
                    <button
                      onClick={() => handleToggle(promo)}
                      title={promo.effective_status === 'active' ? 'إيقاف' : 'تفعيل'}
                      className="p-1.5 rounded hover:bg-slate-100 text-slate-600"
                    >
                      {promo.effective_status === 'active'
                        ? <Pause className="w-4 h-4" />
                        : <Play className="w-4 h-4" />}
                    </button>
                    <button
                      onClick={() => handleDelete(promo)}
                      title="حذف"
                      className="p-1.5 rounded hover:bg-red-50 text-red-500"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <CreatePromotionModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={load}
      />
    </div>
  )
}
