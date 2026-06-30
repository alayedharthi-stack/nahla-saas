import { useState, useEffect, useCallback, useRef } from 'react'
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
  Store, PackageCheck, PackageX, ShieldCheck, CreditCard, Truck,
  MessageSquare, FileText, Info, ChevronDown, ChevronUp,
  Plus, Trash2, ThumbsUp, Pencil, X,
  BarChart2, Activity, Timer,
  Shield,
  Tag, Image as ImageIcon,
} from 'lucide-react'
import { ManualCouponsPanel, AIMediaLibraryPanel } from './IntelligenceLibraries'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  automationsApi,
  IntelligenceDashboard,
  IntelligenceSuggestion,
  CustomerSegment,
  MerchantKnowledge,
  MerchantKnowledgePolicies,
  ResponseQualityData,
} from '../api/automations'
import { settingsApi, type AISettings } from '../api/settings'
import { playgroundApi, type PlaygroundDryRunResponse } from '../api/playground'
import { CategoryBadges, OperationalFactWarning } from './knowledge/aiSettingsHints'
import { StructuredContactsCutoverBanner } from '../components/operations/StructuredContactsCutoverBanner'

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
  const [storeAiSaving, setStoreAiSaving] = useState(false)
  const [storeAiError, setStoreAiError] = useState<string | null>(null)

  useEffect(() => {
    settingsApi.getAll()
      .then(s => setAi(s.ai))
      .catch(() => setError('تعذّر تحميل إعدادات الذكاء'))
      .finally(() => setLoading(false))
  }, [])

  const patch = (p: Partial<AISettings>) => setAi(prev => prev ? { ...prev, ...p } : prev)

  const handleStoreAiToggle = async (enabled: boolean) => {
    if (!ai) return
    setStoreAiSaving(true)
    setStoreAiError(null)
    const previous = ai.store_ai_enabled
    setAi(prev => prev ? { ...prev, store_ai_enabled: enabled } : prev)
    try {
      const res = await settingsApi.patchStoreAI(enabled)
      setAi(res.ai)
    } catch {
      setAi(prev => prev ? { ...prev, store_ai_enabled: previous } : prev)
      setStoreAiError('تعذّر تحديث إعداد الذكاء للمتجر — حاول مجدداً')
    } finally {
      setStoreAiSaving(false)
    }
  }

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

      {/* ── Store-wide AI master switch ── */}
      <div className={`card border-2 ${ai.store_ai_enabled ? 'border-emerald-200' : 'border-violet-300'}`}>
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <ShieldCheck className={`w-4 h-4 ${ai.store_ai_enabled ? 'text-emerald-600' : 'text-violet-600'}`} />
          <h2 className="text-sm font-semibold text-slate-900">الذكاء للمتجر كاملًا</h2>
        </div>
        <div className="p-5 space-y-3">
          <Toggle
            label={ai.store_ai_enabled ? 'تشغيل الذكاء للمتجر' : 'إيقاف الذكاء للمتجر كاملًا'}
            hint={
              ai.store_ai_enabled
                ? 'سيعود الذكاء للرد على العملاء غير الموقوفين فرديًا فقط.'
                : 'عند الإيقاف، لن يرد الذكاء على أي عميل في هذا المتجر. ستبقى الرسائل محفوظة ويمكنك الرد يدويًا. العملاء الموقوفون فرديًا سيبقون موقوفين حتى بعد إعادة تشغيل الذكاء العام.'
            }
            value={ai.store_ai_enabled !== false}
            onChange={v => { if (!storeAiSaving) void handleStoreAiToggle(v) }}
          />
          {storeAiSaving && (
            <p className="text-xs text-slate-400 flex items-center gap-1.5">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> جاري الحفظ…
            </p>
          )}
          {storeAiError && (
            <p className="text-xs text-red-600">{storeAiError}</p>
          )}
        </div>
      </div>

      <div className="rounded-xl border border-amber-200 bg-amber-50/70 px-4 py-3 text-xs text-amber-900 leading-relaxed">
        الحقائق التشغيلية (الأسعار، التوفر، الشحن، الدفع، الموقع، أرقام التواصل) لا تُدار
        من شخصية المساعد. يجب إدارتها من{' '}
        <a href="/knowledge-base" className="font-semibold underline">قاعدة المعرفة</a>
        {' '}أو الكتالوج أو إعدادات التصعيد.
      </div>

      {/* ── 1. Personality ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Bot className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">شخصية المساعد</h2>
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
            <Field label="دور ووصف المساعد" hint="سياق المتجر — ليس حقائق تشغيلية">
              <textarea
                className="input min-h-[90px] resize-y"
                value={ai.assistant_role}
                onChange={e => patch({ assistant_role: e.target.value })}
              />
              <CategoryBadges text={ai.assistant_role} />
            </Field>
          </div>
        </div>
      </div>

      {/* ── 2. Behavior rules ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Settings2 className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">قواعد السلوك</h2>
        </div>
        <div className="p-5 space-y-4">
          <Field label="تعليمات عامة (أسلوب وسلوك)" hint="كيف يتصرف المساعد — بدون حقائق تشغيلية">
            <textarea
              className="input min-h-[100px] resize-y"
              value={ai.owner_instructions}
              onChange={e => patch({ owner_instructions: e.target.value })}
            />
            <CategoryBadges text={ai.owner_instructions} />
            <OperationalFactWarning text={ai.owner_instructions} />
          </Field>
        </div>
      </div>

      {/* ── 3. Sales rules ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">قواعد البيع</h2>
          <p className="text-xs text-slate-500 mt-0.5">متى يُقترح بديل أو عرض — منفصلة عن شخصية المساعد</p>
        </div>
        <div className="p-5 space-y-4">
          <Field label="متى تقترح الخصومات؟">
            <textarea
              className="input min-h-[80px] resize-y"
              value={ai.coupon_rules}
              onChange={e => patch({ coupon_rules: e.target.value })}
            />
            <CategoryBadges text={ai.coupon_rules} />
          </Field>
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
        </div>
      </div>

      {/* ── 4. KB links ── */}
      <div className="card border-brand-100 bg-brand-50/30">
        <div className="px-5 py-4 border-b border-brand-100/60">
          <h2 className="text-sm font-semibold text-slate-900">روابط إلى قاعدة المعرفة</h2>
        </div>
        <ul className="p-5 space-y-2 text-sm">
          <li><a href="/knowledge-base#kb-bucket-shipping" className="text-brand-700 font-medium hover:underline">الشحن والتوصيل</a></li>
          <li><a href="/knowledge-base#kb-bucket-policies" className="text-brand-700 font-medium hover:underline">السياسات</a></li>
          <li><a href="/knowledge-base#kb-bucket-payment" className="text-brand-700 font-medium hover:underline">الدفع والتحويل</a></li>
          <li><a href="/knowledge-base#kb-bucket-escalation" className="text-brand-700 font-medium hover:underline">التصعيد والتواصل</a></li>
        </ul>
      </div>

      {/* ── Escalation (legacy field — classified, not deleted) ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100">
          <h2 className="text-sm font-semibold text-slate-900">قواعد التصعيد (نص حر — يُنقل لاحقاً لقاعدة المعرفة)</h2>
        </div>
        <div className="p-5 space-y-2">
          <StructuredContactsCutoverBanner />
          <Field label="متى تحوّل للإنسان">
            <textarea
              className="input min-h-[80px] resize-y"
              value={ai.escalation_rules}
              onChange={e => patch({ escalation_rules: e.target.value })}
            />
            <CategoryBadges text={ai.escalation_rules} />
            <OperationalFactWarning text={ai.escalation_rules} />
          </Field>
        </div>
      </div>

      {/* ── Policy Rules ── */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <ShieldCheck className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">قواعد الأتمتة الذكية</h2>
          <p className="text-xs text-slate-400 mr-1">تحكّم في سلوك الذكاء تلقائياً</p>
        </div>
        <div className="p-5 space-y-5">

          {/* Coupon cap hours */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-semibold text-slate-700">مدة حماية الكوبون</label>
              <span className="text-xs font-bold text-brand-600 bg-brand-50 px-2 py-0.5 rounded-full">
                {ai.coupon_cap_hours ?? 24} ساعة
              </span>
            </div>
            <p className="text-[10px] text-slate-400 mb-2">
              لا يُرسَل كوبون جديد للعميل إذا تلقّى واحداً خلال هذه المدة
            </p>
            <input
              type="range" min={1} max={168} step={1}
              className="w-full accent-brand-500"
              value={ai.coupon_cap_hours ?? 24}
              onChange={e => patch({ coupon_cap_hours: Number(e.target.value) })}
            />
            <div className="flex justify-between text-[9px] text-slate-300 mt-0.5">
              <span>1 ساعة</span><span>24 ساعة</span><span>72 ساعة</span><span>أسبوع</span>
            </div>
          </div>

          {/* Auto-escalate threshold */}
          <Field label="التصعيد التلقائي للإنسان" hint="عدد الردود العامة المتكررة قبل تحويل المحادثة">
            <select
              className="input"
              value={ai.auto_escalate_after_n ?? 3}
              onChange={e => patch({ auto_escalate_after_n: Number(e.target.value) })}
            >
              {[1,2,3,4,5,7,10].map(n => (
                <option key={n} value={n}>بعد {n} ردود غير محددة</option>
              ))}
            </select>
          </Field>

          {/* Max order value */}
          <Field label="الحد الأقصى لقيمة الطلب (ريال)" hint="الذكاء يرفض الطلبات التي تتجاوز هذه القيمة — اتركه 0 للسماح بكل القيم">
            <input
              type="number" min={0} step={50}
              className="input"
              placeholder="0 = غير محدود"
              value={ai.max_order_value ?? 0}
              onChange={e => patch({ max_order_value: Number(e.target.value) })}
            />
          </Field>

          {/* Context verbosity A/B */}
          <div>
            <p className="text-xs font-semibold text-slate-700 mb-1">حجم سياق الذكاء</p>
            <p className="text-[10px] text-slate-400 mb-2">
              اختبر أيّ الوضعين يُنتج ردوداً أفضل — راقب النتائج في تبويب "أداء الذكاء"
            </p>
            <div className="flex gap-2">
              {([
                { key: 'full',    label: 'مفصّل (كامل)',   desc: 'أكثر منتجات، FAQ، سياسات كاملة' },
                { key: 'compact', label: 'مختصر (تجريبي)', desc: '5 منتجات فقط، بدون FAQ مقترح' },
              ] as const).map(({ key, label, desc }) => (
                <button
                  key={key}
                  onClick={() => patch({ context_verbosity: key })}
                  className={`flex-1 rounded-xl border p-3 text-start transition-colors ${
                    (ai.context_verbosity ?? 'full') === key
                      ? 'border-brand-400 bg-brand-50 text-brand-700'
                      : 'border-slate-200 text-slate-600 hover:border-brand-200'
                  }`}
                >
                  <p className="text-xs font-semibold">{label}</p>
                  <p className="text-[10px] mt-0.5 opacity-70">{desc}</p>
                </button>
              ))}
            </div>
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

// ── Merchant Knowledge Panel ──────────────────────────────────────────────────

function QualityRing({ score }: { score: number }) {
  const color =
    score > 85 ? 'text-emerald-500' :
    score > 70 ? 'text-brand-500' :
    score > 40 ? 'text-amber-500' : 'text-red-500'
  const ringColor =
    score > 85 ? 'stroke-emerald-500' :
    score > 70 ? 'stroke-brand-500' :
    score > 40 ? 'stroke-amber-400' : 'stroke-red-400'
  const r = 28
  const circ = 2 * Math.PI * r
  const offset = circ - (score / 100) * circ
  return (
    <div className="relative w-20 h-20 shrink-0">
      <svg className="w-20 h-20 -rotate-90" viewBox="0 0 72 72">
        <circle cx="36" cy="36" r={r} fill="none" stroke="#e2e8f0" strokeWidth="6" />
        <circle
          cx="36" cy="36" r={r} fill="none"
          className={ringColor}
          strokeWidth="6"
          strokeDasharray={circ}
          strokeDashoffset={offset}
          strokeLinecap="round"
        />
      </svg>
      <span className={`absolute inset-0 flex items-center justify-center text-base font-bold ${color}`}>
        {score}
      </span>
    </div>
  )
}

function SectionHeader({ icon: Icon, title, count }: { icon: React.ElementType; title: string; count?: number }) {
  return (
    <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
      <Icon className="w-4 h-4 text-brand-500 shrink-0" />
      <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
      {count !== undefined && (
        <span className="ms-auto text-xs text-slate-400 font-medium">{count}</span>
      )}
    </div>
  )
}

function EmptySlot({ message }: { message: string }) {
  return (
    <p className="text-xs text-slate-400 text-center py-6 px-4">{message}</p>
  )
}

function PolicyCard({ label, value }: { label: string; value: string }) {
  const hasValue = Boolean(value?.trim())
  return (
    <div className={`rounded-xl border p-3.5 ${hasValue ? 'border-slate-200 bg-white' : 'border-red-100 bg-red-50'}`}>
      <p className={`text-xs font-semibold mb-1 ${hasValue ? 'text-slate-600' : 'text-red-500'}`}>{label}</p>
      {hasValue
        ? <p className="text-xs text-slate-700 leading-relaxed line-clamp-3">{value}</p>
        : <p className="text-xs text-red-400">غير محددة</p>
      }
    </div>
  )
}

function CollapsibleSection({ title, icon: Icon, children, defaultOpen = true }: {
  title: string; icon: React.ElementType; children: React.ReactNode; defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="card overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-2 px-5 py-4 border-b border-slate-100 hover:bg-slate-50 transition-colors text-start"
      >
        <Icon className="w-4 h-4 text-brand-500 shrink-0" />
        <span className="text-sm font-semibold text-slate-900 flex-1">{title}</span>
        {open ? <ChevronUp className="w-4 h-4 text-slate-400" /> : <ChevronDown className="w-4 h-4 text-slate-400" />}
      </button>
      {open && <div>{children}</div>}
    </div>
  )
}

function MerchantKnowledgePanel({ onEditSettings }: { onEditSettings?: () => void }) {
  const [data, setData] = useState<MerchantKnowledge | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)

  // ── Editable FAQ state ───────────────────────────────────────────────────
  const [approvedFaqs, setApprovedFaqs] = useState<string[]>([])
  const [suggestedFaqs, setSuggestedFaqs] = useState<string[]>([])
  const [newFaq, setNewFaq] = useState('')
  const [savingFaq, setSavingFaq] = useState(false)
  const [faqSaved, setFaqSaved] = useState(false)
  const faqDirty = useRef(false)

  // ── Editable Policies state ──────────────────────────────────────────────
  const [policies, setPolicies] = useState<MerchantKnowledgePolicies>({
    return_policy: '', shipping_policy: '', payment_policy: '',
    warranty_policy: '', delivery_areas: '', working_hours: '',
  })
  const [savingPolicies, setSavingPolicies] = useState(false)
  const [policiesSaved, setPoliciesSaved] = useState(false)
  const policiesDirty = useRef(false)

  // ── Blocked customers state ───────────────────────────────────────────────
  const [blockedCustomers, setBlockedCustomers] = useState<string[]>([])
  const [newBlockedPhone, setNewBlockedPhone] = useState('')
  const [savingBlocked, setSavingBlocked] = useState(false)
  const [blockedSaved, setBlockedSaved] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      const result = await automationsApi.getMerchantKnowledge()
      setData(result)
      setApprovedFaqs(result.faqs.approved ?? [])
      setSuggestedFaqs(result.faqs.suggested ?? [])
      setPolicies(result.policies)
      setBlockedCustomers(result.brain_profile?.blocked_customers ?? [])
      faqDirty.current = false
      policiesDirty.current = false
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // ── FAQ handlers ─────────────────────────────────────────────────────────
  const addFaq = () => {
    const q = newFaq.trim()
    if (!q || approvedFaqs.includes(q)) return
    setApprovedFaqs(prev => [...prev, q])
    setNewFaq('')
    faqDirty.current = true
  }

  const deleteFaq = (idx: number) => {
    setApprovedFaqs(prev => prev.filter((_, i) => i !== idx))
    faqDirty.current = true
  }

  const approveSuggested = (q: string) => {
    if (!approvedFaqs.includes(q)) setApprovedFaqs(prev => [...prev, q])
    setSuggestedFaqs(prev => prev.filter(s => s !== q))
    faqDirty.current = true
  }

  const deleteSuggested = (q: string) => {
    setSuggestedFaqs(prev => prev.filter(s => s !== q))
    faqDirty.current = true
  }

  const saveFaqs = async () => {
    setSavingFaq(true)
    try {
      await automationsApi.updateMerchantKnowledge({
        faqs: { approved: approvedFaqs, suggested: suggestedFaqs },
      })
      faqDirty.current = false
      setFaqSaved(true)
      setTimeout(() => setFaqSaved(false), 2500)
    } finally {
      setSavingFaq(false)
    }
  }

  // ── Policy handlers ──────────────────────────────────────────────────────
  const savePolicies = async () => {
    setSavingPolicies(true)
    try {
      await automationsApi.updateMerchantKnowledge({ policies })
      policiesDirty.current = false
      setPoliciesSaved(true)
      setTimeout(() => setPoliciesSaved(false), 2500)
    } finally {
      setSavingPolicies(false)
    }
  }

  // ── Blocked customers handlers ────────────────────────────────────────────
  const addBlockedPhone = () => {
    const phone = newBlockedPhone.trim()
    if (!phone || blockedCustomers.includes(phone)) return
    setBlockedCustomers(prev => [...prev, phone])
    setNewBlockedPhone('')
  }

  const removeBlockedPhone = (phone: string) => {
    setBlockedCustomers(prev => prev.filter(p => p !== phone))
  }

  const saveBlocked = async () => {
    setSavingBlocked(true)
    try {
      await automationsApi.updateMerchantKnowledge({ blocked_customers: blockedCustomers })
      setBlockedSaved(true)
      setTimeout(() => setBlockedSaved(false), 2500)
    } finally {
      setSavingBlocked(false)
    }
  }

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-4">
        <div className="w-10 h-10 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
        <p className="text-sm text-slate-500">جارٍ تحميل معرفة المتجر…</p>
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="flex flex-col items-center justify-center py-24 gap-4">
        <AlertTriangle className="w-10 h-10 text-red-400" />
        <p className="text-sm text-slate-600">تعذّر تحميل بيانات ذكاء المتجر</p>
        <button onClick={load} className="btn-primary text-sm flex items-center gap-2">
          <RefreshCw className="w-4 h-4" /> إعادة المحاولة
        </button>
      </div>
    )
  }

  const { sync_status, quality, products, payment_methods, shipping_methods, pages, warnings, brain_profile } = data

  const lastSync = sync_status.last_sync_at
    ? new Date(sync_status.last_sync_at).toLocaleDateString('ar-SA', { year: 'numeric', month: 'long', day: 'numeric' })
    : 'لم تتم مزامنة'

  return (
    <div className="space-y-5">

      {/* ── Warnings banner ── */}
      {warnings.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 flex gap-3">
          <Info className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
          <div>
            <p className="text-xs font-semibold text-amber-800 mb-1">نواقص تؤثر على جودة الذكاء</p>
            <ul className="space-y-0.5">
              {warnings.map((w, i) => (
                <li key={i} className="text-xs text-amber-700">• {w}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      {/* ── Status overview card ── */}
      <div className="card p-5">
        <div className="flex items-start gap-5 flex-wrap">
          <QualityRing score={quality.score} />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1 flex-wrap">
              <h2 className="text-base font-bold text-slate-900">جودة معرفة الذكاء</h2>
              <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${
                quality.score > 85 ? 'bg-emerald-100 text-emerald-700' :
                quality.score > 70 ? 'bg-brand-100 text-brand-700' :
                quality.score > 40 ? 'bg-amber-100 text-amber-700' : 'bg-red-100 text-red-700'
              }`}>{quality.label}</span>
              {sync_status.is_fresh
                ? <span className="text-xs bg-emerald-100 text-emerald-700 px-2 py-0.5 rounded-full">بيانات محدّثة</span>
                : <span className="text-xs bg-slate-100 text-slate-500 px-2 py-0.5 rounded-full">قديمة</span>
              }
            </div>
            <p className="text-xs text-slate-500 mb-3">
              {sync_status.store_name && <span className="font-medium text-slate-700">{sync_status.store_name}</span>}
              {sync_status.store_name && ' — '}
              آخر مزامنة: {lastSync}
              {sync_status.platform !== 'unknown' && ` (${sync_status.platform})`}
            </p>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-3">
              {[
                { label: 'منتج قابل للطلب', value: products.orderable_count, ok: products.orderable_count > 0 },
                { label: 'مستبعد', value: products.excluded_count, ok: true },
                { label: 'طرق دفع', value: payment_methods.length, ok: payment_methods.length > 0 },
                { label: 'طرق شحن', value: shipping_methods.length, ok: shipping_methods.length > 0 },
                { label: 'FAQ معتمد', value: approvedFaqs.length, ok: approvedFaqs.length > 0 },
                { label: 'صفحات', value: pages.length, ok: true },
              ].map(({ label, value, ok }) => (
                <div key={label} className={`rounded-lg p-2.5 text-center ${ok ? 'bg-slate-50' : 'bg-red-50'}`}>
                  <p className={`text-xl font-bold ${ok ? 'text-slate-800' : 'text-red-500'}`}>{value}</p>
                  <p className="text-[10px] text-slate-500 mt-0.5 leading-tight">{label}</p>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

      {/* ── Orderable products ── */}
      <CollapsibleSection title={`المنتجات التي يستطيع الذكاء بيعها (${products.orderable_count})`} icon={PackageCheck}>
        {products.orderable.length === 0
          ? <EmptySlot message="لا توجد منتجات قابلة للطلب — تحقق من المزامنة مع سلة" />
          : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-slate-100 bg-slate-50">
                    <th className="text-start px-5 py-2.5 font-medium text-slate-500">المنتج</th>
                    <th className="text-start px-3 py-2.5 font-medium text-slate-500">السعر</th>
                    <th className="text-start px-3 py-2.5 font-medium text-slate-500">المخزون</th>
                    <th className="text-start px-3 py-2.5 font-medium text-slate-500 pe-5">التصنيف</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {products.orderable.map((p, i) => (
                    <tr key={i} className="hover:bg-slate-50 transition-colors">
                      <td className="px-5 py-3">
                        <p className="font-medium text-slate-900 truncate max-w-[200px]">{p.title}</p>
                        {p.sku && <p className="text-slate-400 mt-0.5 font-mono text-[10px]">{p.sku}</p>}
                      </td>
                      <td className="px-3 py-3 text-slate-700 whitespace-nowrap">
                        {p.sale_price
                          ? <><span className="font-semibold text-emerald-600">{p.sale_price}</span> <span className="line-through text-slate-400">{p.price}</span></>
                          : <span>{p.price ?? '—'}</span>
                        } <span className="text-slate-400">ر.س</span>
                      </td>
                      <td className="px-3 py-3">
                        <Badge
                          label={p.stock_qty !== null ? `${p.stock_qty} قطعة` : 'متاح'}
                          variant="green"
                        />
                      </td>
                      <td className="px-3 py-3 pe-5 text-slate-500 truncate max-w-[100px]">
                        {(p as unknown as Record<string, unknown>).category as string || '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </CollapsibleSection>

      {/* ── Excluded products ── */}
      {products.excluded.length > 0 && (
        <CollapsibleSection title={`المنتجات المستبعدة (${products.excluded_count})`} icon={PackageX} defaultOpen={false}>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-slate-100 bg-slate-50">
                  <th className="text-start px-5 py-2.5 font-medium text-slate-500">المنتج</th>
                  <th className="text-start px-3 py-2.5 font-medium text-slate-500 pe-5">سبب الاستبعاد</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {products.excluded.map((p, i) => (
                  <tr key={i} className="hover:bg-slate-50 transition-colors">
                    <td className="px-5 py-3">
                      <p className="font-medium text-slate-700 truncate max-w-[200px]">{p.title}</p>
                      {!p.has_salla_id && (
                        <span className="inline-block mt-0.5 text-[10px] text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded">بدون معرّف سلة</span>
                      )}
                    </td>
                    <td className="px-3 py-3 pe-5">
                      <span className="text-red-600 bg-red-50 px-2 py-0.5 rounded text-[10px]">{p.reason}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CollapsibleSection>
      )}

      {/* ── Policies (editable) ── */}
      <CollapsibleSection title="سياسات المتجر" icon={ShieldCheck}>
        <div className="p-5 space-y-4">
          <p className="text-xs text-slate-500">
            هذه السياسات تُستخدم مباشرة بواسطة نحلة للإجابة على أسئلة العملاء. عدّلها ثم احفظ.
          </p>
          <div className="grid sm:grid-cols-2 gap-3">
            {([
              { key: 'return_policy',   label: 'سياسة الإرجاع' },
              { key: 'shipping_policy', label: 'سياسة الشحن' },
              { key: 'payment_policy',  label: 'سياسة الدفع' },
              { key: 'warranty_policy', label: 'ضمان المنتجات' },
              { key: 'delivery_areas',  label: 'مناطق التوصيل' },
              { key: 'working_hours',   label: 'ساعات العمل' },
            ] as { key: keyof MerchantKnowledgePolicies; label: string }[]).map(({ key, label }) => (
              <div key={key} className="flex flex-col gap-1">
                <label className="text-[10px] font-semibold text-slate-500 uppercase tracking-wide">
                  {label}
                </label>
                <textarea
                  rows={3}
                  dir="rtl"
                  className="w-full rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-800 resize-none focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition placeholder-slate-300"
                  placeholder={`أدخل ${label}…`}
                  value={policies[key]}
                  onChange={e => {
                    setPolicies(prev => ({ ...prev, [key]: e.target.value }))
                    policiesDirty.current = true
                  }}
                />
              </div>
            ))}
          </div>
          <div className="flex items-center gap-3 pt-1">
            <button
              onClick={savePolicies}
              disabled={savingPolicies}
              className="btn-primary text-xs flex items-center gap-2 py-2 px-4"
            >
              {savingPolicies ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              حفظ السياسات
            </button>
            {policiesSaved && (
              <span className="text-xs text-emerald-600 flex items-center gap-1">
                <CheckCircle className="w-3.5 h-3.5" /> تم الحفظ
              </span>
            )}
          </div>
        </div>
      </CollapsibleSection>

      {/* ── Payment + Shipping ── */}
      <div className="grid sm:grid-cols-2 gap-5">
        <div className="card">
          <SectionHeader icon={CreditCard} title="طرق الدفع" count={payment_methods.length} />
          <div className="p-4">
            {payment_methods.length === 0
              ? <EmptySlot message="لا توجد طرق دفع — أضفها من إعدادات سلة" />
              : (
                <div className="flex flex-wrap gap-2">
                  {payment_methods.map((m, i) => (
                    <span key={i} className="text-xs bg-brand-50 text-brand-700 border border-brand-200 px-3 py-1 rounded-full font-medium">
                      {m}
                    </span>
                  ))}
                </div>
              )
            }
          </div>
        </div>

        <div className="card">
          <SectionHeader icon={Truck} title="طرق الشحن" count={shipping_methods.length} />
          <div className="p-4">
            {shipping_methods.length === 0
              ? <EmptySlot message="لا توجد طرق شحن — تحقق من المزامنة" />
              : (
                <div className="space-y-2">
                  {shipping_methods.map((m, i) => (
                    <div key={i} className="flex items-center gap-2 text-xs">
                      <span className="font-medium text-slate-800">{m.name}</span>
                      {m.cost && <span className="text-slate-500">— {m.cost} ر.س</span>}
                      {m.eta && <span className="text-slate-400 ms-auto">{m.eta}</span>}
                    </div>
                  ))}
                </div>
              )
            }
          </div>
        </div>
      </div>

      {/* ── FAQ (editable) ── */}
      <CollapsibleSection title={`الأسئلة الشائعة (${approvedFaqs.length} معتمد)`} icon={MessageSquare}>
        <div className="p-5 space-y-5">

          {/* Approved list */}
          <div>
            <p className="text-xs font-semibold text-slate-600 mb-2">
              معتمدة ({approvedFaqs.length})
              <span className="text-[10px] font-normal text-slate-400 ms-1">— يستخدمها الذكاء مباشرة</span>
            </p>
            {approvedFaqs.length === 0
              ? <EmptySlot message="لا توجد أسئلة شائعة معتمدة بعد — أضف أسئلة يطرحها عملاؤك" />
              : (
                <ul className="space-y-1.5">
                  {approvedFaqs.map((q, i) => (
                    <li key={i} className="flex items-start gap-2 text-xs group">
                      <CheckCircle className="w-3.5 h-3.5 text-emerald-500 shrink-0 mt-0.5" />
                      <span className="text-slate-700 flex-1">{q}</span>
                      <button
                        onClick={() => deleteFaq(i)}
                        title="حذف"
                        className="opacity-0 group-hover:opacity-100 transition-opacity text-red-400 hover:text-red-600 shrink-0"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </li>
                  ))}
                </ul>
              )
            }
          </div>

          {/* Suggested list */}
          {suggestedFaqs.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-amber-600 mb-2">
                مقترحة ({suggestedFaqs.length})
                <span className="text-[10px] font-normal text-amber-400 ms-1">— تحتاج موافقتك</span>
              </p>
              <ul className="space-y-1.5">
                {suggestedFaqs.map((q, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs group rounded-lg border border-amber-100 bg-amber-50 px-3 py-2">
                    <Sparkles className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                    <span className="text-slate-600 flex-1">{q}</span>
                    <button
                      onClick={() => approveSuggested(q)}
                      title="اعتماد"
                      className="text-emerald-500 hover:text-emerald-700 shrink-0 ms-1"
                    >
                      <ThumbsUp className="w-3.5 h-3.5" />
                    </button>
                    <button
                      onClick={() => deleteSuggested(q)}
                      title="حذف"
                      className="text-slate-400 hover:text-red-500 shrink-0"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Add new FAQ */}
          <div className="flex gap-2 items-center">
            <input
              type="text"
              dir="rtl"
              className="flex-1 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-brand-300 focus:border-brand-400 transition placeholder-slate-300"
              placeholder="أضف سؤالاً شائعاً جديداً…"
              value={newFaq}
              onChange={e => setNewFaq(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addFaq()}
            />
            <button
              onClick={addFaq}
              disabled={!newFaq.trim()}
              className="btn-secondary text-xs flex items-center gap-1.5 py-2 px-3 shrink-0"
            >
              <Plus className="w-3.5 h-3.5" />
              إضافة
            </button>
          </div>

          {/* Save button */}
          <div className="flex items-center gap-3">
            <button
              onClick={saveFaqs}
              disabled={savingFaq}
              className="btn-primary text-xs flex items-center gap-2 py-2 px-4"
            >
              {savingFaq ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              حفظ الأسئلة الشائعة
            </button>
            {faqSaved && (
              <span className="text-xs text-emerald-600 flex items-center gap-1">
                <CheckCircle className="w-3.5 h-3.5" /> تم الحفظ
              </span>
            )}
          </div>
        </div>
      </CollapsibleSection>

      {/* ── Pages ── */}
      <CollapsibleSection title="الصفحات الثابتة" icon={FileText} defaultOpen={false}>
        {pages.length === 0
          ? (
            <div className="p-6 text-center space-y-2">
              <FileText className="w-8 h-8 text-slate-200 mx-auto" />
              <p className="text-sm text-slate-500">الصفحات غير مربوطة بعد</p>
              <p className="text-xs text-slate-400">سيتم ربط صفحات المتجر (عن المتجر، سياسات، تواصل) تلقائياً عند تفعيل مزامنة الصفحات</p>
            </div>
          )
          : (
            <ul className="divide-y divide-slate-100">
              {pages.map((p, i) => (
                <li key={i} className="px-5 py-3 flex items-center gap-3">
                  <FileText className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="text-xs font-medium text-slate-800">{p.title}</span>
                  {p.url && <a href={p.url} target="_blank" rel="noopener noreferrer" className="text-[10px] text-brand-500 ms-auto hover:underline">{p.url}</a>}
                </li>
              ))}
            </ul>
          )
        }
      </CollapsibleSection>

      {/* ── Blocked Customers ── */}
      <CollapsibleSection title="العملاء المحظورون" icon={Shield} defaultOpen={false}>
        <div className="p-5 space-y-4">
          <p className="text-xs text-slate-500">أرقام هواتف العملاء المزعجين — سيتم تحويل رسائلهم مباشرةً إلى الدعم البشري دون رد آلي.</p>

          {/* Add phone input */}
          <div className="flex gap-2">
            <input
              type="text"
              value={newBlockedPhone}
              onChange={e => setNewBlockedPhone(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addBlockedPhone()}
              placeholder="+966XXXXXXXXX"
              dir="ltr"
              className="input-field text-xs flex-1 font-mono"
            />
            <button
              onClick={addBlockedPhone}
              className="btn-secondary text-xs flex items-center gap-1 px-3 py-2"
            >
              <Plus className="w-3.5 h-3.5" />
              إضافة
            </button>
          </div>

          {/* Blocked list */}
          {blockedCustomers.length === 0 ? (
            <p className="text-xs text-slate-400 text-center py-3">لا يوجد عملاء محظورون</p>
          ) : (
            <ul className="divide-y divide-slate-100">
              {blockedCustomers.map((phone) => (
                <li key={phone} className="flex items-center justify-between py-2">
                  <span className="text-xs font-mono text-slate-700 dir-ltr">{phone}</span>
                  <button
                    onClick={() => removeBlockedPhone(phone)}
                    className="text-red-400 hover:text-red-600 transition-colors"
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/* Save button */}
          <div className="flex items-center gap-3 pt-1">
            <button
              onClick={saveBlocked}
              disabled={savingBlocked}
              className="btn-primary text-xs flex items-center gap-2 py-2 px-4"
            >
              {savingBlocked ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
              حفظ القائمة
            </button>
            {blockedSaved && (
              <span className="text-xs text-emerald-600 flex items-center gap-1">
                <CheckCircle className="w-3.5 h-3.5" /> تم الحفظ
              </span>
            )}
          </div>
        </div>
      </CollapsibleSection>

      {/* ── Brain profile ── */}
      <div className="card p-5">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <Bot className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">إعدادات شخصية الذكاء المُحمَّلة</h2>
          <span className="text-[10px] text-slate-400 ms-1">— هذا ما تراه نحلة في كل محادثة</span>
          {onEditSettings && (
            <button
              type="button"
              onClick={onEditSettings}
              className="ms-auto btn-primary text-xs flex items-center gap-1.5 py-1.5 px-3"
            >
              <Pencil className="w-3.5 h-3.5" />
              تعديل الإعدادات
            </button>
          )}
        </div>
        <p className="text-[11px] text-slate-500 mb-4 leading-relaxed">
          هذه نسخة قراءة من الإعدادات الحالية. لتعديل النبرة، الطول، استراتيجية الكوبون، أو تعليمات المالك،
          اضغط <span className="font-semibold text-brand-600">«تعديل الإعدادات»</span> للانتقال إلى تبويب
          «إعدادات المساعد» حيث textarea وحقول التحرير وزر الحفظ.
        </p>
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3 text-xs">
          {[
            { label: 'نبرة الرد', value: brain_profile.tone === 'friendly' ? 'ودية' : brain_profile.tone === 'professional' ? 'احترافية' : brain_profile.tone },
            { label: 'طول الرد', value: brain_profile.reply_length === 'short' ? 'قصير' : brain_profile.reply_length === 'medium' ? 'متوسط' : 'تفصيلي' },
            { label: 'استراتيجية الكوبون', value: brain_profile.coupon_strategy === 'on_hesitation' ? 'عند التردد' : brain_profile.coupon_strategy },
            { label: 'توصيات المنتجات', value: brain_profile.upsell_enabled ? 'مفعّلة' : 'معطّلة' },
          ].map(({ label, value }) => (
            <div key={label} className="bg-slate-50 rounded-lg px-3 py-2.5">
              <p className="text-[10px] text-slate-400 mb-0.5">{label}</p>
              <p className="font-medium text-slate-800">{value}</p>
            </div>
          ))}
          {brain_profile.owner_instructions && (
            <div className="sm:col-span-2 lg:col-span-3 bg-brand-50 rounded-lg px-3 py-2.5 border border-brand-100">
              <p className="text-[10px] text-brand-500 mb-0.5">تعليمات المالك المُحمَّلة</p>
              <p className="text-slate-700 leading-relaxed line-clamp-3">{brain_profile.owner_instructions}</p>
            </div>
          )}
        </div>

        {/* ── SPL address auto-fill status ── */}
        <div className={`mt-4 rounded-lg px-3 py-2.5 flex items-center gap-2.5 text-xs border ${
          sync_status.spl_enabled
            ? 'bg-emerald-50 border-emerald-100 text-emerald-700'
            : 'bg-amber-50 border-amber-100 text-amber-700'
        }`}>
          <span className={`w-2 h-2 rounded-full shrink-0 ${sync_status.spl_enabled ? 'bg-emerald-500' : 'bg-amber-400'}`} />
          {sync_status.spl_enabled
            ? 'تحليل العنوان فعّال — الرموز الوطنية وروابط الخرائط تُحوَّل تلقائياً إلى عنوان كامل'
            : 'تحليل العنوان معطّل — أضف SPL_NATIONAL_ADDRESS_API_KEY في بيئة التشغيل لتفعيل auto-fill للعناوين'}
        </div>
      </div>

    </div>
  )
}

// ── Brain Analytics Panel ─────────────────────────────────────────────────────

const INTENT_LABELS: Record<string, string> = {
  greeting:       'ترحيب',
  ask_product:    'سؤال منتج',
  ask_price:      'سؤال سعر',
  start_order:    'بدء طلب',
  pay_now:        'طلب دفع',
  shipping:       'شحن / توصيل',
  hesitation:     'تردد',
  handoff:        'تحويل',
  track_order:    'تتبع طلب',
  pick_list_item: 'اختيار من قائمة',
  who_are_you:    'من أنت',
  other:          'أخرى',
}

function MiniBar({ value, max, color = 'bg-brand-400' }: { value: number; max: number; color?: string }) {
  const pct = max > 0 ? Math.max(4, Math.round((value / max) * 100)) : 4
  return (
    <div className="flex-1 bg-slate-100 rounded-full h-2 overflow-hidden">
      <div className={`${color} h-2 rounded-full transition-all`} style={{ width: `${pct}%` }} />
    </div>
  )
}

function FunnelStep({ label, count, total, color }: { label: string; count: number; total: number; color: string }) {
  const pct = total > 0 ? Math.round((count / total) * 100) : 0
  return (
    <div className="flex items-center gap-3 text-xs">
      <span className="w-28 text-end text-slate-600 shrink-0">{label}</span>
      <div className="flex-1 bg-slate-100 rounded-full h-5 overflow-hidden relative">
        <div className={`${color} h-5 rounded-full transition-all`} style={{ width: `${Math.max(pct, 2)}%` }} />
        {pct >= 12 && (
          <span className="absolute inset-0 flex items-center justify-center text-white text-[10px] font-semibold">
            {count.toLocaleString('ar-SA')}
          </span>
        )}
      </div>
      {pct < 12 && <span className="text-slate-700 font-semibold w-8 shrink-0">{count}</span>}
      <span className="text-slate-400 w-8 shrink-0 text-end">{pct}%</span>
    </div>
  )
}

function BrainAnalyticsPanel() {
  const [data, setData] = useState<ResponseQualityData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [period, setPeriod] = useState<'7' | '30'>('7')

  const load = useCallback(async () => {
    setLoading(true)
    setError(false)
    try {
      setData(await automationsApi.getResponseQuality())
    } catch {
      setError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  if (loading) return (
    <div className="flex flex-col items-center justify-center py-24 gap-4">
      <div className="w-10 h-10 border-4 border-brand-200 border-t-brand-500 rounded-full animate-spin" />
      <p className="text-sm text-slate-500">جارٍ تحميل مقاييس أداء الذكاء…</p>
    </div>
  )

  if (error || !data) return (
    <div className="flex flex-col items-center justify-center py-24 gap-4">
      <AlertTriangle className="w-10 h-10 text-red-400" />
      <p className="text-sm text-slate-600">تعذّر تحميل بيانات الأداء</p>
      <button onClick={load} className="btn-primary text-sm flex items-center gap-2">
        <RefreshCw className="w-4 h-4" /> إعادة المحاولة
      </button>
    </div>
  )

  const m = period === '7' ? data.last_7_days : data.last_30_days

  const maxDaily = Math.max(...data.daily.map(d => d.turns), 1)

  return (
    <div className="space-y-5">

      {/* ── Period toggle ── */}
      <div className="flex items-center gap-2">
        <span className="text-xs text-slate-500 me-1">الفترة:</span>
        {(['7', '30'] as const).map(p => (
          <button
            key={p}
            onClick={() => setPeriod(p)}
            className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
              period === p
                ? 'bg-brand-500 text-white border-brand-500'
                : 'bg-white text-slate-600 border-slate-200 hover:border-brand-300'
            }`}
          >
            {p === '7' ? 'آخر 7 أيام' : 'آخر 30 يوماً'}
          </button>
        ))}
        <button onClick={load} className="ms-auto btn-secondary text-xs flex items-center gap-1.5 py-1.5 px-3">
          <RefreshCw className="w-3.5 h-3.5" /> تحديث
        </button>
      </div>

      {/* ── KPI cards ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: 'إجمالي المحادثات', value: m.sessions_total, icon: MessageSquare, color: 'text-brand-600', bg: 'bg-brand-50' },
          { label: 'إجمالي الردود', value: m.turns_total, icon: Activity, color: 'text-blue-600', bg: 'bg-blue-50' },
          { label: 'طلبات مؤكدة', value: m.order_confirmed_count, icon: CheckCircle, color: 'text-emerald-600', bg: 'bg-emerald-50' },
          { label: 'متوسط وقت الرد', value: m.avg_latency_ms ? `${m.avg_latency_ms}ms` : '—', icon: Timer, color: 'text-slate-600', bg: 'bg-slate-100' },
        ].map(({ label, value, icon: Icon, color, bg }) => (
          <div key={label} className="card px-4 py-3 flex items-start gap-3">
            <div className={`${bg} rounded-lg p-2 shrink-0`}>
              <Icon className={`w-4 h-4 ${color}`} />
            </div>
            <div className="min-w-0">
              <p className={`text-lg font-bold ${color}`}>{typeof value === 'number' ? value.toLocaleString('ar-SA') : value}</p>
              <p className="text-[10px] text-slate-500 leading-tight">{label}</p>
            </div>
          </div>
        ))}
      </div>

      {/* ── Conversion funnel ── */}
      <div className="card p-5">
        <div className="flex items-center gap-2 mb-4">
          <BarChart2 className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">قمع التحويل</h2>
          <span className="text-[10px] text-slate-400 ms-1">— من رد إلى طلب مؤكد</span>
        </div>
        <div className="space-y-2.5">
          <FunnelStep label="ردود المساعد" count={m.turns_total} total={m.turns_total} color="bg-brand-400" />
          <FunnelStep label="بدأ طلباً" count={m.order_started_count} total={m.turns_total} color="bg-blue-400" />
          <FunnelStep label="أرسل رابط دفع" count={m.payment_link_count} total={m.turns_total} color="bg-amber-400" />
          <FunnelStep label="أكّد الطلب" count={m.order_confirmed_count} total={m.turns_total} color="bg-emerald-500" />
          <FunnelStep label="استخدم كوبون" count={m.coupon_redeemed_count} total={m.turns_total} color="bg-purple-400" />
          <FunnelStep label="حُوِّل لخدمة عملاء" count={m.handoff_count} total={m.turns_total} color="bg-red-400" />
        </div>
        {m.order_started_count > 0 && (
          <div className="mt-3 pt-3 border-t border-slate-100 flex gap-4 text-xs text-slate-500">
            <span>معدل التحويل: <strong className="text-emerald-600">{(m.conversion_rate * 100).toFixed(1)}%</strong></span>
            <span>معدل التحويل لخدمة عملاء: <strong className="text-red-500">{(m.handoff_rate * 100).toFixed(1)}%</strong></span>
          </div>
        )}
      </div>

      {/* ── Daily sparkline + Intents/Actions grid ── */}
      <div className="grid lg:grid-cols-2 gap-5">

        {/* Daily sparkline */}
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-4 flex items-center gap-2">
            <TrendingUp className="w-4 h-4 text-brand-500" /> النشاط اليومي (7 أيام)
          </h2>
          <div className="flex items-end gap-1.5 h-20">
            {data.daily.map(d => {
              const pct = maxDaily > 0 ? Math.max(4, Math.round((d.turns / maxDaily) * 100)) : 4
              return (
                <div key={d.date} className="flex-1 flex flex-col items-center gap-1 group relative">
                  <div
                    className="w-full bg-brand-400 rounded-t transition-all hover:bg-brand-500"
                    style={{ height: `${pct}%`, minHeight: 4 }}
                  />
                  {d.orders_confirmed > 0 && (
                    <div
                      className="w-1.5 h-1.5 rounded-full bg-emerald-500 absolute bottom-5"
                      title={`${d.orders_confirmed} طلبات مؤكدة`}
                    />
                  )}
                  <span className="text-[8px] text-slate-400 mt-0.5">
                    {new Date(d.date).toLocaleDateString('ar-SA', { weekday: 'short' })}
                  </span>
                </div>
              )
            })}
          </div>
          <p className="text-[10px] text-slate-400 mt-1 text-center">كل شريط = عدد ردود الذكاء • النقاط الخضراء = طلبات مؤكدة</p>
        </div>

        {/* Top intents */}
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-4 flex items-center gap-2">
            <Brain className="w-4 h-4 text-brand-500" /> أكثر النوايا تكراراً (30 يوم)
          </h2>
          {data.top_intents.length === 0
            ? <p className="text-xs text-slate-400 text-center py-4">لا توجد بيانات بعد</p>
            : (
              <div className="space-y-2.5">
                {data.top_intents.map(({ intent, count }) => {
                  const maxCount = data.top_intents[0]?.count || 1
                  return (
                    <div key={intent} className="flex items-center gap-2 text-xs">
                      <span className="w-24 text-end text-slate-600 shrink-0 truncate">
                        {INTENT_LABELS[intent] || intent}
                      </span>
                      <MiniBar value={count} max={maxCount} />
                      <span className="text-slate-700 font-semibold w-8 text-end shrink-0">{count}</span>
                    </div>
                  )
                })}
              </div>
            )
          }
        </div>
      </div>

      {/* ── Top actions ── */}
      {data.top_actions.length > 0 && (
        <div className="card p-5">
          <h2 className="text-sm font-semibold text-slate-900 mb-4 flex items-center gap-2">
            <Zap className="w-4 h-4 text-amber-500" /> أكثر الإجراءات تنفيذاً (30 يوم)
          </h2>
          <div className="grid sm:grid-cols-2 gap-2.5">
            {data.top_actions.map(({ action, count }) => {
              const maxCount = data.top_actions[0]?.count || 1
              return (
                <div key={action} className="flex items-center gap-2 text-xs">
                  <span className="w-32 text-end text-slate-600 shrink-0 truncate font-mono text-[10px]">{action}</span>
                  <MiniBar value={count} max={maxCount} color="bg-amber-400" />
                  <span className="text-slate-700 font-semibold w-8 text-end shrink-0">{count}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* ── Latency summary ── */}
      {(m.avg_latency_ms !== null) && (
        <div className="card p-5 flex items-center gap-6 flex-wrap">
          <Timer className="w-5 h-5 text-slate-400 shrink-0" />
          <div>
            <p className="text-[10px] text-slate-400 mb-0.5">متوسط وقت الاستجابة</p>
            <p className="text-xl font-bold text-slate-800">{m.avg_latency_ms} <span className="text-xs font-normal text-slate-400">ms</span></p>
          </div>
          {m.p95_latency_ms !== null && (
            <div>
              <p className="text-[10px] text-slate-400 mb-0.5">P95 (أبطأ 5%)</p>
              <p className="text-xl font-bold text-slate-800">{m.p95_latency_ms} <span className="text-xs font-normal text-slate-400">ms</span></p>
            </div>
          )}
          <div>
            <p className="text-[10px] text-slate-400 mb-0.5">متوسط الردود/محادثة</p>
            <p className="text-xl font-bold text-slate-800">{m.avg_turns_per_session}</p>
          </div>
        </div>
      )}

    </div>
  )
}

// ── AI Playground Panel ───────────────────────────────────────────────────────

function AIPlaygroundPanel() {
  const [message, setMessage] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<PlaygroundDryRunResponse | null>(null)

  const runDryRun = async () => {
    const text = message.trim()
    if (!text || loading) return
    setLoading(true)
    setError(null)
    setResult(null)
    try {
      const res = await playgroundApi.dryRun({ message: text })
      setResult(res)
    } catch {
      setError('تعذّر تشغيل الاختبار — حاول مجدداً')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-sky-200 bg-sky-50/80 px-4 py-3 flex items-start gap-2">
        <Info className="w-4 h-4 text-sky-600 shrink-0 mt-0.5" />
        <div className="text-xs text-sky-900 leading-relaxed">
          <span className="inline-flex items-center rounded-full bg-sky-100 px-2 py-0.5 text-[11px] font-semibold text-sky-800 mb-1">
            Dry Run — لن يتم إرسال أي رسالة واتساب
          </span>
          <p className="mt-1">
            اكتب رسالة اختبار لمعرفة ما كان الذكاء سيرد به — بدون إرسال حقيقي وبدون تغيير
            محادثات العملاء.
          </p>
        </div>
      </div>

      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <MessageSquare className="w-4 h-4 text-brand-500" />
          <h2 className="text-sm font-semibold text-slate-900">ساحة اختبار الذكاء</h2>
        </div>
        <div className="p-5 space-y-4">
          <Field
            label="رسالة العميل"
            hint="مثل: هل السدر متوفر؟ أو أرسل رقم التتبع"
          >
            <textarea
              className="input min-h-[120px] resize-y"
              value={message}
              onChange={e => setMessage(e.target.value)}
              placeholder="اكتب رسالة اختبار مثل: هل السدر متوفر؟"
            />
          </Field>
          <button
            type="button"
            onClick={() => { void runDryRun() }}
            disabled={loading || !message.trim()}
            className="btn-primary text-sm flex items-center gap-2 disabled:opacity-60"
          >
            {loading ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                جاري الاختبار…
              </>
            ) : (
              <>
                <Sparkles className="w-4 h-4" />
                اختبار الرد
              </>
            )}
          </button>
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
      </div>

      {result && (
        <div className="card border border-slate-200">
          <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-slate-900">نتيجة المعاينة</h3>
            <Badge variant={result.would_send ? 'green' : 'amber'}>
              {result.would_send ? 'سيرسل ردًا' : 'لن يرسل'}
            </Badge>
          </div>
          <div className="p-5 space-y-4 text-sm">
            {result.blocked_reason === 'store_ai_disabled' && (
              <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-2 text-xs text-violet-900">
                الذكاء متوقف للمتجر، لذلك لن يتم إرسال رد للعملاء. هذه نتيجة اختبار فقط.
              </div>
            )}

            <div>
              <p className="text-xs font-medium text-slate-500 mb-1">الرد المتوقع</p>
              <p className="text-slate-800 whitespace-pre-wrap rounded-lg bg-slate-50 px-3 py-2 min-h-[48px]">
                {result.reply_text || '—'}
              </p>
            </div>

            <div className="grid sm:grid-cols-2 gap-3 text-xs">
              <div className="rounded-lg bg-slate-50 px-3 py-2">
                <span className="text-slate-500">نوع الإرسال</span>
                <p className="font-medium text-slate-800 mt-0.5">{result.outbound_kind}</p>
              </div>
              <div className="rounded-lg bg-slate-50 px-3 py-2">
                <span className="text-slate-500">سبب المنع</span>
                <p className="font-medium text-slate-800 mt-0.5">{result.blocked_reason || '—'}</p>
              </div>
              <div className="rounded-lg bg-slate-50 px-3 py-2">
                <span className="text-slate-500">decision / topic</span>
                <p className="font-medium text-slate-800 mt-0.5">
                  {[result.decision_action, result.decision_topic].filter(Boolean).join(' · ') || '—'}
                </p>
              </div>
              <div className="rounded-lg bg-slate-50 px-3 py-2">
                <span className="text-slate-500">owner / LLM</span>
                <p className="font-medium text-slate-800 mt-0.5">
                  {result.owner || '—'} · {result.used_llm ? 'LLM' : 'FakeFacts'}
                </p>
              </div>
            </div>

            {result.needs_context && (
              <p className="text-xs text-amber-700 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
                يحتاج سياق طلب (order context) — لا يُخترَع رقم تتبع في v1.
              </p>
            )}

            {result.warnings.length > 0 && (
              <div>
                <p className="text-xs font-medium text-slate-500 mb-1">تحذيرات</p>
                <ul className="text-xs text-amber-800 space-y-1 list-disc list-inside">
                  {result.warnings.map(w => (
                    <li key={w}>{w}</li>
                  ))}
                </ul>
              </div>
            )}

            <div>
              <p className="text-xs font-medium text-slate-500 mb-1">Side effects</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(result.side_effects).map(([key, val]) => (
                  <span
                    key={key}
                    className={`text-[11px] px-2 py-1 rounded-full ${
                      val ? 'bg-red-100 text-red-700' : 'bg-emerald-50 text-emerald-700'
                    }`}
                  >
                    {key}: {val ? 'true' : 'false'}
                  </span>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function Intelligence() {
  useLanguage() // initialise RTL context

  // Default to the editable AI settings tab — merchants kept landing on the
  // dashboard / merchant-knowledge tabs and reporting "the AI page is read-only,
  // there is no save button". The settings tab is where the textareas + save
  // controls actually live, so it should be the entry point.
  const [activeTab, setActiveTab] = useState<'dashboard' | 'settings' | 'merchant' | 'analytics' | 'coupons' | 'media' | 'playground'>('settings')
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
          ) : activeTab === 'merchant' ? (
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <Store className="w-3.5 h-3.5" /> ما تعرفه نحلة عن متجرك
            </span>
          ) : activeTab === 'analytics' ? (
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <BarChart2 className="w-3.5 h-3.5" /> أداء الذكاء وجودة الردود
            </span>
          ) : activeTab === 'coupons' ? (
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <Tag className="w-3.5 h-3.5" /> أكواد خصم تستخدمها نحلة
            </span>
          ) : activeTab === 'media' ? (
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <ImageIcon className="w-3.5 h-3.5" /> صور وملفات ترسلها نحلة مع ردودها
            </span>
          ) : activeTab === 'playground' ? (
            <span className="text-xs text-slate-400 flex items-center gap-1.5">
              <Sparkles className="w-3.5 h-3.5" /> معاينة آمنة بدون إرسال واتساب
            </span>
          ) : undefined
        }
      />

      {/* ── Tabs ──────────────────────────────────────────────────────────── */}
      <div className="border-b border-slate-200 -mx-3 px-3 md:-mx-6 md:px-6">
        <div className="flex gap-1 overflow-x-auto">
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
          <button
            onClick={() => setActiveTab('merchant')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'merchant'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <Store className="w-4 h-4 shrink-0" />
            ذكاء المتجر
          </button>
          <button
            onClick={() => setActiveTab('coupons')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'coupons'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <Tag className="w-4 h-4 shrink-0" />
            الكوبونات اليدوية
          </button>
          <button
            onClick={() => setActiveTab('media')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'media'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <ImageIcon className="w-4 h-4 shrink-0" />
            مكتبة الوسائط
          </button>
          <button
            onClick={() => setActiveTab('playground')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'playground'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <Sparkles className="w-4 h-4 shrink-0" />
            ساحة اختبار الذكاء
          </button>
          <button
            onClick={() => setActiveTab('analytics')}
            className={`flex items-center gap-2 px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 -mb-px ${
              activeTab === 'analytics'
                ? 'border-brand-500 text-brand-600'
                : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'
            }`}
          >
            <BarChart2 className="w-4 h-4 shrink-0" />
            أداء الذكاء
          </button>
        </div>
      </div>

      {/* ── AI Settings Tab ────────────────────────────────────────────────── */}
      {activeTab === 'settings' && <AISettingsPanel />}

      {/* ── Merchant Knowledge Tab ─────────────────────────────────────────── */}
      {activeTab === 'merchant' && (
        <MerchantKnowledgePanel onEditSettings={() => setActiveTab('settings')} />
      )}

      {/* ── Manual Coupons Tab ─────────────────────────────────────────────── */}
      {activeTab === 'coupons' && <ManualCouponsPanel />}

      {/* ── AI Media Library Tab ───────────────────────────────────────────── */}
      {activeTab === 'media' && <AIMediaLibraryPanel />}

      {/* ── AI Playground Tab ──────────────────────────────────────────────── */}
      {activeTab === 'playground' && <AIPlaygroundPanel />}

      {/* ── Brain Analytics Tab ────────────────────────────────────────────── */}
      {activeTab === 'analytics' && <BrainAnalyticsPanel />}

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
