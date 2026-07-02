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
  Settings,
  ChevronDown,
  ChevronUp,
  Award,
  Brain,
  Server,
  Save,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import AiBanner from '../components/ui/AiBanner'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  featureRealityApi,
  type CouponAiPolicy,
  type CouponChannel,
  type CouponGlobalDefaults,
  type CouponLevel,
  type CouponLevelId,
  type CouponOrigin,
  type CouponPoolMode,
  type CouponRule,
  type CouponValidityPreset,
  type CouponsDashboard,
  type DashboardCoupon,
} from '../api/featureReality'

const DEFAULT_LEVELS: CouponLevel[] = [
  { id: 'bronze', label: 'برونزي',   threshold: '+1 طلب',   discount_default: 5,  discount_min: 3,  discount_max: 5,  validity_hours: 24, max_uses: 1, per_customer_usage: 1, allowed_channels: ['ai', 'campaign', 'autopilot'], enabled: true },
  { id: 'silver', label: 'فضي',      threshold: '+3 طلبات', discount_default: 10, discount_min: 8,  discount_max: 12, validity_hours: 48, max_uses: 1, per_customer_usage: 1, allowed_channels: ['ai', 'campaign', 'autopilot'], enabled: true },
  { id: 'gold',   label: 'ذهبي',     threshold: '+7 طلبات', discount_default: 20, discount_min: 15, discount_max: 25, validity_hours: 72, max_uses: 2, per_customer_usage: 1, allowed_channels: ['campaign', 'autopilot'],       enabled: true },
  { id: 'vip',    label: 'استثنائي', threshold: '+15 طلب',  discount_default: 30, discount_min: 25, discount_max: 40, validity_hours: 72, max_uses: 3, per_customer_usage: 1, allowed_channels: ['campaign', 'autopilot'],       enabled: true },
]

const DEFAULT_GLOBAL_DEFAULTS: CouponGlobalDefaults = {
  discount_type: 'percentage',
  default_discount_value: 10,
  total_usage_limit: null,
  customer_limit: null,
  per_customer_usage: 1,
  min_order_amount: 0,
  default_validity: '24h',
  custom_validity_hours: null,
  allowed_channels: ['ai', 'campaign', 'autopilot'],
  combinable_with_offers: false,
}

const DEFAULT_AI_POLICY: CouponAiPolicy = {
  enabled: true,
  allowed_levels: ['bronze', 'silver'],
  min_remaining_hours: 3,
  pool_mode: 'pool_first',
}

const emptyData: CouponsDashboard = {
  rules: [],
  vip_tiers: [],
  levels: DEFAULT_LEVELS,
  global_defaults: DEFAULT_GLOBAL_DEFAULTS,
  ai_policy: DEFAULT_AI_POLICY,
  coupons: [],
}

// ── Level metadata ───────────────────────────────────────────────────────────

const LEVEL_META: Record<CouponLevelId, { label: string; bg: string; text: string; ring: string; iconBg: string; }> = {
  bronze: { label: 'برونزي',   bg: 'bg-orange-50/60',  text: 'text-orange-700', ring: 'ring-orange-200',  iconBg: 'bg-orange-100 text-orange-600' },
  silver: { label: 'فضي',      bg: 'bg-slate-50',      text: 'text-slate-700',  ring: 'ring-slate-200',   iconBg: 'bg-slate-100 text-slate-600' },
  gold:   { label: 'ذهبي',     bg: 'bg-amber-50/60',   text: 'text-amber-700',  ring: 'ring-amber-200',   iconBg: 'bg-amber-100 text-amber-600' },
  vip:    { label: 'استثنائي', bg: 'bg-purple-50/60',  text: 'text-purple-700', ring: 'ring-purple-200',  iconBg: 'bg-purple-100 text-purple-600' },
}

const LEVEL_BADGE_VARIANT: Record<CouponLevelId, 'amber' | 'slate' | 'purple' | 'blue'> = {
  bronze: 'amber',
  silver: 'slate',
  gold:   'amber',
  vip:    'purple',
}

const SOURCE_TYPE_LABEL: Record<NonNullable<DashboardCoupon['source_type']>, { label: string; variant: 'green' | 'slate' | 'blue' }> = {
  system:   { label: 'نظام',           variant: 'green' },
  manual:   { label: 'يدوي',           variant: 'slate' },
  imported: { label: 'مستورد من سلة',  variant: 'blue'  },
}

const SYNC_BADGE_VARIANT: Record<NonNullable<DashboardCoupon['sync_badge']>, 'green' | 'slate' | 'red' | 'blue'> = {
  synced:     'green',
  not_pushed: 'slate',
  failed:     'red',
  imported:   'blue',
}

const CHANNEL_LABEL: Record<CouponChannel, { label: string; variant: 'purple' | 'blue' | 'amber' | 'slate' }> = {
  ai:        { label: 'ذكاء',      variant: 'purple' },
  campaign:  { label: 'حملة',      variant: 'blue'   },
  autopilot: { label: 'طيار آلي',  variant: 'amber'  },
  shared:    { label: 'مشترك',     variant: 'slate'  },
}

function formatRemaining(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  if (seconds <= 0) return 'منتهي'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days >= 1) return `${days} يوم${days === 1 ? '' : ''}${hours ? ` و${hours} س` : ''}`
  if (hours >= 1) return `${hours} ساعة${minutes ? ` و${minutes} د` : ''}`
  return `${minutes} دقيقة`
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

type CouponFilter = 'all' | 'system' | 'manual' | 'imported'

const FILTERS: Array<{ key: CouponFilter; label: string; hint: string }> = [
  { key: 'all',      label: 'كل الكوبونات',         hint: 'عرض الكل' },
  { key: 'system',   label: '🤖 من النظام',          hint: 'الكوبونات التي يولّدها النظام تلقائياً' },
  { key: 'manual',   label: '✋ يدوي',                hint: 'الكوبونات التي أنشأتها يدوياً' },
  { key: 'imported', label: '⤓ مستورد',              hint: 'كوبونات مستوردة من سلة أو زد' },
]

type LevelFilter = 'all' | CouponLevelId

const LEVEL_FILTERS: Array<{ key: LevelFilter; label: string }> = [
  { key: 'all',    label: 'جميع المستويات' },
  { key: 'bronze', label: 'برونزي'   },
  { key: 'silver', label: 'فضي'      },
  { key: 'gold',   label: 'ذهبي'     },
  { key: 'vip',    label: 'استثنائي' },
]

const TABLE_HEADERS = ['الكود', 'المستوى', 'المصدر', 'سلة', 'القناة', 'الخصم', 'الاستخدامات', 'المتبقي', 'الحالة', '']

function sourceTypeOf(c: DashboardCoupon): NonNullable<DashboardCoupon['source_type']> {
  if (c.source_type) return c.source_type
  const o = c.origin
  if (o === 'manual') return 'manual'
  if (o) return 'system'
  return c.category === 'auto' ? 'system' : 'manual'
}

// ─────────────────────────────────────────────────────────────────────────────

export default function Coupons() {
  const [data, setData] = useState<CouponsDashboard>(emptyData)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [filter, setFilter] = useState<CouponFilter>('all')
  const [levelFilter, setLevelFilter] = useState<LevelFilter>('all')
  const [editingRule, setEditingRule] = useState<CouponRule | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const { t } = useLanguage()

  const levels = data.levels && data.levels.length === 4 ? data.levels : DEFAULT_LEVELS
  const globalDefaults = data.global_defaults || DEFAULT_GLOBAL_DEFAULTS
  const aiPolicy = data.ai_policy || DEFAULT_AI_POLICY

  const load = () => {
    featureRealityApi.coupons()
      .then(d => setData({
        ...d,
        levels:          (d.levels && d.levels.length === 4 ? d.levels : DEFAULT_LEVELS),
        global_defaults: d.global_defaults || DEFAULT_GLOBAL_DEFAULTS,
        ai_policy:       d.ai_policy       || DEFAULT_AI_POLICY,
      }))
      .catch(() => setData(emptyData))
  }

  useEffect(() => { load() }, [])

  const persistSettings = async (patch: Partial<CouponsDashboard>) => {
    const next = { ...data, ...patch }
    setData(next)
    try {
      const saved = await featureRealityApi.saveCouponSettings({
        rules:           next.rules,
        vip_tiers:       next.vip_tiers,
        levels:          next.levels,
        global_defaults: next.global_defaults,
        ai_policy:       next.ai_policy,
      })
      setData(prev => ({
        ...prev,
        rules:           saved.rules           ?? prev.rules,
        vip_tiers:       saved.vip_tiers       ?? prev.vip_tiers,
        levels:          saved.levels          ?? prev.levels,
        global_defaults: saved.global_defaults ?? prev.global_defaults,
        ai_policy:       saved.ai_policy       ?? prev.ai_policy,
      }))
    } catch {
      load()
      alert('تعذّر حفظ الإعدادات')
    }
  }

  const persistRules = (nextRules: CouponRule[]) =>
    persistSettings({ rules: nextRules })

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
    let arr = data.coupons
    if (filter !== 'all') {
      arr = arr.filter(c => sourceTypeOf(c) === filter)
    }
    if (levelFilter !== 'all') {
      arr = arr.filter(c => (c.coupon_level || 'silver') === levelFilter)
    }
    return arr
  }, [data.coupons, filter, levelFilter])

  // KPI counts per source so the merchant sees the breakdown at a glance.
  const sourceCounts = useMemo(() => {
    const out = { all: data.coupons.length, system: 0, manual: 0, imported: 0 }
    for (const c of data.coupons) {
      const s = sourceTypeOf(c)
      out[s] = (out[s] || 0) + 1
    }
    return out
  }, [data.coupons])

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

      {/* ── Settings panel ───────────────────────────────────────────── */}
      <CouponSettingsPanel
        open={settingsOpen}
        onToggle={() => setSettingsOpen(s => !s)}
        levels={levels}
        globalDefaults={globalDefaults}
        aiPolicy={aiPolicy}
        onSave={persistSettings}
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

      {/* 4-tier coupon levels */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900 flex items-center gap-2">
            <Crown className="w-4 h-4 text-amber-500" /> مستويات الكوبونات
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            أربعة مستويات يصنّف الذكاء العميل بينها — كل مستوى له نسبة وصلاحية واستخدامات خاصة
          </p>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 divide-y lg:divide-y-0 lg:divide-x lg:divide-x-reverse divide-slate-100">
          {levels.map(level => {
            const m = LEVEL_META[level.id]
            const range = level.discount_min === level.discount_max
              ? `${level.discount_default}%`
              : `${level.discount_min}–${level.discount_max}%`
            return (
              <div key={level.id} className={`flex flex-col items-center py-6 px-4 ${m.bg}`}>
                <div className={`w-10 h-10 rounded-full flex items-center justify-center mb-2 ${m.iconBg}`}>
                  <Award className="w-5 h-5" />
                </div>
                <span className={`text-xs font-bold tracking-wide ${m.text}`}>{m.label}</span>
                <p className={`text-2xl font-bold mt-1 ${m.text}`}>{range}</p>
                <p className="text-[11px] text-slate-500 mt-1">{level.threshold}</p>
                <p className="text-[10px] text-slate-400 mt-2">صلاحية {level.validity_hours} ساعة</p>
              </div>
            )
          })}
        </div>
      </div>

      {/* Coupons table — with origin column */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <h2 className="text-sm font-semibold text-slate-900">الأكواد الصادرة</h2>
            <div className="flex flex-wrap items-center gap-1 bg-slate-100 p-1 rounded-lg">
              {FILTERS.map(f => {
                const active = filter === f.key
                const count = sourceCounts[f.key as keyof typeof sourceCounts] ?? 0
                return (
                  <button
                    key={f.key}
                    onClick={() => setFilter(f.key)}
                    title={f.hint}
                    className={`text-[11px] px-2.5 py-1 rounded-md transition inline-flex items-center gap-1.5 ${
                      active
                        ? 'bg-white text-slate-900 shadow-sm font-semibold'
                        : 'text-slate-600 hover:text-slate-800'
                    }`}
                  >
                    <span>{f.label}</span>
                    <span className={`text-[10px] px-1.5 rounded-full ${active ? 'bg-slate-100 text-slate-600' : 'bg-white/60 text-slate-500'}`}>
                      {count}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            {LEVEL_FILTERS.map(lf => {
              const active = levelFilter === lf.key
              return (
                <button
                  key={lf.key}
                  onClick={() => setLevelFilter(lf.key)}
                  className={`text-[11px] px-2.5 py-1 rounded-full border transition ${
                    active
                      ? 'border-brand-400 bg-brand-50 text-brand-700 font-semibold'
                      : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                  }`}
                >
                  {lf.label}
                </button>
              )
            })}
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
                const sourceType = sourceTypeOf(c)
                const sMeta = SOURCE_TYPE_LABEL[sourceType]
                const level = c.coupon_level || null
                const channel = c.allocation_channel || null
                const isExpired = c.remaining_seconds != null && c.remaining_seconds <= 0
                const isDepleted = c.limit > 0 && c.usages >= c.limit
                const statusLabel = !c.active ? 'غير نشط' : isExpired ? 'منتهي' : isDepleted ? 'مستنفد' : 'نشط'
                const statusVariant: 'green' | 'slate' | 'red' | 'amber' =
                  !c.active ? 'slate' : isExpired ? 'red' : isDepleted ? 'amber' : 'green'
                return (
                  <tr key={c.id} className="hover:bg-slate-50 transition-colors">
                    <td className="px-5 py-3.5">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-mono font-semibold text-slate-800" dir="ltr">{c.code}</span>
                        <button
                          onClick={() => copyCode(c.code, c.id)}
                          className="text-slate-300 hover:text-slate-500 transition-colors"
                          title="نسخ الكود"
                        >
                          <Copy className="w-3 h-3" />
                        </button>
                        {copiedId === c.id && <span className="text-xs text-emerald-600">تم النسخ!</span>}
                      </div>
                    </td>
                    <td className="px-5 py-3.5">
                      {level
                        ? <Badge label={LEVEL_META[level].label} variant={LEVEL_BADGE_VARIANT[level]} />
                        : <span className="text-xs text-slate-400">—</span>}
                    </td>
                    <td className="px-5 py-3.5" title={om.hint}>
                      <Badge label={c.source_label || sMeta.label} variant={sMeta.variant} />
                    </td>
                    <td className="px-5 py-3.5">
                      {c.sync_badge_label ? (
                        <div className="flex flex-col gap-0.5">
                          <Badge
                            label={c.sync_badge_label}
                            variant={SYNC_BADGE_VARIANT[c.sync_badge || 'not_pushed']}
                          />
                          {c.sync_error ? (
                            <span className="text-[10px] text-red-500 max-w-[9rem] truncate" title={c.sync_error}>
                              {c.sync_error}
                            </span>
                          ) : null}
                        </div>
                      ) : (
                        <span className="text-xs text-slate-400">—</span>
                      )}
                    </td>
                    <td className="px-5 py-3.5">
                      {channel
                        ? <Badge label={CHANNEL_LABEL[channel].label} variant={CHANNEL_LABEL[channel].variant} />
                        : <span className="text-xs text-slate-400">—</span>}
                    </td>
                    <td className="px-5 py-3.5 text-xs font-semibold text-slate-900 whitespace-nowrap">
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
                        <span className="text-xs text-slate-500 whitespace-nowrap">{c.usages}/{c.limit || '∞'}</span>
                      </div>
                    </td>
                    <td className="px-5 py-3.5 text-xs text-slate-500 whitespace-nowrap">
                      {formatRemaining(c.remaining_seconds)}
                    </td>
                    <td className="px-5 py-3.5">
                      <button onClick={() => handleToggleCoupon(c)}>
                        <Badge label={statusLabel} variant={statusVariant} dot />
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

// ── Coupon settings panel ────────────────────────────────────────────────────
//
// Collapsible 3-tab panel that drives every coupon the system generates.
// The merchant sets defaults once here; every level / AI request inherits
// from these unless overridden at the level layer.

type SettingsTab = 'global' | 'levels' | 'ai'

interface SettingsPanelProps {
  open: boolean
  onToggle: () => void
  levels: CouponLevel[]
  globalDefaults: CouponGlobalDefaults
  aiPolicy: CouponAiPolicy
  onSave: (patch: Partial<CouponsDashboard>) => Promise<void>
}

function CouponSettingsPanel({ open, onToggle, levels, globalDefaults, aiPolicy, onSave }: SettingsPanelProps) {
  const [tab, setTab] = useState<SettingsTab>('global')
  const [draftGlobal, setDraftGlobal] = useState<CouponGlobalDefaults>(globalDefaults)
  const [draftLevels, setDraftLevels] = useState<CouponLevel[]>(levels)
  const [draftAi, setDraftAi] = useState<CouponAiPolicy>(aiPolicy)
  const [saving, setSaving] = useState(false)
  const [savedAt, setSavedAt] = useState<number | null>(null)

  useEffect(() => { setDraftGlobal(globalDefaults) }, [globalDefaults])
  useEffect(() => { setDraftLevels(levels) }, [levels])
  useEffect(() => { setDraftAi(aiPolicy) }, [aiPolicy])

  const dirty =
    JSON.stringify(draftGlobal) !== JSON.stringify(globalDefaults) ||
    JSON.stringify(draftLevels) !== JSON.stringify(levels) ||
    JSON.stringify(draftAi)     !== JSON.stringify(aiPolicy)

  const handleSave = async () => {
    setSaving(true)
    try {
      await onSave({
        global_defaults: draftGlobal,
        levels: draftLevels,
        ai_policy: draftAi,
      })
      setSavedAt(Date.now())
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card">
      <button
        type="button"
        onClick={onToggle}
        className="w-full flex items-center justify-between gap-3 px-5 py-4 hover:bg-slate-50/50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center">
            <Settings className="w-4 h-4" />
          </div>
          <div className="text-start">
            <h2 className="text-sm font-semibold text-slate-900">إعدادات الكوبونات</h2>
            <p className="text-[11px] text-slate-500">قواعد عامة + 4 مستويات + سياسة الذكاء</p>
          </div>
        </div>
        {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
      </button>

      {open && (
        <div className="border-t border-slate-100">
          <div className="flex items-center gap-1 px-3 pt-3">
            {([
              { key: 'global', label: 'الإعدادات العامة', icon: Server },
              { key: 'levels', label: 'المستويات',         icon: Award  },
              { key: 'ai',     label: 'سياسة الذكاء',      icon: Brain  },
            ] as const).map(t => {
              const active = tab === t.key
              const Icon = t.icon
              return (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={`text-xs px-3 py-2 rounded-t-lg inline-flex items-center gap-1.5 transition ${
                    active
                      ? 'bg-white border border-b-transparent border-slate-200 text-slate-900 font-semibold'
                      : 'text-slate-500 hover:text-slate-700'
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" />
                  {t.label}
                </button>
              )
            })}
          </div>

          <div className="px-5 py-5 border-t border-slate-200">
            {tab === 'global' && (
              <GlobalDefaultsForm value={draftGlobal} onChange={setDraftGlobal} />
            )}
            {tab === 'levels' && (
              <LevelsForm value={draftLevels} onChange={setDraftLevels} />
            )}
            {tab === 'ai' && (
              <AiPolicyForm value={draftAi} onChange={setDraftAi} />
            )}
          </div>

          <div className="px-5 py-3 border-t border-slate-100 bg-slate-50/50 flex items-center justify-end gap-3">
            {savedAt && !dirty && (
              <span className="text-[11px] text-emerald-600">تم الحفظ ✓</span>
            )}
            <button
              type="button"
              onClick={handleSave}
              disabled={!dirty || saving}
              className="btn-primary text-xs inline-flex items-center gap-1.5 disabled:opacity-50"
            >
              <Save className="w-3.5 h-3.5" />
              {saving ? 'جارٍ الحفظ…' : 'حفظ الإعدادات'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

const ALL_CHANNELS: CouponChannel[] = ['ai', 'campaign', 'autopilot']

function GlobalDefaultsForm({ value, onChange }: {
  value: CouponGlobalDefaults
  onChange: (v: CouponGlobalDefaults) => void
}) {
  const set = <K extends keyof CouponGlobalDefaults>(k: K, v: CouponGlobalDefaults[K]) =>
    onChange({ ...value, [k]: v })
  const isCustom = value.default_validity === 'custom'
  return (
    <div className="grid sm:grid-cols-2 gap-4">
      <Field label="نوع الخصم">
        <div className="grid grid-cols-2 gap-2">
          {(['percentage', 'fixed'] as const).map(k => {
            const active = value.discount_type === k
            const Icon = k === 'percentage' ? Percent : Coins
            return (
              <button
                key={k}
                type="button"
                onClick={() => set('discount_type', k)}
                className={`px-3 py-2 rounded-lg border text-sm inline-flex items-center gap-2 ${
                  active ? 'border-brand-400 bg-brand-50 text-brand-700' : 'border-slate-200 text-slate-700'
                }`}
              >
                <Icon className="w-3.5 h-3.5" />
                {k === 'percentage' ? 'نسبة' : 'مبلغ'}
              </button>
            )
          })}
        </div>
      </Field>

      <Field label={`القيمة الافتراضية ${value.discount_type === 'percentage' ? '(%)' : '(ر.س)'}`}>
        <NumberInput
          value={value.default_discount_value}
          onChange={v => set('default_discount_value', v ?? 0)}
          min={0}
          max={value.discount_type === 'percentage' ? 100 : undefined}
        />
      </Field>

      <Field label="الحد الأقصى الإجمالي للاستخدام" hint="فارغ = بلا حد">
        <NumberInput value={value.total_usage_limit} onChange={v => set('total_usage_limit', v)} min={0} nullable />
      </Field>

      <Field label="عدد العملاء المسموح لهم" hint="فارغ = بلا حد">
        <NumberInput value={value.customer_limit} onChange={v => set('customer_limit', v)} min={0} nullable />
      </Field>

      <Field label="استخدامات لكل عميل">
        <NumberInput value={value.per_customer_usage} onChange={v => set('per_customer_usage', v ?? 1)} min={1} />
      </Field>

      <Field label="الحد الأدنى للطلب (ر.س)">
        <NumberInput value={value.min_order_amount} onChange={v => set('min_order_amount', v ?? 0)} min={0} step={0.01} />
      </Field>

      <Field label="الصلاحية الافتراضية">
        <div className="flex flex-wrap gap-1.5">
          {(['3h', '6h', '24h', 'custom'] as CouponValidityPreset[]).map(opt => {
            const active = value.default_validity === opt
            const lbl = opt === '3h' ? '3 ساعات' : opt === '6h' ? '6 ساعات' : opt === '24h' ? '24 ساعة' : 'مخصص'
            return (
              <button
                key={opt}
                type="button"
                onClick={() => set('default_validity', opt)}
                className={`text-xs px-3 py-1.5 rounded-md border ${
                  active ? 'border-brand-400 bg-brand-50 text-brand-700 font-semibold' : 'border-slate-200 text-slate-600'
                }`}
              >
                {lbl}
              </button>
            )
          })}
        </div>
        {isCustom && (
          <div className="mt-2">
            <NumberInput
              value={value.custom_validity_hours}
              onChange={v => set('custom_validity_hours', v)}
              min={1}
              nullable
              placeholder="عدد الساعات"
            />
          </div>
        )}
      </Field>

      <Field label="القنوات المسموح بها">
        <div className="flex flex-wrap gap-1.5">
          {ALL_CHANNELS.map(ch => {
            const checked = value.allowed_channels.includes(ch)
            return (
              <button
                key={ch}
                type="button"
                onClick={() => set('allowed_channels', checked
                  ? value.allowed_channels.filter(c => c !== ch)
                  : [...value.allowed_channels, ch],
                )}
                className={`text-xs px-3 py-1.5 rounded-full border inline-flex items-center gap-1.5 ${
                  checked ? 'border-brand-400 bg-brand-50 text-brand-700 font-semibold' : 'border-slate-200 text-slate-500'
                }`}
              >
                {CHANNEL_LABEL[ch].label}
              </button>
            )
          })}
        </div>
      </Field>

      <Field label="الدمج مع العروض الأخرى" className="sm:col-span-2">
        <Toggle
          on={value.combinable_with_offers}
          onChange={v => set('combinable_with_offers', v)}
          on_label="مسموح بالدمج"
          off_label="غير مسموح بالدمج"
        />
      </Field>
    </div>
  )
}

function LevelsForm({ value, onChange }: {
  value: CouponLevel[]
  onChange: (v: CouponLevel[]) => void
}) {
  const updateLevel = (idx: number, patch: Partial<CouponLevel>) => {
    onChange(value.map((lv, i) => i === idx ? { ...lv, ...patch } : lv))
  }
  return (
    <div className="space-y-4">
      {value.map((lv, idx) => {
        const m = LEVEL_META[lv.id]
        return (
          <div key={lv.id} className={`rounded-xl border p-4 ${m.bg}`}>
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <div className={`w-8 h-8 rounded-full flex items-center justify-center ${m.iconBg}`}>
                  <Award className="w-4 h-4" />
                </div>
                <div>
                  <p className={`text-sm font-bold ${m.text}`}>{m.label}</p>
                  <p className="text-[11px] text-slate-500">{lv.threshold}</p>
                </div>
              </div>
              <Toggle
                on={lv.enabled}
                onChange={v => updateLevel(idx, { enabled: v })}
                on_label="مفعّل"
                off_label="متوقف"
              />
            </div>
            <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
              <Field label="الافتراضي %" small>
                <NumberInput value={lv.discount_default} onChange={v => updateLevel(idx, { discount_default: v ?? 0 })} min={0} max={100} />
              </Field>
              <Field label="من %" small>
                <NumberInput value={lv.discount_min} onChange={v => updateLevel(idx, { discount_min: v ?? 0 })} min={0} max={100} />
              </Field>
              <Field label="إلى %" small>
                <NumberInput value={lv.discount_max} onChange={v => updateLevel(idx, { discount_max: v ?? 0 })} min={0} max={100} />
              </Field>
              <Field label="الصلاحية (ساعة)" small>
                <NumberInput value={lv.validity_hours} onChange={v => updateLevel(idx, { validity_hours: v ?? 1 })} min={1} />
              </Field>
              <Field label="الاستخدامات" small>
                <NumberInput value={lv.max_uses} onChange={v => updateLevel(idx, { max_uses: v ?? 1 })} min={1} />
              </Field>
              <Field label="لكل عميل" small>
                <NumberInput value={lv.per_customer_usage} onChange={v => updateLevel(idx, { per_customer_usage: v ?? 1 })} min={1} />
              </Field>
              <Field label="القنوات" small className="sm:col-span-2">
                <div className="flex flex-wrap gap-1">
                  {ALL_CHANNELS.map(ch => {
                    const checked = lv.allowed_channels.includes(ch)
                    return (
                      <button
                        key={ch}
                        type="button"
                        onClick={() => updateLevel(idx, {
                          allowed_channels: checked
                            ? lv.allowed_channels.filter(c => c !== ch)
                            : [...lv.allowed_channels, ch],
                        })}
                        className={`text-[11px] px-2 py-1 rounded-full border ${
                          checked ? 'border-brand-400 bg-white text-brand-700 font-semibold' : 'border-slate-200 text-slate-500'
                        }`}
                      >
                        {CHANNEL_LABEL[ch].label}
                      </button>
                    )
                  })}
                </div>
              </Field>
            </div>
          </div>
        )
      })}
    </div>
  )
}

function AiPolicyForm({ value, onChange }: {
  value: CouponAiPolicy
  onChange: (v: CouponAiPolicy) => void
}) {
  const set = <K extends keyof CouponAiPolicy>(k: K, v: CouponAiPolicy[K]) =>
    onChange({ ...value, [k]: v })
  return (
    <div className="space-y-5">
      <div className="rounded-lg border border-slate-200 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-sm font-semibold text-slate-900">تفعيل استخدام الكوبونات للذكاء</p>
            <p className="text-[11px] text-slate-500 mt-0.5">عند الإيقاف، لن يقدم الذكاء أي كوبون داخل المحادثة</p>
          </div>
          <Toggle on={value.enabled} onChange={v => set('enabled', v)} on_label="مفعّل" off_label="متوقف" />
        </div>
      </div>

      <Field label="المستويات المسموح للذكاء بإصدارها">
        <div className="flex flex-wrap gap-1.5">
          {(['bronze', 'silver', 'gold', 'vip'] as CouponLevelId[]).map(lv => {
            const checked = value.allowed_levels.includes(lv)
            return (
              <button
                key={lv}
                type="button"
                onClick={() => set('allowed_levels', checked
                  ? value.allowed_levels.filter(x => x !== lv)
                  : [...value.allowed_levels, lv],
                )}
                className={`text-xs px-3 py-1.5 rounded-full border ${
                  checked ? 'border-brand-400 bg-brand-50 text-brand-700 font-semibold' : 'border-slate-200 text-slate-500'
                }`}
              >
                {LEVEL_META[lv].label}
              </button>
            )
          })}
        </div>
      </Field>

      <div className="grid sm:grid-cols-2 gap-4">
        <Field label="أقل صلاحية متبقية (ساعة)" hint="افتراضياً 3 — لا يقدّم الذكاء كوبوناً سينتهي قبل هذا">
          <NumberInput value={value.min_remaining_hours} onChange={v => set('min_remaining_hours', v ?? 0)} min={0} />
        </Field>

        <Field label="مصدر الكوبونات للذكاء">
          <div className="grid grid-cols-1 gap-1.5">
            {([
              { key: 'pool_first',     label: 'يفضل المخزون، يولّد عند الحاجة' },
              { key: 'pool_only',      label: 'من المخزون فقط' },
              { key: 'on_demand_only', label: 'يولّد عند الحاجة فقط' },
            ] as Array<{ key: CouponPoolMode; label: string }>).map(opt => {
              const active = value.pool_mode === opt.key
              return (
                <button
                  key={opt.key}
                  type="button"
                  onClick={() => set('pool_mode', opt.key)}
                  className={`text-start text-xs px-3 py-2 rounded-lg border ${
                    active ? 'border-brand-400 bg-brand-50 text-brand-700 font-semibold' : 'border-slate-200 text-slate-600'
                  }`}
                >
                  {opt.label}
                </button>
              )
            })}
          </div>
        </Field>
      </div>
    </div>
  )
}

// ── Tiny form primitives ─────────────────────────────────────────────────────

function Field({ label, hint, children, small, className = '' }: {
  label: string; hint?: string; children: React.ReactNode; small?: boolean; className?: string
}) {
  return (
    <label className={`block ${className}`}>
      <span className={`block ${small ? 'text-[10px]' : 'text-xs'} font-medium text-slate-700 mb-1`}>{label}</span>
      {children}
      {hint && <span className="block text-[10px] text-slate-400 mt-1">{hint}</span>}
    </label>
  )
}

function NumberInput({ value, onChange, min, max, step, nullable, placeholder }: {
  value: number | null | undefined
  onChange: (v: number | null) => void
  min?: number
  max?: number
  step?: number
  nullable?: boolean
  placeholder?: string
}) {
  return (
    <input
      type="number"
      value={value ?? ''}
      onChange={e => {
        const v = e.target.value
        if (v === '') {
          onChange(nullable ? null : 0)
        } else {
          let n = Number(v)
          if (Number.isNaN(n)) n = min ?? 0
          if (typeof min === 'number') n = Math.max(min, n)
          if (typeof max === 'number') n = Math.min(max, n)
          onChange(n)
        }
      }}
      min={min}
      max={max}
      step={step ?? 1}
      placeholder={placeholder}
      className="w-full text-sm px-3 py-2 rounded-lg border border-slate-200 focus:border-brand-400 focus:ring-2 focus:ring-brand-100 outline-none bg-white"
    />
  )
}

function Toggle({ on, onChange, on_label, off_label }: {
  on: boolean; onChange: (v: boolean) => void; on_label?: string; off_label?: string
}) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      className="inline-flex items-center gap-2"
    >
      {on
        ? <ToggleRight className="w-7 h-7 text-brand-500" />
        : <ToggleLeft  className="w-7 h-7 text-slate-300" />}
      <span className="text-[11px] text-slate-600">{on ? on_label : off_label}</span>
    </button>
  )
}
