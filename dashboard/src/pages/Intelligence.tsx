import { useState, useEffect, useCallback } from 'react'
import {
  RefreshCw,
  Brain,
  TrendingUp,
  AlertTriangle,
  Crown,
  Zap,
  Users,
  CheckCircle,
  Sparkles,
  Clock,
  Save, Bot, Loader2, ToggleLeft, ToggleRight, Settings2,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  automationsApi,
  IntelligenceDashboard,
  IntelligenceSuggestion,
  CustomerSegment,
} from '../api/automations'
import { settingsApi, type AISettings } from '../api/settings'

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatArabicDate(dateStr: string): string {
  try {
    const date = new Date(dateStr)
    return date.toLocaleDateString('ar-SA', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
    })
  } catch {
    return dateStr
  }
}

function confidenceVariant(confidence: number): 'green' | 'amber' | 'red' {
  if (confidence > 80) return 'green'
  if (confidence > 60) return 'amber'
  return 'red'
}

function inactiveDaysVariant(days: number): 'red' | 'amber' {
  return days > 90 ? 'red' : 'amber'
}

function priorityDotColor(priority: 'high' | 'medium' | 'low'): string {
  if (priority === 'high') return 'bg-red-500'
  if (priority === 'medium') return 'bg-amber-500'
  return 'bg-slate-400'
}

function SuggestionIcon({ type }: { type: string }) {
  if (type === 'reorder') return <RefreshCw className="w-4 h-4 text-brand-500 shrink-0" />
  if (type === 'winback') return <Users className="w-4 h-4 text-blue-500 shrink-0" />
  if (type === 'vip') return <Crown className="w-4 h-4 text-amber-500 shrink-0" />
  return <Sparkles className="w-4 h-4 text-purple-500 shrink-0" />
}

function segmentBarColor(color: string): string {
  const map: Record<string, string> = {
    green: 'bg-emerald-500',
    blue: 'bg-blue-500',
    amber: 'bg-amber-500',
    slate: 'bg-slate-300',
    red: 'bg-red-500',
    purple: 'bg-purple-500',
  }
  return map[color] ?? 'bg-slate-300'
}

// ── Loading Spinner ────────────────────────────────────────────────────────────

function LoadingState() {
  return (
    <div className="flex flex-col items-center justify-center py-24 gap-4">
      <div className="w-10 h-10 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
      <p className="text-sm text-slate-500 font-medium">جارٍ تحليل بيانات العملاء…</p>
    </div>
  )
}

// ── Error State ───────────────────────────────────────────────────────────────

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-24 gap-4">
      <AlertTriangle className="w-10 h-10 text-red-400" />
      <p className="text-sm text-slate-600 font-medium">تعذّر تحميل البيانات</p>
      <button
        onClick={onRetry}
        className="btn-primary text-sm flex items-center gap-2"
      >
        <RefreshCw className="w-4 h-4" />
        إعادة المحاولة
      </button>
    </div>
  )
}

// ── AI Settings Panel ─────────────────────────────────────────────────────────

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-700 mb-1">{label}</label>
      {children}
      {hint && <p className="text-xs text-slate-400 mt-1">{hint}</p>}
    </div>
  )
}

function Toggle({ label, hint, value, onChange }: { label: string; hint?: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-start justify-between py-3 border-b border-slate-50 last:border-0">
      <div>
        <p className="text-sm text-slate-800">{label}</p>
        {hint && <p className="text-xs text-slate-400 mt-0.5">{hint}</p>}
      </div>
      <button onClick={() => onChange(!value)} className="ms-4 shrink-0">
        {value ? <ToggleRight className="w-6 h-6 text-brand-500" /> : <ToggleLeft className="w-6 h-6 text-slate-300" />}
      </button>
    </div>
  )
}

function AISettingsPanel() {
  const [ai, setAi]       = useState<AISettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving]   = useState(false)
  const [saved, setSaved]     = useState(false)
  const [error, setError]     = useState<string | null>(null)

  useEffect(() => {
    settingsApi.getAll()
      .then(s => setAi(s.ai))
      .catch(() => setError('تعذّر تحميل إعدادات الذكاء'))
      .finally(() => setLoading(false))
  }, [])

  const patch = (p: Partial<AISettings>) => setAi(prev => prev ? { ...prev, ...p } : prev)

  const handleSave = async () => {
    if (!ai) return
    setSaving(true); setError(null); setSaved(false)
    try {
      const res = await settingsApi.update({ ai })
      setAi(res.ai)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch {
      setError('فشل الحفظ — حاول مجدداً')
    } finally { setSaving(false) }
  }

  if (loading) return (
    <div className="flex items-center justify-center py-16 gap-2 text-slate-400 text-sm">
      <Loader2 className="w-4 h-4 animate-spin text-brand-500" /> جاري التحميل...
    </div>
  )

  if (!ai) return (
    <div className="card p-6 text-center text-sm text-red-500">
      {error ?? 'تعذّر تحميل الإعدادات'}
    </div>
  )

  return (
    <div className="space-y-5">

      {/* ── Personality ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Bot className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">شخصية المساعد</h2>
          <p className="text-xs text-slate-400 mr-1">اسم نحلة ونبرتها وطريقة تواصلها</p>
        </div>
        <div className="p-5 grid sm:grid-cols-2 gap-4">
          <Field label="اسم المساعد">
            <input className="input" value={ai.assistant_name} onChange={e => patch({ assistant_name: e.target.value })} placeholder="نحلة" />
          </Field>
          <Field label="نبرة الرد">
            <select className="input" value={ai.reply_tone} onChange={e => patch({ reply_tone: e.target.value as AISettings['reply_tone'] })}>
              <option value="friendly">ودية وقريبة</option>
              <option value="professional">احترافية ورسمية</option>
              <option value="sales">مبيعات وإقناع</option>
            </select>
          </Field>
          <Field label="طول الرد">
            <select className="input" value={ai.reply_length} onChange={e => patch({ reply_length: e.target.value as AISettings['reply_length'] })}>
              <option value="short">قصير ومختصر</option>
              <option value="medium">متوسط</option>
              <option value="detailed">تفصيلي وشامل</option>
            </select>
          </Field>
          <Field label="لغة الردود">
            <select className="input" value={ai.default_language} onChange={e => patch({ default_language: e.target.value as AISettings['default_language'] })}>
              <option value="arabic">عربي فقط</option>
              <option value="english">إنجليزي فقط</option>
              <option value="bilingual">ثنائي اللغة</option>
            </select>
          </Field>
          <div className="sm:col-span-2">
            <Field label="دور ووصف المساعد" hint="يُقرأ بواسطة الذكاء الاصطناعي لفهم طبيعة المتجر وشخصيته">
              <textarea
                className="input min-h-[90px] resize-y"
                value={ai.assistant_role}
                onChange={e => patch({ assistant_role: e.target.value })}
                placeholder="مثال: أنت مساعدة لمتجر ملابس رجالية فاخرة في الرياض. تُجيب بلهجة ودية ومحترفة وتساعد العملاء في اختيار المنتجات المناسبة..."
              />
            </Field>
          </div>
        </div>
      </div>

      {/* ── Owner Instructions ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Settings2 className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">تعليمات المالك</h2>
          <p className="text-xs text-slate-400 mr-1">قواعد وسياسات تُضاف لكل محادثة</p>
        </div>
        <div className="p-5 space-y-4">
          <Field label="تعليمات عامة" hint="قواعد يجب أن تلتزم بها نحلة دائماً في كل محادثة">
            <textarea
              className="input min-h-[100px] resize-y"
              value={ai.owner_instructions}
              onChange={e => patch({ owner_instructions: e.target.value })}
              placeholder="مثال: لا تعطِ وعوداً بالتوصيل قبل التأكد من المخزون. لا تذكر أسعار المنافسين. تعامل مع الشكاوى بأعلى مستوى من الاحترام..."
            />
          </Field>
          <Field label="متى تقترح الخصومات؟" hint="كيف يتصرف الذكاء عند الحديث عن العروض والكوبونات">
            <div className="rounded-lg border border-amber-200/70 bg-amber-50/60 px-3 py-2 mb-2 text-[11px] text-amber-800 leading-relaxed">
              هذا الحقل يتحكم في نبرة المحادثة فقط. إدارة قواعد الكوبونات الفعلية (نسبة الخصم، الصلاحية) تتم من صفحة <a href="/coupons" className="underline font-semibold">الكوبونات</a>.
            </div>
            <textarea
              className="input min-h-[80px] resize-y"
              value={ai.coupon_rules}
              onChange={e => patch({ coupon_rules: e.target.value })}
              placeholder="مثال: اقترح خصماً فقط عند تردد العميل أو عند عدم الشراء لأكثر من 30 يوماً..."
            />
          </Field>
          <Field label="قواعد التصعيد للإنسان" hint="متى تحوّل نحلة المحادثة للمالك أو فريق الدعم">
            <textarea
              className="input min-h-[80px] resize-y"
              value={ai.escalation_rules}
              onChange={e => patch({ escalation_rules: e.target.value })}
              placeholder="مثال: حوّل المحادثة للمالك عند: شكاوى الجودة، الطلبات بأكثر من 500 ريال، العملاء الغاضبين..."
            />
          </Field>
        </div>
      </div>

      {/* ── Discounts & Recommendations ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">الخصومات والتوصيات</h2>
        </div>
        <div className="p-5 space-y-4">
          <Field label="الحد الأقصى للخصم المسموح به">
            <select className="input" value={ai.allowed_discount_levels} onChange={e => patch({ allowed_discount_levels: e.target.value })}>
              <option value="0">بدون خصم</option>
              <option value="5">5%</option>
              <option value="10">10%</option>
              <option value="15">15%</option>
              <option value="20">20%</option>
              <option value="30">30%</option>
            </select>
          </Field>
          <Toggle
            label="تفعيل توصيات المنتجات"
            hint="نحلة تقترح منتجات ذات صلة أثناء المحادثة"
            value={ai.recommendations_enabled}
            onChange={v => patch({ recommendations_enabled: v })}
          />
          <div className="p-3 bg-amber-50 rounded-lg border border-amber-200">
            <p className="text-xs text-amber-700 flex items-start gap-2">
              <Bot className="w-3.5 h-3.5 shrink-0 mt-0.5" />
              التغييرات تُطبَّق فوراً على المحادثات الجديدة. المحادثات الجارية لا تتأثر.
            </p>
          </div>
        </div>
      </div>

      {/* ── Save bar ── */}
      <div className="flex items-center gap-3 flex-wrap pb-2">
        <button onClick={handleSave} disabled={saving} className="btn-primary text-sm">
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          {saving ? 'جاري الحفظ...' : 'حفظ الإعدادات'}
        </button>
        {saved && (
          <span className="flex items-center gap-1.5 text-sm text-emerald-600">
            <CheckCircle className="w-4 h-4" /> تم الحفظ بنجاح
          </span>
        )}
        {error && (
          <span className="flex items-center gap-1.5 text-sm text-red-600">
            <AlertTriangle className="w-3.5 h-3.5" /> {error}
          </span>
        )}
      </div>
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Intelligence() {
  useLanguage() // initialise RTL context

  const [activeTab, setActiveTab] = useState<'dashboard' | 'settings'>('dashboard')
  const [data, setData] = useState<IntelligenceDashboard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      const result = await automationsApi.getIntelligence()
      setData(result)
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const handleApplySuggestion = (suggestion: IntelligenceSuggestion) => {
    alert(`سيتم تطبيق التوصية: ${suggestion.title}`)
  }

  return (
    <div className="space-y-6">
      {/* ── Page Header ───────────────────────────────────────────────────── */}
      <PageHeader
        title="الذكاء الاصطناعي"
        subtitle="إعدادات المساعد، الشخصية، ولوحة التحليلات الذكية"
        action={
          activeTab === 'dashboard' ? (
            <button
              onClick={load}
              disabled={loading}
              className="btn-secondary text-sm flex items-center gap-2"
            >
              <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
              تحديث
            </button>
          ) : undefined
        }
      />

      {/* ── Tabs ──────────────────────────────────────────────────────────── */}
      <div className="border-b border-slate-200 -mx-3 px-3 md:-mx-6 md:px-6">
        <div className="flex gap-1">
          <button
            onClick={() => setActiveTab('settings')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'settings'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <Bot className="w-4 h-4 shrink-0" />
            إعدادات المساعد
          </button>
          <button
            onClick={() => setActiveTab('dashboard')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'dashboard'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <Brain className="w-4 h-4 shrink-0" />
            لوحة الذكاء
          </button>
        </div>
      </div>

      {/* ── AI Settings Tab ────────────────────────────────────────────────── */}
      {activeTab === 'settings' && <AISettingsPanel />}

      {/* ── Dashboard Tab ─────────────────────────────────────────────────── */}
      {activeTab === 'dashboard' && (<>
      {/* ── States ────────────────────────────────────────────────────────── */}
      {loading && <LoadingState />}
      {!loading && error && <ErrorState onRetry={load} />}

      {!loading && !error && data && (
        <div className="space-y-6">
          {/* ── Summary StatCards ──────────────────────────────────────────── */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <StatCard
              label="سيُعيدون الطلب قريباً"
              value={String(data.summary.reorder_soon_count)}
              change={0}
              icon={RefreshCw}
              iconColor="text-emerald-600"
              iconBg="bg-emerald-50"
            />
            <StatCard
              label="في خطر المغادرة"
              value={String(data.summary.churn_risk_count)}
              change={0}
              icon={AlertTriangle}
              iconColor="text-red-600"
              iconBg="bg-red-50"
            />
            <StatCard
              label="عملاء VIP"
              value={String(data.summary.vip_count)}
              change={0}
              icon={Crown}
              iconColor="text-amber-600"
              iconBg="bg-amber-50"
            />
            <StatCard
              label="أتمتة نشطة"
              value={String(data.summary.active_automations)}
              change={0}
              icon={Zap}
              iconColor="text-brand-600"
              iconBg="bg-brand-50"
            />
          </div>

          <div className="grid grid-cols-2 lg:grid-cols-2 gap-4">
            <StatCard
              label="عملاء محتملون"
              value={String(data.summary.leads_count ?? 0)}
              change={0}
              icon={Users}
              iconColor="text-blue-600"
              iconBg="bg-blue-50"
            />
            <StatCard
              label="غير نشطين"
              value={String(data.summary.inactive_count ?? 0)}
              change={0}
              icon={Clock}
              iconColor="text-slate-600"
              iconBg="bg-slate-100"
            />
          </div>

          {/* ── Suggestions Panel ──────────────────────────────────────────── */}
          {data.suggestions.length > 0 && (
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  توصيات نحلة الذكية 💡
                </h2>
              </div>
              <ul className="divide-y divide-slate-100">
                {data.suggestions.map((suggestion) => (
                  <li
                    key={suggestion.id}
                    className="flex items-center gap-3 px-5 py-3.5 hover:bg-slate-50 transition-colors"
                  >
                    {/* Priority dot */}
                    <span
                      className={`w-2 h-2 rounded-full shrink-0 ${priorityDotColor(suggestion.priority)}`}
                    />
                    {/* Type icon */}
                    <SuggestionIcon type={suggestion.type} />
                    {/* Text */}
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-semibold text-slate-900 truncate">
                        {suggestion.title}
                      </p>
                      <p className="text-xs text-slate-500 truncate mt-0.5">
                        {suggestion.desc}
                      </p>
                    </div>
                    {/* Apply button */}
                    <button
                      onClick={() => handleApplySuggestion(suggestion)}
                      className="btn-primary text-xs shrink-0 py-1.5 px-3"
                    >
                      تطبيق
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ── Two-column: Reorder Predictions + Churn Risk ───────────────── */}
          <div className="grid lg:grid-cols-2 gap-4">
            {/* Reorder Predictions */}
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  عملاء يُتوقع إعادة طلبهم قريباً 🐝
                </h2>
              </div>
              {data.reorder_predictions.length === 0 ? (
                <p className="text-xs text-slate-400 text-center py-8">لا توجد تنبؤات حالياً</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-slate-100 bg-slate-50">
                        <th className="text-start px-5 py-2.5 font-medium text-slate-500">
                          العميل
                        </th>
                        <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                          المنتج
                        </th>
                        <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                          التاريخ المتوقع
                        </th>
                        <th className="text-start px-3 py-2.5 font-medium text-slate-500 pe-5">
                          الثقة
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-100">
                      {data.reorder_predictions.map((pred, idx) => (
                        <tr key={idx} className="hover:bg-slate-50 transition-colors">
                          <td className="px-5 py-3">
                            <p className="font-medium text-slate-900 truncate max-w-[110px]">
                              {pred.customer_name}
                            </p>
                            <p
                              dir="ltr"
                              className="text-slate-400 mt-0.5 font-mono truncate max-w-[110px]"
                            >
                              {pred.phone}
                            </p>
                          </td>
                          <td className="px-3 py-3 text-slate-600 truncate max-w-[100px]">
                            {pred.product_name}
                          </td>
                          <td className="px-3 py-3 text-slate-600 whitespace-nowrap">
                            {formatArabicDate(pred.predicted_date)}
                          </td>
                          <td className="px-3 py-3 pe-5">
                            <Badge
                              label={`${pred.confidence}%`}
                              variant={confidenceVariant(pred.confidence)}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>

            {/* Churn Risk */}
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  عملاء في خطر المغادرة ⚠️
                </h2>
              </div>
              {data.churn_risk.length === 0 ? (
                <p className="text-xs text-slate-400 text-center py-8">لا يوجد عملاء في خطر حالياً</p>
              ) : (
                <ul className="divide-y divide-slate-100">
                  {data.churn_risk.map((customer, idx) => (
                    <li key={idx} className="px-5 py-3.5 space-y-2 hover:bg-slate-50 transition-colors">
                      {/* Name + inactive days + target button */}
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-2 min-w-0">
                          <p className="text-xs font-semibold text-slate-900 truncate">
                            {customer.customer_name}
                          </p>
                          <Badge
                            label={`${customer.days_inactive} يوم`}
                            variant={inactiveDaysVariant(customer.days_inactive)}
                          />
                        </div>
                        <button className="btn-secondary text-xs py-1 px-2.5 shrink-0">
                          استهدف
                        </button>
                      </div>
                      {/* Last purchase */}
                      <p className="text-xs text-slate-400">
                        آخر شراء:{' '}
                        <span className="text-slate-600">
                          {formatArabicDate(customer.last_purchase)}
                        </span>
                      </p>
                      {/* Risk bar */}
                      <div className="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
                        <div
                          className={`h-1.5 rounded-full transition-all ${
                            customer.risk_score > 70 ? 'bg-red-500' : 'bg-amber-500'
                          }`}
                          style={{ width: `${Math.min(customer.risk_score, 100)}%` }}
                        />
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          {/* ── Customer Segments ──────────────────────────────────────────── */}
          {data.segments.length > 0 && (
            <div className="card p-5">
              <h2 className="text-sm font-semibold text-slate-900 mb-4">حالات العملاء التشغيلية</h2>
              {(() => {
                const total = data.segments.reduce((sum, s) => sum + s.count, 0) || 1
                return (
                  <div className="space-y-3">
                    {data.segments.map((segment) => {
                      const pct = Math.round((segment.count / total) * 100)
                      return (
                        <div key={segment.key} className="flex items-center gap-3">
                          {/* Label */}
                          <span className="text-xs text-slate-600 w-28 shrink-0 text-end">
                            {segment.label}
                          </span>
                          {/* Bar track */}
                          <div className="flex-1 bg-slate-100 rounded-full h-4 overflow-hidden">
                            <div
                              className={`h-4 rounded-full flex items-center justify-end pe-2 transition-all ${segmentBarColor(
                                segment.color
                              )}`}
                              style={{ width: `${Math.max(pct, 4)}%` }}
                            >
                              {pct >= 12 && (
                                <span className="text-white text-xs font-semibold leading-none">
                                  {segment.count}
                                </span>
                              )}
                            </div>
                          </div>
                          {/* Count outside bar when bar is narrow */}
                          {pct < 12 && (
                            <span className="text-xs font-semibold text-slate-700 shrink-0 w-8">
                              {segment.count}
                            </span>
                          )}
                          {pct >= 12 && <span className="w-8 shrink-0" />}
                        </div>
                      )
                    })}
                  </div>
                )
              })()}
            </div>
          )}

          {!!data.rfm_segments?.length && (
            <div className="card p-5">
              <h2 className="text-sm font-semibold text-slate-900 mb-4">قطاعات RFM الذكية</h2>
              <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {data.rfm_segments.map((segment) => (
                  <div key={segment.key} className="rounded-xl border border-slate-100 bg-slate-50 px-4 py-3">
                    <p className="text-xs text-slate-500">{segment.label}</p>
                    <p className="text-lg font-semibold text-slate-900 mt-1">
                      {segment.count.toLocaleString('ar-SA')}
                    </p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── VIP Customers ──────────────────────────────────────────────── */}
          {data.vip_customers.length > 0 && (
            <div className="card">
              <div className="px-5 py-4 border-b border-slate-100">
                <h2 className="text-sm font-semibold text-slate-900">
                  أفضل العملاء قيمةً 👑
                </h2>
              </div>
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-slate-100 bg-slate-50">
                      <th className="text-start px-5 py-2.5 font-medium text-slate-500 w-8">
                        #
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                        الاسم
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                        الإنفاق الكلي
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500">
                        الطلبات
                      </th>
                      <th className="text-start px-3 py-2.5 font-medium text-slate-500 pe-5">
                        الشريحة
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-100">
                    {data.vip_customers.map((vip, idx) => (
                      <tr key={idx} className="hover:bg-slate-50 transition-colors">
                        <td className="px-5 py-3 text-slate-400 font-medium">{idx + 1}</td>
                        <td className="px-3 py-3">
                          <div className="flex items-center gap-2">
                            {idx === 0 && (
                              <Crown className="w-3.5 h-3.5 text-amber-500 shrink-0" />
                            )}
                            <span className="font-medium text-slate-900 truncate max-w-[140px]">
                              {vip.customer_name}
                            </span>
                          </div>
                        </td>
                        <td className="px-3 py-3 font-semibold text-slate-900 whitespace-nowrap">
                          {vip.total_spent.toLocaleString('ar-SA')} ر.س
                        </td>
                        <td className="px-3 py-3 text-slate-600">{vip.orders}</td>
                        <td className="px-3 py-3 pe-5">
                          <Badge
                            label={vip.segment}
                            variant={
                              vip.segment === 'VIP'
                                ? 'amber'
                                : vip.segment === 'نشط'
                                ? 'green'
                                : 'slate'
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
      </>)}
    </div>
  )
}
