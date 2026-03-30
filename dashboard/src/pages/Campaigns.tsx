import { useState, useEffect, useCallback } from 'react'
import {
  Plus, Send, Users, ShoppingCart, BarChart2, CheckCircle, XCircle,
  Megaphone, ChevronRight, ChevronLeft, Tag, Crown, Zap, Clock,
  Smartphone, AlertCircle, RefreshCw, X, MessageSquare, FileText,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import Badge from '../components/ui/Badge'
import StatCard from '../components/ui/StatCard'
import PageHeader from '../components/ui/PageHeader'
import { useLanguage } from '../i18n/context'
import {
  campaignsApi, WaTemplate, CampaignRecord, CreateCampaignPayload,
  extractVariables, renderTemplate, getTemplateBody, getTemplateHeader, getTemplateFooter,
} from '../api/campaigns'

// ── Types ─────────────────────────────────────────────────────────────────────

type CampaignType = 'broadcast' | 'abandoned_cart' | 'vip' | 'new_arrivals' | 'win_back'
type AudienceType = 'all' | 'vip' | 'abandoned_cart' | 'inactive'
type ScheduleType = 'immediate' | 'scheduled' | 'delayed'

interface WizardState {
  step: number
  campaignType: CampaignType | null
  template: WaTemplate | null
  variables: Record<string, string>
  audienceType: AudienceType
  scheduleType: ScheduleType
  scheduleTime: string
  delayMinutes: number
  couponCode: string
  campaignName: string
  testPhone: string
  testSent: boolean
  testMessage: string
}

const INITIAL_WIZARD: WizardState = {
  step: 1,
  campaignType: null,
  template: null,
  variables: {},
  audienceType: 'all',
  scheduleType: 'immediate',
  scheduleTime: '',
  delayMinutes: 30,
  couponCode: '',
  campaignName: '',
  testPhone: '',
  testSent: false,
  testMessage: '',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const AUDIENCE_COUNTS: Record<AudienceType, number> = {
  all: 1850, vip: 310, abandoned_cart: 87, inactive: 420,
}

const CAMPAIGN_TYPES: { id: CampaignType; label: string; desc: string; icon: React.ReactNode; color: string }[] = [
  { id: 'broadcast',     label: 'بث جماعي',       desc: 'أرسل رسالة لجميع عملائك أو شريحة محددة',     icon: <Megaphone   className="w-6 h-6" />, color: 'text-blue-500 bg-blue-50 border-blue-200' },
  { id: 'abandoned_cart',label: 'عربة متروكة',     desc: 'استرجع العملاء الذين أضافوا منتجات ولم يكملوا', icon: <ShoppingCart className="w-6 h-6" />, color: 'text-amber-500 bg-amber-50 border-amber-200' },
  { id: 'vip',           label: 'عروض VIP',         desc: 'خصومات حصرية لعملائك الأكثر ولاءً',           icon: <Crown       className="w-6 h-6" />, color: 'text-purple-500 bg-purple-50 border-purple-200' },
  { id: 'new_arrivals',  label: 'وصول جديد',        desc: 'أبلغ عملاءك بأحدث منتجاتك',                  icon: <Zap         className="w-6 h-6" />, color: 'text-emerald-500 bg-emerald-50 border-emerald-200' },
  { id: 'win_back',      label: 'استرجاع العملاء',  desc: 'تواصل مع العملاء غير النشطين منذ فترة',       icon: <Users       className="w-6 h-6" />, color: 'text-rose-500 bg-rose-50 border-rose-200' },
]

const STATUS_META: Record<string, { label: string; variant: 'green' | 'amber' | 'blue' | 'slate' | 'red' }> = {
  active:    { label: 'نشطة',    variant: 'green' },
  scheduled: { label: 'مجدولة',  variant: 'amber' },
  completed: { label: 'مكتملة',  variant: 'blue'  },
  paused:    { label: 'موقوفة',  variant: 'red'   },
  draft:     { label: 'مسودة',   variant: 'slate' },
}

const TYPE_META: Record<string, { label: string; icon: React.ReactNode }> = {
  broadcast:     { label: 'بث جماعي',      icon: <Megaphone   className="w-3.5 h-3.5 text-blue-500" />  },
  abandoned_cart:{ label: 'عربة متروكة',   icon: <ShoppingCart className="w-3.5 h-3.5 text-amber-500" /> },
  vip:           { label: 'VIP',           icon: <Crown       className="w-3.5 h-3.5 text-purple-500" /> },
  new_arrivals:  { label: 'وصول جديد',     icon: <Zap         className="w-3.5 h-3.5 text-emerald-500" /> },
  win_back:      { label: 'استرجاع',       icon: <Users       className="w-3.5 h-3.5 text-rose-500" />   },
}

const AUDIENCE_LABELS: Record<AudienceType, string> = {
  all:           'جميع العملاء',
  vip:           'عملاء VIP فقط',
  abandoned_cart:'عربات متروكة',
  inactive:      'عملاء غير نشطين',
}

const STEP_LABELS = [
  'نوع الحملة', 'القالب', 'المتغيرات', 'الجمهور',
  'الجدولة', 'الكوبون', 'المعاينة', 'إرسال تجريبي',
]

// ── WhatsApp preview bubble ───────────────────────────────────────────────────

function WaPreview({ header, body, footer }: { header: string; body: string; footer: string }) {
  return (
    <div className="bg-[#e5ddd5] rounded-xl p-4 min-h-32 flex items-end">
      <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm max-w-xs p-3 text-sm space-y-1" dir="rtl">
        {header && <p className="font-semibold text-slate-900 text-xs">{header}</p>}
        {body && (
          <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">
            {body}
          </p>
        )}
        {footer && <p className="text-slate-400 text-[10px] mt-1">{footer}</p>}
        <p className="text-[10px] text-slate-300 text-end">✓✓ الآن</p>
      </div>
    </div>
  )
}

// ── Template card ─────────────────────────────────────────────────────────────

function TemplateCard({ tpl, selected, onClick }: { tpl: WaTemplate; selected: boolean; onClick: () => void }) {
  const header = getTemplateHeader(tpl)
  const body   = getTemplateBody(tpl)
  const vars   = extractVariables(body)

  return (
    <button
      onClick={onClick}
      className={`text-start border rounded-xl p-4 transition-all hover:shadow-md w-full ${
        selected
          ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
          : 'border-slate-200 bg-white hover:border-slate-300'
      }`}
    >
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs font-semibold text-slate-800">{tpl.name.replace(/_/g, ' ')}</p>
        <Badge label={tpl.category === 'MARKETING' ? 'تسويق' : 'خدمة'} variant={tpl.category === 'MARKETING' ? 'amber' : 'blue'} />
      </div>
      {header && <p className="text-xs font-medium text-slate-700 mb-1">{header}</p>}
      <p className="text-xs text-slate-500 line-clamp-2 mb-2" dir="rtl">{body}</p>
      {vars.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {vars.map(v => (
            <span key={v} className="text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-mono">{v}</span>
          ))}
        </div>
      )}
    </button>
  )
}

// ── Step components ───────────────────────────────────────────────────────────

function Step1Type({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">اختر نوع الحملة الذي يناسب هدفك التسويقي</p>
      <div className="grid sm:grid-cols-2 gap-3">
        {CAMPAIGN_TYPES.map(ct => (
          <button
            key={ct.id}
            onClick={() => setWiz(w => ({ ...w, campaignType: ct.id }))}
            className={`flex items-start gap-3 border rounded-xl p-4 text-start transition-all hover:shadow-md ${
              wiz.campaignType === ct.id
                ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
                : 'border-slate-200 bg-white hover:border-slate-300'
            }`}
          >
            <span className={`p-2 rounded-lg border ${ct.color}`}>{ct.icon}</span>
            <div>
              <p className="text-sm font-semibold text-slate-900">{ct.label}</p>
              <p className="text-xs text-slate-500 mt-0.5">{ct.desc}</p>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

function Step2Template({
  wiz, setWiz, templates, loading,
}: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>>; templates: WaTemplate[]; loading: boolean }) {
  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-slate-400">
        <RefreshCw className="w-5 h-5 animate-spin me-2" /> جارٍ تحميل القوالب…
      </div>
    )
  }
  if (templates.length === 0) {
    return (
      <div className="py-12 text-center space-y-3">
        <FileText className="w-10 h-10 text-slate-200 mx-auto" />
        <p className="text-sm text-slate-500">لا توجد قوالب معتمدة بعد.</p>
        <p className="text-xs text-slate-400">
          انتقل إلى{' '}
          <Link to="/templates" className="text-brand-500 underline font-medium">
            قوالب واتساب
          </Link>
          {' '}لإنشاء قالب وإرساله لـ Meta للاعتماد.
        </p>
      </div>
    )
  }
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">
        اختر قالب واتساب معتمد من Meta. لا يمكن إرسال رسائل غير مبنية على قالب معتمد.
      </p>
      <div className="grid sm:grid-cols-2 gap-3 max-h-80 overflow-y-auto pe-1">
        {templates.map(tpl => (
          <TemplateCard
            key={tpl.id}
            tpl={tpl}
            selected={wiz.template?.id === tpl.id}
            onClick={() => setWiz(w => ({ ...w, template: tpl, variables: {} }))}
          />
        ))}
      </div>
    </div>
  )
}

function Step3Variables({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  const body = getTemplateBody(wiz.template!)
  const vars = extractVariables(body)

  const VAR_HINTS: Record<string, string> = {
    '{{1}}': 'اسم العميل',
    '{{2}}': 'رابط أو قيمة',
    '{{3}}': 'كود الكوبون',
    '{{4}}': 'اسم المتجر',
  }

  if (vars.length === 0) {
    return (
      <div className="py-10 text-center text-sm text-slate-400 flex flex-col items-center gap-2">
        <CheckCircle className="w-8 h-8 text-emerald-400" />
        هذا القالب لا يحتوي على متغيرات — يمكنك المتابعة مباشرة.
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        حدد القيمة التي ستُستبدل بكل متغير عند إرسال الرسالة للعميل.
        يمكنك استخدام بيانات ديناميكية كاسم العميل ورابط العربة.
      </p>
      {vars.map(v => (
        <div key={v}>
          <label className="label flex items-center gap-2">
            <span className="font-mono text-amber-700 bg-amber-50 px-1.5 py-0.5 rounded text-[11px]">{v}</span>
            <span className="text-slate-600">{VAR_HINTS[v] ?? 'قيمة ديناميكية'}</span>
          </label>
          <input
            className="input text-sm"
            placeholder={`مثال: ${VAR_HINTS[v] ?? v}`}
            value={wiz.variables[v] ?? ''}
            onChange={e => setWiz(w => ({ ...w, variables: { ...w.variables, [v]: e.target.value } }))}
          />
        </div>
      ))}
    </div>
  )
}

function Step4Audience({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  const audiences: { id: AudienceType; icon: React.ReactNode }[] = [
    { id: 'all',           icon: <Users        className="w-4 h-4" /> },
    { id: 'vip',           icon: <Crown        className="w-4 h-4" /> },
    { id: 'abandoned_cart',icon: <ShoppingCart className="w-4 h-4" /> },
    { id: 'inactive',      icon: <Clock        className="w-4 h-4" /> },
  ]
  return (
    <div className="space-y-3">
      <p className="text-xs text-slate-500">حدد الشريحة المستهدفة لهذه الحملة</p>
      <div className="grid sm:grid-cols-2 gap-3">
        {audiences.map(a => (
          <button
            key={a.id}
            onClick={() => setWiz(w => ({ ...w, audienceType: a.id, campaignName: w.campaignName }))}
            className={`flex items-center gap-3 border rounded-xl p-4 text-start transition-all hover:shadow-md ${
              wiz.audienceType === a.id
                ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
                : 'border-slate-200 bg-white hover:border-slate-300'
            }`}
          >
            <span className="p-2 rounded-lg bg-slate-100 text-slate-600">{a.icon}</span>
            <div>
              <p className="text-sm font-semibold text-slate-900">{AUDIENCE_LABELS[a.id]}</p>
              <p className="text-xs text-slate-500">{AUDIENCE_COUNTS[a.id].toLocaleString('ar-SA')} عميل</p>
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

function Step5Schedule({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  return (
    <div className="space-y-5">
      <p className="text-xs text-slate-500">متى تريد إرسال هذه الحملة؟</p>
      <div className="grid gap-3">
        {([
          { id: 'immediate' as ScheduleType, label: 'إرسال فوري', desc: 'سيتم الإرسال بعد إطلاق الحملة مباشرةً', icon: <Send className="w-4 h-4" /> },
          { id: 'scheduled' as ScheduleType, label: 'جدولة بتاريخ محدد', desc: 'اختر تاريخاً ووقتاً للإرسال', icon: <Clock className="w-4 h-4" /> },
          { id: 'delayed'   as ScheduleType, label: 'تأخير محدد', desc: 'أرسل بعد مرور مدة زمنية من الحدث', icon: <RefreshCw className="w-4 h-4" /> },
        ] as const).map(opt => (
          <button
            key={opt.id}
            onClick={() => setWiz(w => ({ ...w, scheduleType: opt.id }))}
            className={`flex items-center gap-3 border rounded-xl p-4 text-start transition-all ${
              wiz.scheduleType === opt.id
                ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
                : 'border-slate-200 bg-white hover:border-slate-300'
            }`}
          >
            <span className="p-2 rounded-lg bg-slate-100 text-slate-600">{opt.icon}</span>
            <div>
              <p className="text-sm font-semibold text-slate-900">{opt.label}</p>
              <p className="text-xs text-slate-500">{opt.desc}</p>
            </div>
          </button>
        ))}
      </div>

      {wiz.scheduleType === 'scheduled' && (
        <div>
          <label className="label">تاريخ ووقت الإرسال</label>
          <input
            type="datetime-local"
            className="input text-sm"
            value={wiz.scheduleTime}
            onChange={e => setWiz(w => ({ ...w, scheduleTime: e.target.value }))}
          />
        </div>
      )}
      {wiz.scheduleType === 'delayed' && (
        <div>
          <label className="label">التأخير (بالدقائق)</label>
          <select
            className="input text-sm"
            value={wiz.delayMinutes}
            onChange={e => setWiz(w => ({ ...w, delayMinutes: Number(e.target.value) }))}
          >
            {[15, 30, 60, 120, 360, 720, 1440].map(m => (
              <option key={m} value={m}>{m < 60 ? `${m} دقيقة` : `${m / 60} ساعة`}</option>
            ))}
          </select>
        </div>
      )}
    </div>
  )
}

function Step6Coupon({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  const COUPONS = ['WELCOME20', 'VIP50', 'CART10AUTO', 'BULK100', '— بدون كوبون —']
  return (
    <div className="space-y-4">
      <p className="text-xs text-slate-500">
        اختر كوبون خصم اختياري لإرفاقه مع الرسالة. تأكد أن القالب يحتوي على متغير للكود.
      </p>
      <div className="grid gap-2">
        {COUPONS.map(c => (
          <button
            key={c}
            onClick={() => setWiz(w => ({ ...w, couponCode: c.startsWith('—') ? '' : c }))}
            className={`flex items-center gap-3 border rounded-xl px-4 py-3 text-start transition-all ${
              (c.startsWith('—') ? wiz.couponCode === '' : wiz.couponCode === c)
                ? 'border-brand-500 bg-brand-50 ring-2 ring-brand-200'
                : 'border-slate-200 bg-white hover:border-slate-300'
            }`}
          >
            {!c.startsWith('—') && <Tag className="w-4 h-4 text-brand-500 shrink-0" />}
            <span className="text-sm font-mono font-medium text-slate-800" dir={c.startsWith('—') ? 'rtl' : 'ltr'}>{c}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function Step7Preview({ wiz, setWiz }: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>> }) {
  const body   = renderTemplate(getTemplateBody(wiz.template!),   wiz.variables)
  const header = renderTemplate(getTemplateHeader(wiz.template!), wiz.variables)
  const footer = getTemplateFooter(wiz.template!)

  return (
    <div className="space-y-5">
      <p className="text-xs text-slate-500">هذا ما سيراه العميل في واتساب. تأكد من صحة المحتوى قبل الإرسال.</p>

      <div className="grid sm:grid-cols-2 gap-4">
        <div>
          <h4 className="text-xs font-medium text-slate-500 mb-2">تفاصيل الحملة</h4>
          <div className="space-y-2 text-xs">
            {[
              ['القالب',    wiz.template!.name.replace(/_/g, ' ')],
              ['الجمهور',   AUDIENCE_LABELS[wiz.audienceType] + ` (${AUDIENCE_COUNTS[wiz.audienceType].toLocaleString('ar-SA')} عميل)`],
              ['الجدولة',   wiz.scheduleType === 'immediate' ? 'فوري' : wiz.scheduleType === 'delayed' ? `بعد ${wiz.delayMinutes} دقيقة` : wiz.scheduleTime],
              ['الكوبون',   wiz.couponCode || 'بدون كوبون'],
            ].map(([k, v]) => (
              <div key={k} className="flex gap-2 bg-slate-50 rounded-lg px-3 py-2">
                <span className="text-slate-400 w-20 shrink-0">{k}</span>
                <span className="font-medium text-slate-800 truncate">{v}</span>
              </div>
            ))}
          </div>
          <div className="mt-4">
            <label className="label">اسم الحملة</label>
            <input
              className="input text-sm"
              placeholder="مثال: حملة رمضان 2026"
              value={wiz.campaignName}
              onChange={e => setWiz(w => ({ ...w, campaignName: e.target.value }))}
            />
          </div>
        </div>

        <div>
          <h4 className="text-xs font-medium text-slate-500 mb-2">معاينة الرسالة</h4>
          <WaPreview header={header} body={body} footer={footer} />
        </div>
      </div>
    </div>
  )
}

function Step8TestSend({
  wiz, setWiz, onTestSend, testLoading,
}: { wiz: WizardState; setWiz: React.Dispatch<React.SetStateAction<WizardState>>; onTestSend: () => void; testLoading: boolean }) {
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-4">
        <AlertCircle className="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
        <p className="text-xs text-amber-800">
          سيتم إرسال رسالة اختبار حقيقية إلى الرقم المُدخل. تأكد أن الرقم صحيح وأنك تمتلك صلاحية الإرسال.
        </p>
      </div>

      <div>
        <label className="label">رقم الهاتف للاختبار</label>
        <div className="flex gap-2">
          <input
            className="input text-sm flex-1"
            placeholder="+966 50 000 0000"
            dir="ltr"
            value={wiz.testPhone}
            onChange={e => setWiz(w => ({ ...w, testPhone: e.target.value, testSent: false, testMessage: '' }))}
          />
          <button
            onClick={onTestSend}
            disabled={!wiz.testPhone || testLoading}
            className="btn-primary text-sm shrink-0"
          >
            {testLoading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            إرسال تجريبي
          </button>
        </div>
      </div>

      {wiz.testSent && (
        <div className="flex items-center gap-2 text-emerald-700 bg-emerald-50 border border-emerald-200 rounded-xl p-3">
          <CheckCircle className="w-4 h-4 shrink-0" />
          <p className="text-xs">{wiz.testMessage}</p>
        </div>
      )}
    </div>
  )
}

// ── Wizard modal ──────────────────────────────────────────────────────────────

function CampaignWizard({
  onClose, onCreated,
}: { onClose: () => void; onCreated: (c: CampaignRecord) => void }) {
  const [wiz, setWiz] = useState<WizardState>(INITIAL_WIZARD)
  const [templates, setTemplates] = useState<WaTemplate[]>([])
  const [templatesLoading, setTemplatesLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testLoading, setTestLoading] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    campaignsApi.getTemplates()
      .then(r => setTemplates(r.templates))
      .catch(() => setTemplates([]))
      .finally(() => setTemplatesLoading(false))
  }, [])

  const canNext = (): boolean => {
    if (wiz.step === 1) return !!wiz.campaignType
    if (wiz.step === 2) return !!wiz.template
    if (wiz.step === 7) return !!wiz.campaignName.trim()
    return true
  }

  const next = () => setWiz(w => ({ ...w, step: Math.min(w.step + 1, 8) }))
  const prev = () => setWiz(w => ({ ...w, step: Math.max(w.step - 1, 1) }))

  const handleTestSend = async () => {
    if (!wiz.testPhone || !wiz.template) return
    setTestLoading(true)
    try {
      const res = await campaignsApi.testSend(
        wiz.testPhone,
        wiz.template.id,
        wiz.template.name,
        wiz.template.language,
        wiz.variables,
      )
      setWiz(w => ({ ...w, testSent: true, testMessage: res.message }))
    } catch {
      setWiz(w => ({ ...w, testSent: true, testMessage: 'حدث خطأ أثناء الإرسال التجريبي.' }))
    } finally {
      setTestLoading(false)
    }
  }

  const handleLaunch = async () => {
    if (!wiz.template || !wiz.campaignType) return
    setSaving(true)
    setError('')
    try {
      const payload: CreateCampaignPayload = {
        name: wiz.campaignName,
        campaign_type: wiz.campaignType,
        template_id: wiz.template.id,
        template_name: wiz.template.name,
        template_language: wiz.template.language,
        template_category: wiz.template.category,
        template_body: getTemplateBody(wiz.template),
        template_variables: wiz.variables,
        audience_type: wiz.audienceType,
        audience_count: AUDIENCE_COUNTS[wiz.audienceType],
        schedule_type: wiz.scheduleType,
        schedule_time: wiz.scheduleType === 'scheduled' ? wiz.scheduleTime : undefined,
        delay_minutes: wiz.scheduleType === 'delayed' ? wiz.delayMinutes : undefined,
        coupon_code: wiz.couponCode,
      }
      const created = await campaignsApi.create(payload)
      onCreated(created)
      onClose()
    } catch {
      setError('حدث خطأ أثناء إنشاء الحملة. حاول مجدداً.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">إنشاء حملة واتساب</h2>
            <p className="text-xs text-slate-400 mt-0.5">{STEP_LABELS[wiz.step - 1]} — الخطوة {wiz.step} من 8</p>
          </div>
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Step progress bar */}
        <div className="px-6 pt-4">
          <div className="flex gap-1">
            {Array.from({ length: 8 }, (_, i) => (
              <div
                key={i}
                className={`h-1 flex-1 rounded-full transition-colors ${
                  i + 1 < wiz.step ? 'bg-brand-500' : i + 1 === wiz.step ? 'bg-brand-300' : 'bg-slate-100'
                }`}
              />
            ))}
          </div>
        </div>

        {/* Step content */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {wiz.step === 1 && <Step1Type wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 2 && <Step2Template wiz={wiz} setWiz={setWiz} templates={templates} loading={templatesLoading} />}
          {wiz.step === 3 && <Step3Variables wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 4 && <Step4Audience wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 5 && <Step5Schedule wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 6 && <Step6Coupon wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 7 && <Step7Preview wiz={wiz} setWiz={setWiz} />}
          {wiz.step === 8 && <Step8TestSend wiz={wiz} setWiz={setWiz} onTestSend={handleTestSend} testLoading={testLoading} />}
        </div>

        {/* Footer nav */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-slate-100">
          <button
            onClick={prev}
            disabled={wiz.step === 1}
            className="btn-ghost text-sm disabled:opacity-30"
          >
            <ChevronRight className="w-4 h-4" /> السابق
          </button>

          {error && <p className="text-xs text-red-500">{error}</p>}

          {wiz.step < 8 ? (
            <button
              onClick={next}
              disabled={!canNext()}
              className="btn-primary text-sm disabled:opacity-40"
            >
              التالي <ChevronLeft className="w-4 h-4" />
            </button>
          ) : (
            <button
              onClick={handleLaunch}
              disabled={saving}
              className="btn-primary text-sm"
            >
              {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
              {saving ? 'جارٍ الإنشاء…' : 'إطلاق الحملة'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Campaign list row ─────────────────────────────────────────────────────────

function CampaignRow({ campaign, onStatusChange }: { campaign: CampaignRecord; onStatusChange: (id: number, status: string) => void }) {
  const sm = STATUS_META[campaign.status] ?? STATUS_META['draft']
  const tm = TYPE_META[campaign.campaign_type] ?? TYPE_META['broadcast']
  const openRate = campaign.sent_count > 0 ? Math.round((campaign.read_count / campaign.sent_count) * 100) : 0
  const convRate = campaign.sent_count > 0 ? Math.round((campaign.converted_count / campaign.sent_count) * 100) : 0

  return (
    <tr className="hover:bg-slate-50 transition-colors">
      <td className="px-5 py-3.5">
        <p className="text-xs font-semibold text-slate-900">{campaign.name}</p>
        <p className="text-[10px] text-slate-400 font-mono mt-0.5">{campaign.template_name?.replace(/_/g, ' ')}</p>
      </td>
      <td className="px-5 py-3.5">
        <span className="flex items-center gap-1.5 text-xs text-slate-600">{tm.icon} {tm.label}</span>
      </td>
      <td className="px-5 py-3.5">
        <Badge label={sm.label} variant={sm.variant} dot />
      </td>
      <td className="px-5 py-3.5 text-xs text-slate-700">{campaign.audience_count.toLocaleString('ar-SA')}</td>
      <td className="px-5 py-3.5 text-xs text-slate-700">{campaign.sent_count.toLocaleString('ar-SA')}</td>
      <td className="px-5 py-3.5">
        <span className="text-xs text-slate-700">
          {campaign.sent_count > 0 ? `${campaign.read_count} (${openRate}%)` : '—'}
        </span>
      </td>
      <td className="px-5 py-3.5">
        <span className={`text-xs font-medium ${campaign.converted_count > 0 ? 'text-emerald-600' : 'text-slate-400'}`}>
          {campaign.sent_count > 0 ? `${campaign.converted_count} (${convRate}%)` : '—'}
        </span>
      </td>
      <td className="px-5 py-3.5">
        {campaign.status === 'active' && (
          <button
            onClick={() => onStatusChange(campaign.id, 'paused')}
            className="text-xs text-red-400 hover:text-red-600 transition-colors flex items-center gap-1"
          >
            <XCircle className="w-3.5 h-3.5" /> إيقاف
          </button>
        )}
        {campaign.status === 'paused' && (
          <button
            onClick={() => onStatusChange(campaign.id, 'active')}
            className="text-xs text-brand-500 hover:text-brand-700 transition-colors flex items-center gap-1"
          >
            <Send className="w-3.5 h-3.5" /> استئناف
          </button>
        )}
        {campaign.status === 'draft' && (
          <button
            onClick={() => onStatusChange(campaign.id, 'active')}
            className="text-xs text-brand-500 hover:text-brand-700 transition-colors flex items-center gap-1"
          >
            <Send className="w-3.5 h-3.5" /> إطلاق
          </button>
        )}
      </td>
    </tr>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

const TABLE_HEADERS = ['الحملة', 'النوع', 'الحالة', 'الجمهور', 'المُرسَل', 'معدل القراءة', 'التحويل', '']

export default function Campaigns() {
  const [showWizard, setShowWizard] = useState(false)
  const [campaigns, setCampaigns] = useState<CampaignRecord[]>([])
  const [loading, setLoading] = useState(true)
  const { t } = useLanguage()

  const loadCampaigns = useCallback(() => {
    setLoading(true)
    campaignsApi.list()
      .then(r => setCampaigns(r.campaigns))
      .catch(() => setCampaigns([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { loadCampaigns() }, [loadCampaigns])

  const handleStatusChange = async (id: number, status: string) => {
    try {
      const updated = await campaignsApi.updateStatus(id, status)
      setCampaigns(cs => cs.map(c => c.id === updated.id ? updated : c))
    } catch { /* ignore */ }
  }

  const handleCreated = (c: CampaignRecord) => {
    setCampaigns(cs => [c, ...cs])
  }

  // Compute summary stats from loaded campaigns
  const activeCampaigns = campaigns.filter(c => c.status === 'active').length
  const totalSent = campaigns.reduce((s, c) => s + c.sent_count, 0)
  const totalRead = campaigns.reduce((s, c) => s + c.read_count, 0)
  const totalConverted = campaigns.reduce((s, c) => s + c.converted_count, 0)
  const avgOpenRate = totalSent > 0 ? `${Math.round((totalRead / totalSent) * 100)}%` : '—'

  return (
    <div className="space-y-5">
      {showWizard && (
        <CampaignWizard onClose={() => setShowWizard(false)} onCreated={handleCreated} />
      )}

      <PageHeader
        title={t(tr => tr.pages.campaigns.title)}
        subtitle={t(tr => tr.pages.campaigns.subtitle)}
        action={
          <button onClick={() => setShowWizard(true)} className="btn-primary text-sm">
            <Plus className="w-4 h-4" /> {t(tr => tr.actions.newCampaign)}
          </button>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="حملات نشطة"     value={String(activeCampaigns)}                    change={0}   icon={Megaphone}     iconColor="text-brand-600"   iconBg="bg-brand-50" />
        <StatCard label="رسائل مُرسَلة"  value={totalSent.toLocaleString('ar-SA')}           change={18}  icon={Send}           iconColor="text-blue-600"    iconBg="bg-blue-50" />
        <StatCard label="معدل القراءة"   value={avgOpenRate}                                 change={4.2} icon={BarChart2}      iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label="التحويلات"      value={totalConverted.toLocaleString('ar-SA')}      change={9.1} icon={CheckCircle}    iconColor="text-purple-600"  iconBg="bg-purple-50" />
      </div>

      {/* Compliance notice */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-200 rounded-xl px-4 py-3">
        <MessageSquare className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
        <p className="text-xs text-blue-800">
          <span className="font-semibold">تنبيه الامتثال: </span>
          جميع الحملات تعتمد على قوالب واتساب معتمدة من Meta. لا يمكن إرسال رسائل حرة خارج القوالب المعتمدة وفقاً لسياسة واتساب للأعمال.
        </p>
      </div>

      {/* Campaigns table */}
      <div className="card">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-900">الحملات</h2>
          <button onClick={loadCampaigns} className="text-slate-400 hover:text-slate-600">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        {loading ? (
          <div className="py-16 text-center text-sm text-slate-400 flex items-center justify-center gap-2">
            <RefreshCw className="w-4 h-4 animate-spin" /> جارٍ التحميل…
          </div>
        ) : campaigns.length === 0 ? (
          <div className="py-16 text-center space-y-3">
            <Megaphone className="w-10 h-10 text-slate-200 mx-auto" />
            <p className="text-sm text-slate-400">لا توجد حملات بعد.</p>
            <button onClick={() => setShowWizard(true)} className="btn-primary text-sm mx-auto">
              <Plus className="w-4 h-4" /> أنشئ حملتك الأولى
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-100">
                  {TABLE_HEADERS.map((h, i) => (
                    <th key={i} className="text-start px-5 py-3 text-xs font-medium text-slate-500 uppercase tracking-wide whitespace-nowrap">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {campaigns.map(c => (
                  <CampaignRow key={c.id} campaign={c} onStatusChange={handleStatusChange} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Template compliance info */}
      <div className="card p-5">
        <div className="flex items-start gap-3">
          <Smartphone className="w-5 h-5 text-brand-500 shrink-0 mt-0.5" />
          <div className="flex-1">
            <div className="flex items-center justify-between mb-1">
              <h3 className="text-sm font-semibold text-slate-900">قوالب واتساب المعتمدة</h3>
              <Link to="/templates" className="text-xs text-brand-500 hover:text-brand-700 flex items-center gap-1">
                <FileText className="w-3.5 h-3.5" /> إدارة القوالب
              </Link>
            </div>
            <p className="text-xs text-slate-500 mt-0.5 mb-3">
              الحملات تستخدم فقط القوالب ذات الحالة APPROVED. يمكنك إنشاء قوالب جديدة من صفحة قوالب واتساب وإرسالها لـ Meta للمراجعة.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}
