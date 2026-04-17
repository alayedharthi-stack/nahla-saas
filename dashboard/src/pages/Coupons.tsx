import { useEffect, useMemo, useState } from 'react'
import {
  Plus,
  Tag,
  Copy,
  Crown,
  Zap,
  Trash2,
  ToggleLeft,
  ToggleRight,
  Bot,
  Hand,
  Gift,
  TrendingUp,
  Sparkles,
  ShieldCheck,
  Pencil,
  X,
  Clock,
  Percent,
  Coins,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import AiBanner from '../components/ui/AiBanner'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  featureRealityApi,
  type CouponOrigin,
  type CouponRule,
  type CouponsDashboard,
  type DashboardCoupon,
} from '../api/featureReality'

const emptyData: CouponsDashboard = {
  rules: [],
  vip_tiers: [],
  coupons: [],
}

// ── Origin → display metadata ────────────────────────────────────────────────
//
// The merchant should never wonder "where did this code come from?". Each
// origin gets a distinct icon/colour so a glance at the table tells them
// whether the AI is pulling its weight or whether they're still managing
// codes by hand.

interface OriginMeta {
  label:    string
  variant:  'green' | 'amber' | 'red' | 'blue' | 'slate' | 'purple'
  icon:     typeof Bot
  hint:     string
}

const ORIGIN_META: Record<CouponOrigin, OriginMeta> = {
  automation: {
    label:   '🤖 من الطيار الآلي',
    variant: 'purple',
    icon:    Bot,
    hint:    'أنشأها الطيار الآلي عند تشغيل أتمتة',
  },
  promotion: {
    label:   '🎁 من عرض ترويجي',
    variant: 'amber',
    icon:    Gift,
    hint:    'كود شخصي مولّد من قاعدة عرض في صفحة العروض',
  },
  vip: {
    label:   '👑 مكافأة VIP',
    variant: 'amber',
    icon:    Crown,
    hint:    'مولّدة تلقائياً للعملاء الأكثر قيمة',
  },
  widget: {
    label:   '✨ من أداة جذب',
    variant: 'blue',
    icon:    Sparkles,
    hint:    'مولّدة من أداة زيادة المبيعات على الموقع',
  },
  manual: {
    label:   '✋ يدوي',
    variant: 'slate',
    icon:    Hand,
    hint:    'أنشأته أنت يدوياً',
  },
}

function originOf(c: DashboardCoupon): CouponOrigin {
  if (c.origin) return c.origin
  // Fallback for older API responses.
  if (c.category === 'vip')  return 'vip'
  if (c.category === 'auto') return 'automation'
  return 'manual'
}

// ── Rule → engine mapping (display only) ─────────────────────────────────────
//
// Each backend rule slug belongs conceptually to one of the four Autopilot
// engines. We surface this mapping so the merchant sees "this rule lives
// inside the Recovery engine" rather than thinking of rules as standalone.

interface RuleEngineMeta {
  engine:   'recovery' | 'growth' | 'experience'
  label:    string
  desc:     string
}

const RULE_ENGINE: Record<string, RuleEngineMeta> = {
  abandoned_cart: { engine: 'recovery', label: 'محرك الاسترجاع', desc: 'قيمة الكوبون المُستخدم في المرحلة الأخيرة من سير استرداد السلات (24 ساعة) — التوقيت يُدار من الأتمتة الذكية' },
  unpaid_order:   { engine: 'recovery', label: 'محرك الاسترجاع', desc: 'تحصيل الطلبات غير المدفوعة' },
  customer_winback:{engine: 'recovery', label: 'محرك الاسترجاع', desc: 'استعادة العملاء الخاملين' },
  vip_customers:  { engine: 'growth',   label: 'محرك النمو',     desc: 'مكافأة العملاء الأكثر قيمة' },
  repeat_purchase:{ engine: 'growth',   label: 'محرك النمو',     desc: 'تحفيز الشراء المتكرر' },
  predictive_reorder:{engine: 'growth', label: 'محرك النمو',     desc: 'إعادة الطلب التنبؤي' },
  active_coupons: { engine: 'recovery', label: 'محرك الاسترجاع', desc: 'الكوبونات النشطة في الحملات' },
  coupon_rules:   { engine: 'experience',label: 'محرك التجربة',  desc: 'قواعد عرض الخصم في المحادثة' },
}

const ENGINE_VARIANT: Record<RuleEngineMeta['engine'], 'green' | 'blue' | 'purple'> = {
  recovery:   'green',
  growth:     'blue',
  experience: 'purple',
}

function engineMeta(ruleId: string): RuleEngineMeta {
  return RULE_ENGINE[ruleId] || { engine: 'experience', label: 'الطيار الآلي', desc: 'يُدار بواسطة الطيار الآلي' }
}

// ── Type label ───────────────────────────────────────────────────────────────

const typeLabel = (t: DashboardCoupon['type']) =>
  t === 'percentage' ? 'نسبة مئوية' : 'مبلغ ثابت'

// ── KPIs ─────────────────────────────────────────────────────────────────────

interface KpiTone { fg: string; bg: string }
const TONES: Record<'green' | 'amber' | 'purple' | 'slate', KpiTone> = {
  green:  { fg: 'text-emerald-600', bg: 'bg-emerald-50' },
  amber:  { fg: 'text-amber-600',   bg: 'bg-amber-50'   },
  purple: { fg: 'text-purple-600',  bg: 'bg-purple-50'  },
  slate:  { fg: 'text-slate-600',   bg: 'bg-slate-100'  },
}

function KpiCard({ label, value, hint, accent, icon: Icon }: {
  label: string; value: string | number; hint?: string
  accent: keyof typeof TONES; icon: typeof Bot
}) {
  const t = TONES[accent]
  return (
    <div className="card p-4 flex items-start gap-3">
      <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${t.bg} ${t.fg} shrink-0`}>
        <Icon className="w-5 h-5" />
      </div>
      <div className="min-w-0">
        <p className="text-[11px] text-slate-500 truncate">{label}</p>
        <p className="text-lg font-bold text-slate-900 mt-0.5">{value}</p>
        {hint && <p className="text-[10px] text-slate-400 mt-0.5">{hint}</p>}
      </div>
    </div>
  )
}

// ── Filter tabs ──────────────────────────────────────────────────────────────

type CouponFilter = 'all' | 'ai' | 'manual'

const FILTERS: Array<{ key: CouponFilter; label: string; hint: string }> = [
  { key: 'all',    label: 'كل الكوبونات',         hint: 'عرض الكل' },
  { key: 'ai',     label: '🤖 توليد ذكي',         hint: 'الكوبونات التي يولّدها الطيار الآلي' },
  { key: 'manual', label: '✋ يدوي (استثنائي)',    hint: 'الكوبونات التي أنشأتها يدوياً' },
]

const TABLE_HEADERS = ['الكود', 'المصدر', 'النوع', 'الخصم', 'الاستخدامات', 'الانتهاء', 'الحالة', '']

// ─────────────────────────────────────────────────────────────────────────────

export default function Coupons() {
  const [data, setData] = useState<CouponsDashboard>(emptyData)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<CouponFilter>('all')
  const [editingRule, setEditingRule] = useState<CouponRule | null>(null)
  const { t } = useLanguage()

  const load = () => {
    featureRealityApi.coupons()
      .then(setData)
      .catch(() => setData(emptyData))
  }

  useEffect(() => { load() }, [])

  const persistRules = async (nextRules: CouponRule[]) => {
    setData(rs => ({ ...rs, rules: nextRules }))
    try {
      const saved = await featureRealityApi.saveCouponSettings({
        rules: nextRules,
        vip_tiers: data.vip_tiers,
      })
      setData(rs => ({ ...rs, rules: saved.rules, vip_tiers: saved.vip_tiers }))
    } catch {
      load()
      alert('تعذّر حفظ إعدادات القواعد')
    }
  }

  const toggleRule = (id: string) => {
    const nextRules = data.rules.map(r => r.id === id ? { ...r, enabled: !r.enabled } : r)
    return persistRules(nextRules)
  }

  const saveRule = (updated: CouponRule) => {
    const nextRules = data.rules.map(r => r.id === updated.id ? { ...r, ...updated } : r)
    return persistRules(nextRules)
  }

  const handleCreateCoupon = async () => {
    if (!window.confirm(
      'الكوبونات اليدوية مخصّصة للحالات الاستثنائية فقط.\n'
      + 'في الوضع الطبيعي، الطيار الآلي يولّد الأكواد تلقائياً عبر القواعد والعروض.\n\n'
      + 'هل تريد المتابعة وإنشاء كود يدوي؟',
    )) return
    const code = window.prompt('أدخل كود الكوبون')
    if (!code) return
    const type = (window.prompt('نوع الخصم: percentage أو fixed', 'percentage') || 'percentage') as 'percentage' | 'fixed'
    const value = window.prompt('قيمة الخصم')
    if (!value) return
    try {
      await featureRealityApi.createCoupon({
        code,
        type,
        value,
        category: 'standard',
        active: true,
      })
      load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر إنشاء الكوبون')
    }
  }

  const handleToggleCoupon = async (coupon: DashboardCoupon) => {
    try {
      await featureRealityApi.updateCoupon(coupon.id, { active: !coupon.active })
      load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر تحديث الكوبون')
    }
  }

  const handleDeleteCoupon = async (coupon: DashboardCoupon) => {
    if (!window.confirm(`حذف الكوبون ${coupon.code}؟`)) return
    try {
      await featureRealityApi.deleteCoupon(coupon.id)
      load()
    } catch (e) {
      alert(e instanceof Error ? e.message : 'تعذّر حذف الكوبون')
    }
  }

  const copyCode = (code: string, id: string) => {
    navigator.clipboard.writeText(code)
    setCopiedId(id)
    setTimeout(() => setCopiedId(null), 1500)
  }

  // ── KPIs ────────────────────────────────────────────────────────────────────

  const kpis = useMemo(() => {
    const total       = data.coupons.length
    const active      = data.coupons.filter(c => c.active).length
    const aiGenerated = data.coupons.filter(c => originOf(c) !== 'manual').length
    const manual      = total - aiGenerated
    const totalUses   = data.coupons.reduce((sum, c) => sum + (c.usages || 0), 0)
    const aiPct       = total > 0 ? Math.round((aiGenerated / total) * 100) : 0
    return { total, active, aiGenerated, manual, totalUses, aiPct }
  }, [data.coupons])

  const filteredCoupons = useMemo(() => {
    if (filter === 'all')    return data.coupons
    if (filter === 'manual') return data.coupons.filter(c => originOf(c) === 'manual')
    return data.coupons.filter(c => originOf(c) !== 'manual')
  }, [data.coupons, filter])

  return (
    <div className="space-y-5">
      <PageHeader
        title={t(tr => tr.pages.coupons.title)}
        subtitle={t(tr => tr.pages.coupons.subtitle)}
        action={
          <button
            className="text-xs px-3 py-2 rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50 inline-flex items-center gap-1.5"
            onClick={handleCreateCoupon}
            title="استخدم فقط في الحالات الاستثنائية — في الوضع الطبيعي يتولّى الطيار الآلي توليد الأكواد"
          >
            <Plus className="w-3.5 h-3.5" /> كود يدوي
          </button>
        }
      />

      {/* AI banner — sets the tone: this is an AI-managed system */}
      <AiBanner
        title="نحلة تُولّد الكوبونات وترسلها تلقائياً"
        body="أنت تحدّد القواعد، الحدود، نسبة الخصم، والشروط. الطيار الآلي يتولّى الباقي: متى يُصدر الكوبون، لمن، وعبر أي حملة. الكوبونات اليدوية أعلاه مخصّصة للحالات الاستثنائية فقط."
        bullets={[
          'كل قاعدة تنتمي لمحرك (استرجاع / نمو / تجربة)',
          'كل كود يُسجَّل بمصدره: ذكاء أم يدوي',
          'الطيار الآلي يحترم الحد الأقصى للخصم في الإعدادات',
          'يمكنك تعطيل أي قاعدة بدون التأثير على الباقي',
        ]}
      />

      {/* KPIs */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <KpiCard label="إجمالي الكوبونات"     value={kpis.total}              accent="slate"  icon={Tag} />
        <KpiCard label="نشطة الآن"            value={kpis.active}             accent="green"  icon={ShieldCheck} />
        <KpiCard
          label="مولّدة بواسطة الذكاء"
          value={`${kpis.aiGenerated}`}
          hint={`${kpis.aiPct}% من الإجمالي`}
          accent="purple"
          icon={Bot}
        />
        <KpiCard label="استخدامات إجمالية"    value={kpis.totalUses}          accent="amber"  icon={TrendingUp} />
      </div>

      {/* Coupon Rules — primary section, framed as AI rules */}
      <div className="card">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-slate-900 flex items-center gap-2">
              <Zap className="w-4 h-4 text-amber-500" />
              قواعد توليد الكوبونات الذكي
            </h2>
            <p className="text-xs text-slate-500 mt-0.5">
              فعّل أي قاعدة لتسمح للطيار الآلي بتوليد كوبونات شخصية وإرسالها ضمن حملاتها
            </p>
          </div>
          <Link
            to="/smart-automations"
            className="text-[11px] text-brand-600 hover:text-brand-700 font-medium hidden sm:inline-flex items-center gap-1"
          >
            <Bot className="w-3 h-3" /> إدارة الحملات
          </Link>
        </div>
        <ul className="divide-y divide-slate-100">
          {data.rules.length === 0 && (
            <li className="px-5 py-6 text-center text-xs text-slate-400">
              لا توجد قواعد بعد — ستظهر هنا تلقائياً عند تفعيل أتمتة من صفحة الطيار الآلي
            </li>
          )}
          {data.rules.map((rule) => {
            const meta = engineMeta(rule.id)
            const dt = rule.discount_type ?? 'percentage'
            const dv = rule.discount_value ?? 10
            const valueLabel = dt === 'percentage' ? `${dv}%` : `${dv} ر.س`
            return (
              <li key={rule.id} className="px-5 py-4">
                <div className="flex items-start justify-between gap-3 flex-wrap">
                  <div className="flex items-start gap-3 min-w-0 flex-1">
                    <div className={`w-9 h-9 rounded-lg flex items-center justify-center shrink-0 ${
                      meta.engine === 'recovery' ? 'bg-emerald-50 text-emerald-600' :
                      meta.engine === 'growth'   ? 'bg-blue-50 text-blue-600'       :
                                                    'bg-purple-50 text-purple-600'
                    }`}>
                      <Zap className="w-4 h-4" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 flex-wrap">
                        <p className="text-sm font-semibold text-slate-900">{rule.label}</p>
                        <Badge label={meta.label} variant={ENGINE_VARIANT[meta.engine]} />
                      </div>
                      {rule.description && (
                        <p className="text-[11px] text-slate-500 mt-1 leading-relaxed">{rule.description}</p>
                      )}
                      {/* Live parameter chips */}
                      <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                        <span className="inline-flex items-center gap-1 text-[11px] text-slate-700 bg-slate-100 px-2 py-0.5 rounded-md">
                          {dt === 'percentage' ? <Percent className="w-3 h-3" /> : <Coins className="w-3 h-3" />}
                          خصم {valueLabel}
                        </span>
                        <span className="inline-flex items-center gap-1 text-[11px] text-slate-700 bg-slate-100 px-2 py-0.5 rounded-md">
                          <Clock className="w-3 h-3" />
                          صلاحية {rule.validity_days ?? 1} {(rule.validity_days ?? 1) === 1 ? 'يوم' : 'أيام'}
                        </span>
                        {(rule.min_order_amount ?? 0) > 0 && (
                          <span className="inline-flex items-center gap-1 text-[11px] text-slate-700 bg-slate-100 px-2 py-0.5 rounded-md">
                            حد أدنى {rule.min_order_amount} ر.س
                          </span>
                        )}
                        {rule.max_uses && (
                          <span className="inline-flex items-center gap-1 text-[11px] text-slate-700 bg-slate-100 px-2 py-0.5 rounded-md">
                            استخدامات {rule.max_uses}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      onClick={() => setEditingRule(rule)}
                      className="text-xs px-2.5 py-1.5 rounded-lg border border-slate-200 text-slate-600 hover:bg-slate-50 inline-flex items-center gap-1"
                      title="تعديل القاعدة"
                    >
                      <Pencil className="w-3.5 h-3.5" /> تعديل
                    </button>
                    <button
                      onClick={() => toggleRule(rule.id)}
                      title={rule.enabled ? 'تعطيل القاعدة' : 'تفعيل القاعدة'}
                    >
                      {rule.enabled
                        ? <ToggleRight className="w-7 h-7 text-brand-500" />
                        : <ToggleLeft  className="w-7 h-7 text-slate-300" />}
                    </button>
                  </div>
                </div>
              </li>
            )
          })}
        </ul>
      </div>

      {/* Rule edit modal */}
      <RuleEditorModal
        rule={editingRule}
        onClose={() => setEditingRule(null)}
        onSave={async updated => {
          await saveRule(updated)
          setEditingRule(null)
        }}
      />

      {/* VIP Tiers */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900 flex items-center gap-2">
            <Crown className="w-4 h-4 text-amber-500" /> مستويات خصم VIP
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            الطيار الآلي يصنّف العملاء تلقائياً ويُصدر الكود المناسب لكل مستوى
          </p>
        </div>
        <div className="grid sm:grid-cols-3 divide-y sm:divide-y-0 sm:divide-x sm:divide-x-reverse divide-slate-100">
          {data.vip_tiers.map(({ tier, threshold, discount }, idx) => {
            const color = idx === 0 ? 'text-slate-500 bg-slate-50' : idx === 1 ? 'text-amber-600 bg-amber-50' : 'text-purple-600 bg-purple-50'
            return (
              <div key={tier} className={`flex flex-col items-center py-6 ${color.split(' ')[1]}`}>
                <span className={`text-xs font-bold uppercase tracking-widest ${color.split(' ')[0]}`}>{tier}</span>
                <p className={`text-3xl font-bold mt-2 ${color.split(' ')[0]}`}>{discount}</p>
                <p className="text-xs text-slate-500 mt-1">{threshold}</p>
              </div>
            )
          })}
        </div>
      </div>

      {/* Coupons table — with origin column */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <h2 className="text-sm font-semibold text-slate-900">الأكواد الصادرة</h2>
            <div className="flex flex-wrap items-center gap-1 bg-slate-100 p-1 rounded-lg">
              {FILTERS.map(f => {
                const active = filter === f.key
                return (
                  <button
                    key={f.key}
                    onClick={() => setFilter(f.key)}
                    title={f.hint}
                    className={`text-[11px] px-2.5 py-1 rounded-md transition ${
                      active
                        ? 'bg-white text-slate-900 shadow-sm font-semibold'
                        : 'text-slate-600 hover:text-slate-800'
                    }`}
                  >
                    {f.label}
                  </button>
                )
              })}
            </div>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100">
                {TABLE_HEADERS.map((h) => (
                  <th key={h} className="text-start px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide whitespace-nowrap">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filteredCoupons.length === 0 && (
                <tr>
                  <td colSpan={TABLE_HEADERS.length} className="px-5 py-12 text-center text-sm text-slate-400">
                    لا توجد كوبونات في هذا التصنيف
                  </td>
                </tr>
              )}
              {filteredCoupons.map((c) => {
                const origin = originOf(c)
                const om = ORIGIN_META[origin]
                const OIcon = om.icon
                return (
                  <tr key={c.id} className="hover:bg-slate-50 transition-colors">
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-2">
                        <OIcon className={`w-3.5 h-3.5 shrink-0 ${
                          om.variant === 'purple' ? 'text-purple-500' :
                          om.variant === 'amber'  ? 'text-amber-500'  :
                          om.variant === 'blue'   ? 'text-blue-500'   :
                                                    'text-slate-400'
                        }`} />
                        <span className="text-xs font-mono font-semibold text-slate-800" dir="ltr">{c.code}</span>
                        <button
                          onClick={() => copyCode(c.code, c.id)}
                          className="text-slate-300 hover:text-slate-500 transition-colors"
                        >
                          <Copy className="w-3 h-3" />
                        </button>
                        {copiedId === c.id && <span className="text-xs text-emerald-600">تم النسخ!</span>}
                      </div>
                    </td>
                    <td className="px-5 py-3.5" title={om.hint}>
                      <Badge label={om.label} variant={om.variant} />
                    </td>
                    <td className="px-5 py-3.5 text-xs text-slate-600">{typeLabel(c.type)}</td>
                    <td className="px-5 py-3.5 text-xs font-semibold text-slate-900">
                      {c.type === 'percentage' ? `${c.value}%` : `${c.value} ر.س`}
                    </td>
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-2">
                        <div className="flex-1 bg-slate-100 rounded-full h-1.5 w-20">
                          <div
                            className="bg-brand-500 h-1.5 rounded-full"
                            style={{ width: `${c.limit > 0 ? Math.min((c.usages / c.limit) * 100, 100) : 0}%` }}
                          />
                        </div>
                        <span className="text-xs text-slate-500">{c.usages}/{c.limit || '∞'}</span>
                      </div>
                    </td>
                    <td className="px-5 py-3.5 text-xs text-slate-500" dir="ltr">{c.expires}</td>
                    <td className="px-5 py-3.5">
                      <button onClick={() => handleToggleCoupon(c)}>
                        <Badge label={c.active ? 'نشط' : 'غير نشط'} variant={c.active ? 'green' : 'slate'} dot />
                      </button>
                    </td>
                    <td className="px-5 py-3.5">
                      <button className="text-slate-300 hover:text-red-500 transition-colors" onClick={() => handleDeleteCoupon(c)}>
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Footer hint — directs the merchant where to manage strategy */}
      <div className="text-center text-xs text-slate-500 py-2">
        لإدارة الحملات التي تستخدم هذه الكوبونات تلقائياً، انتقل إلى{' '}
        <Link to="/smart-automations" className="text-brand-600 hover:underline font-medium">الطيار الآلي</Link>
        {' '}— ولتعريف العروض التلقائية بدون كود انتقل إلى{' '}
        <Link to="/promotions" className="text-brand-600 hover:underline font-medium">العروض</Link>.
      </div>
    </div>
  )
}

// ── Rule editor modal ────────────────────────────────────────────────────────
//
// Shown when the merchant clicks "تعديل" on a rule. Edits all the parameters
// the AI will read at runtime: discount type/value, validity window, minimum
// order amount, max uses per coupon, and the on/off switch.

interface RuleEditorProps {
  rule:    CouponRule | null
  onClose: () => void
  onSave:  (rule: CouponRule) => void | Promise<void>
}

function RuleEditorModal({ rule, onClose, onSave }: RuleEditorProps) {
  const [draft, setDraft] = useState<CouponRule | null>(null)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  // Sync draft whenever a new rule starts editing.
  useEffect(() => {
    if (rule) {
      setDraft({
        ...rule,
        discount_type:    rule.discount_type    ?? 'percentage',
        discount_value:   rule.discount_value   ?? 10,
        validity_days:    rule.validity_days    ?? 1,
        min_order_amount: rule.min_order_amount ?? 0,
        max_uses:         rule.max_uses         ?? 1,
        description:      rule.description      ?? '',
      })
      setErr(null)
    } else {
      setDraft(null)
    }
  }, [rule])

  if (!rule || !draft) return null

  const isPct = draft.discount_type === 'percentage'

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    setErr(null)
    if ((draft.discount_value ?? 0) <= 0) {
      setErr('قيمة الخصم يجب أن تكون أكبر من صفر')
      return
    }
    if (isPct && (draft.discount_value ?? 0) > 100) {
      setErr('نسبة الخصم لا يمكن أن تتجاوز 100%')
      return
    }
    if ((draft.validity_days ?? 1) < 1) {
      setErr('مدة الصلاحية يجب أن تكون يوماً واحداً على الأقل')
      return
    }
    setSaving(true)
    try {
      await onSave(draft)
    } catch (e2) {
      setErr(e2 instanceof Error ? e2.message : 'تعذّر الحفظ')
    } finally {
      setSaving(false)
    }
  }

  const meta = engineMeta(draft.id)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40 backdrop-blur-sm">
      <div className="card w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-100">
          <div className="min-w-0">
            <h2 className="text-base font-bold text-slate-900 truncate">تعديل القاعدة</h2>
            <p className="text-xs text-slate-500 mt-0.5 truncate">{draft.label}</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-700">
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSave} className="p-5 space-y-4">
          <div className="rounded-lg border border-amber-200/70 bg-amber-50/60 px-3 py-2.5">
            <div className="flex items-start gap-2">
              <Bot className="w-4 h-4 text-amber-600 mt-0.5 shrink-0" />
              <div className="min-w-0">
                <p className="text-xs font-semibold text-amber-900">قاعدة يُنفّذها الطيار الآلي</p>
                <p className="text-[11px] text-amber-800/80 leading-relaxed mt-0.5">
                  {meta.label} — {meta.desc}. أنت تحدّد المعايير، ونحلة تختار التوقيت والعميل.
                </p>
              </div>
            </div>
          </div>

          {/* Description */}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">الوصف</label>
            <textarea
              value={draft.description ?? ''}
              onChange={e => setDraft({ ...draft, description: e.target.value })}
              rows={2}
              placeholder="ملاحظات داخلية للتاجر فقط"
              className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none resize-none"
            />
          </div>

          {/* Discount type */}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">نوع الخصم *</label>
            <div className="grid grid-cols-2 gap-2">
              {(['percentage', 'fixed'] as const).map(k => {
                const active = draft.discount_type === k
                const Icon = k === 'percentage' ? Percent : Coins
                const lbl  = k === 'percentage' ? 'نسبة مئوية' : 'مبلغ ثابت'
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => setDraft({ ...draft, discount_type: k })}
                    className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition ${
                      active
                        ? 'border-brand-400 bg-brand-50 text-brand-700'
                        : 'border-slate-200 hover:border-slate-300 text-slate-700'
                    }`}
                  >
                    <Icon className="w-4 h-4 shrink-0" />
                    {lbl}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Discount value */}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              قيمة الخصم * {isPct ? '(%)' : '(ر.س)'}
            </label>
            <input
              type="number"
              step={isPct ? '1' : '0.01'}
              min={0}
              max={isPct ? 100 : undefined}
              value={draft.discount_value ?? 0}
              onChange={e => setDraft({ ...draft, discount_value: Number(e.target.value) })}
              className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
            />
            {isPct && (
              <p className="text-[11px] text-slate-500 mt-1">
                ملاحظة: الحد الأقصى للخصم في الإعدادات يُطبَّق تلقائياً عند التوليد.
              </p>
            )}
          </div>

          {/* Validity + min order */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                مدة الصلاحية (أيام) *
              </label>
              <input
                type="number"
                min={1}
                value={draft.validity_days ?? 1}
                onChange={e => setDraft({ ...draft, validity_days: Math.max(1, Number(e.target.value)) })}
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-700 mb-1">
                الحد الأدنى للطلب (ر.س)
              </label>
              <input
                type="number"
                min={0}
                step="0.01"
                value={draft.min_order_amount ?? 0}
                onChange={e => setDraft({ ...draft, min_order_amount: Number(e.target.value) })}
                placeholder="0 = بدون شرط"
                className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
              />
            </div>
          </div>

          {/* Max uses */}
          <div>
            <label className="block text-xs font-medium text-slate-700 mb-1">
              الحد الأقصى للاستخدام (لكل كود)
            </label>
            <input
              type="number"
              min={1}
              value={draft.max_uses ?? 1}
              onChange={e => setDraft({
                ...draft,
                max_uses: e.target.value ? Math.max(1, Number(e.target.value)) : null,
              })}
              placeholder="1 = استخدام واحد فقط"
              className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none"
            />
          </div>

          {/* Enable/disable */}
          <label className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg border border-slate-200">
            <div>
              <p className="text-sm font-medium text-slate-900">القاعدة مفعّلة</p>
              <p className="text-[11px] text-slate-500 mt-0.5">
                عند التعطيل، لن يُولِّد الطيار الآلي كوبونات لهذه القاعدة
              </p>
            </div>
            <button
              type="button"
              onClick={() => setDraft({ ...draft, enabled: !draft.enabled })}
              className="shrink-0"
            >
              {draft.enabled
                ? <ToggleRight className="w-7 h-7 text-brand-500" />
                : <ToggleLeft  className="w-7 h-7 text-slate-300" />}
            </button>
          </label>

          {err && <p className="text-xs text-red-600">{err}</p>}

          <div className="flex items-center justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="text-sm px-4 py-2 rounded-lg text-slate-600 hover:bg-slate-100"
            >
              إلغاء
            </button>
            <button
              type="submit"
              disabled={saving}
              className="btn-primary text-sm disabled:opacity-50"
            >
              {saving ? 'جارٍ الحفظ…' : 'حفظ التغييرات'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
