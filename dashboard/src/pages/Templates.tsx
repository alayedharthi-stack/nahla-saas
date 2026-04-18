import { useState, useEffect, useCallback } from 'react'
import {
  Plus, RefreshCw, CheckCircle, Clock, XCircle, AlertCircle,
  Eye, Trash2, ChevronLeft, ChevronRight, X, MessageSquare,
  Type, Link2, Phone, Copy as CopyIcon, Zap, Star,
  BookOpen, Download, Sparkles, Tag, Search, Bot, CheckCheck,
  Pencil, Send,
} from 'lucide-react'
import Badge from '../components/ui/Badge'
import PageHeader from '../components/ui/PageHeader'
import StatCard from '../components/ui/StatCard'
import { useLanguage } from '../i18n/context'
import {
  templatesApi, WhatsAppTemplateRecord, CreateTemplatePayload,
  TemplateStatus, TemplateCategory, TemplateComponent, TemplateButton,
  TemplateVarMapRecord, NahlaLibraryTemplate,
  getBody, getHeader, getFooter, getButtons,
  extractVars, renderBody, countVars,
  STATUS_COLORS, STATUS_LABELS, CATEGORY_LABELS, LANGUAGE_LABELS,
} from '../api/templates'

// ── Default templates metadata ────────────────────────────────────────────────

const DEFAULT_TEMPLATE_META: Record<string, {
  purposeLabel: string
  automationLabel: string
  automationType: string
  varLabels: Record<string, string>
}> = {
  order_status_update_ar: {
    purposeLabel: 'إشعار تحديث حالة الطلب',
    automationLabel: 'إشعارات الطلبات',
    automationType: 'order_status_update',
    varLabels: { '{{1}}': 'اسم العميل', '{{2}}': 'رقم الطلب', '{{3}}': 'حالة الطلب' },
  },
  cod_order_confirmation_ar: {
    purposeLabel: 'تأكيد الطلب النقدي',
    automationLabel: 'الطلبات بالدفع عند الاستلام',
    automationType: 'order_status_update',
    varLabels: { '{{1}}': 'اسم العميل', '{{2}}': 'اسم المنتج', '{{3}}': 'مبلغ الطلب' },
  },
  predictive_reorder_reminder_ar: {
    purposeLabel: 'تذكير إعادة الطلب التنبؤي',
    automationLabel: 'predictive_reorder',
    automationType: 'predictive_reorder',
    varLabels: { '{{1}}': 'اسم العميل', '{{2}}': 'اسم المنتج', '{{3}}': 'رابط إعادة الطلب' },
  },
}

function isDefaultTemplate(name: string) {
  return name in DEFAULT_TEMPLATE_META
}

// ── WhatsApp bubble preview ───────────────────────────────────────────────────

function WaPreview({
  header, body, footer, buttons,
}: { header: string; body: string; footer: string; buttons: TemplateButton[] }) {
  return (
    <div className="bg-[#e5ddd5] rounded-xl p-4 flex items-end min-h-28">
      <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm max-w-xs w-full p-3 space-y-1" dir="rtl">
        {header && <p className="font-semibold text-slate-900 text-xs border-b border-slate-100 pb-1">{header}</p>}
        {body && (
          <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">{body}</p>
        )}
        {footer && <p className="text-[10px] text-slate-400 mt-1">{footer}</p>}
        {buttons.length > 0 && (
          <div className="border-t border-slate-100 pt-2 space-y-1">
            {buttons.map((btn, i) => (
              <div key={i} className="text-center text-xs text-blue-600 font-medium py-0.5">
                {btn.text}
              </div>
            ))}
          </div>
        )}
        <p className="text-[10px] text-slate-300 text-end">✓✓ الآن</p>
      </div>
    </div>
  )
}

// ── Template row ──────────────────────────────────────────────────────────────

function TemplateRow({
  tpl, onPreview, onDelete, onSubmit, onEdit, isSubmitting,
}: { tpl: WhatsAppTemplateRecord; onPreview: () => void; onDelete: () => void; onSubmit: () => void; onEdit: () => void; isSubmitting?: boolean }) {
  const vars = countVars(tpl)
  const sm = (STATUS_COLORS[tpl.status] ?? 'slate') as 'green' | 'amber' | 'red' | 'slate' | 'purple'
  const isDefault = isDefaultTemplate(tpl.name)
  const meta = DEFAULT_TEMPLATE_META[tpl.name]
  const compatibility = tpl.compatibility

  return (
    <tr className={`hover:bg-slate-50 transition-colors ${isDefault ? 'bg-brand-50/30' : ''}`}>
      <td className="px-5 py-3.5">
        <div className="flex items-center gap-2 flex-wrap">
          <p className="text-xs font-semibold text-slate-900">{tpl.name.replace(/_/g, ' ')}</p>
          {isDefault && (
            <span className="inline-flex items-center gap-1 text-[10px] bg-brand-100 text-brand-700 border border-brand-200 px-1.5 py-0.5 rounded-full font-medium">
              <Star className="w-2.5 h-2.5" />
              افتراضي
            </span>
          )}
        </div>
        {isDefault && meta && (
          <p className="text-[10px] text-brand-600 mt-0.5 flex items-center gap-1">
            <Zap className="w-2.5 h-2.5" />
            {meta.purposeLabel}
          </p>
        )}
        {!isDefault && tpl.meta_template_id && (
          <p className="text-[10px] text-slate-400 font-mono mt-0.5">{tpl.meta_template_id}</p>
        )}
      </td>
      <td className="px-5 py-3.5 text-xs text-slate-600">{LANGUAGE_LABELS[tpl.language] ?? tpl.language}</td>
      <td className="px-5 py-3.5">
        <Badge
          label={CATEGORY_LABELS[tpl.category as TemplateCategory] ?? tpl.category}
          variant={tpl.category === 'MARKETING' ? 'amber' : tpl.category === 'UTILITY' ? 'blue' : 'purple'}
        />
      </td>
      <td className="px-5 py-3.5">
        <Badge label={STATUS_LABELS[tpl.status] ?? tpl.status} variant={sm} dot />
        {tpl.status === 'REJECTED' && tpl.rejection_reason && (
          <p className="text-[10px] text-red-500 mt-0.5 max-w-xs truncate">{tpl.rejection_reason}</p>
        )}
        {compatibility?.issues?.[0] && tpl.status !== 'REJECTED' && (
          <p className="text-[10px] text-slate-500 mt-0.5 max-w-xs truncate">{compatibility.issues[0]}</p>
        )}
      </td>
      <td className="px-5 py-3.5">
        <div className="flex flex-col items-start gap-1">
          {vars > 0 ? (
            <span className="inline-flex items-center gap-1 text-xs text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full">
              {vars} متغير
            </span>
          ) : (
            <span className="text-xs text-slate-300">—</span>
          )}
          {compatibility && (
            <span className={`inline-flex items-center gap-1 text-[10px] px-2 py-0.5 rounded-full ${
              compatibility.compatibility === 'compatible'
                ? 'bg-emerald-50 text-emerald-700'
                : compatibility.compatibility === 'pending_meta'
                ? 'bg-amber-50 text-amber-700'
                : 'bg-slate-100 text-slate-600'
            }`}>
              {compatibility.compatibility === 'compatible'
                ? 'متوافق'
                : compatibility.compatibility === 'pending_meta'
                ? 'بانتظار اعتماد Meta'
                : 'يحتاج مراجعة'}
            </span>
          )}
        </div>
      </td>
      <td className="px-5 py-3.5 text-xs text-slate-400 whitespace-nowrap" dir="ltr">
        {tpl.updated_at ? new Date(tpl.updated_at).toLocaleDateString('ar-SA') : '—'}
      </td>
      <td className="px-5 py-3.5">
        <div className="flex items-center gap-2">
          <button
            onClick={onPreview}
            className="text-slate-400 hover:text-brand-500 transition-colors"
            title="معاينة"
          >
            <Eye className="w-4 h-4" />
          </button>
          {tpl.status !== 'APPROVED' && (
            <button
              onClick={onEdit}
              className="text-slate-400 hover:text-amber-500 transition-colors"
              title="تعديل"
            >
              <Pencil className="w-4 h-4" />
            </button>
          )}
          {tpl.submittable && (
            <button
              onClick={onSubmit}
              disabled={isSubmitting}
              className={`flex items-center gap-1 transition-colors text-[11px] font-medium px-2 py-1 rounded-lg ${
                isSubmitting
                  ? 'text-slate-400 bg-slate-100 cursor-not-allowed'
                  : 'text-brand-500 hover:text-brand-700 bg-brand-50 hover:bg-brand-100'
              }`}
              title={isSubmitting ? 'جارٍ الإرسال…' : 'إرسال إلى Meta للمراجعة'}
            >
              {isSubmitting
                ? <RefreshCw className="w-3 h-3 animate-spin" />
                : <Send className="w-3 h-3" />
              }
              {isSubmitting ? 'جارٍ الإرسال…' : 'إرسال لـ Meta'}
            </button>
          )}
          {tpl.status !== 'APPROVED' && (
            <button
              onClick={onDelete}
              className="text-slate-300 hover:text-red-500 transition-colors"
              title="حذف"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      </td>
    </tr>
  )
}

// ── Preview modal ─────────────────────────────────────────────────────────────

function PreviewModal({ tpl, onClose }: { tpl: WhatsAppTemplateRecord; onClose: () => void }) {
  const [vars, setVars] = useState<Record<string, string>>({})
  const [varMapData, setVarMapData] = useState<TemplateVarMapRecord | null>(null)

  const bodyRaw = getBody(tpl)
  const varKeys = extractVars(bodyRaw)
  const footer  = getFooter(tpl)
  const buttons = getButtons(tpl)
  const isDefault = isDefaultTemplate(tpl.name)
  const defaultMeta = DEFAULT_TEMPLATE_META[tpl.name]

  // Pre-fill var inputs with Arabic placeholder labels from default meta
  const getVarPlaceholder = (varKey: string): string => {
    if (defaultMeta?.varLabels[varKey]) return defaultMeta.varLabels[varKey]
    if (varMapData?.var_map_annotated[varKey]) return varMapData.var_map_annotated[varKey].label
    return `قيمة ${varKey}`
  }

  // Fetch var map from API for non-default templates
  useEffect(() => {
    if (!isDefault && tpl.id) {
      templatesApi.getVarMap(tpl.id)
        .then(setVarMapData)
        .catch(() => {/* non-critical */})
    }
  }, [tpl.id, isDefault])

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-lg flex flex-col max-h-[90vh]">
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-bold text-slate-900">معاينة القالب</h2>
            {isDefault && (
              <span className="inline-flex items-center gap-1 text-[10px] bg-brand-100 text-brand-700 border border-brand-200 px-1.5 py-0.5 rounded-full font-medium">
                <Star className="w-2.5 h-2.5" />
                افتراضي
              </span>
            )}
          </div>
          <button onClick={onClose}><X className="w-5 h-5 text-slate-400 hover:text-slate-600" /></button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-5">
          {/* Meta info */}
          <div className="grid grid-cols-3 gap-3 text-xs">
            <div className="bg-slate-50 rounded-lg p-2.5">
              <p className="text-slate-400 mb-0.5">الاسم</p>
              <p className="font-medium text-slate-800 truncate">{tpl.name.replace(/_/g, ' ')}</p>
            </div>
            <div className="bg-slate-50 rounded-lg p-2.5">
              <p className="text-slate-400 mb-0.5">الفئة</p>
              <p className="font-medium text-slate-800">{CATEGORY_LABELS[tpl.category as TemplateCategory]}</p>
            </div>
            <div className="bg-slate-50 rounded-lg p-2.5">
              <p className="text-slate-400 mb-0.5">الحالة</p>
              <Badge label={STATUS_LABELS[tpl.status] ?? tpl.status} variant={(STATUS_COLORS[tpl.status] ?? 'slate') as 'green' | 'amber' | 'red' | 'slate' | 'purple'} dot />
            </div>
          </div>

          {tpl.compatibility && (
            <div className="bg-slate-50 rounded-xl p-3 space-y-2">
              <p className="text-xs font-semibold text-slate-700">توافق القالب مع نحلة</p>
              <div className="flex flex-wrap gap-2">
                <Badge
                  label={
                    tpl.compatibility.compatibility === 'compatible'
                      ? 'متوافق'
                      : tpl.compatibility.compatibility === 'pending_meta'
                      ? 'بانتظار اعتماد Meta'
                      : 'يحتاج مراجعة'
                  }
                  variant={
                    tpl.compatibility.compatibility === 'compatible'
                      ? 'green'
                      : tpl.compatibility.compatibility === 'pending_meta'
                      ? 'amber'
                      : 'slate'
                  }
                />
                <Badge
                  label={`${tpl.compatibility.placeholder_count} متغير`}
                  variant="blue"
                />
              </div>
              {!!tpl.compatibility.supported_features?.length && (
                <p className="text-[11px] text-slate-600">
                  الميزات المدعومة: {tpl.compatibility.supported_features.join('، ')}
                </p>
              )}
              {!!tpl.compatibility.issues?.length && (
                <div className="space-y-1">
                  {tpl.compatibility.issues.map((issue, idx) => (
                    <p key={idx} className="text-[11px] text-slate-500">{issue}</p>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Default template — variable mapping panel */}
          {isDefault && defaultMeta && (
            <div className="bg-brand-50 border border-brand-200 rounded-xl p-4">
              <div className="flex items-center gap-2 mb-3">
                <Zap className="w-3.5 h-3.5 text-brand-600 shrink-0" />
                <p className="text-xs font-semibold text-brand-800">
                  ربط المتغيرات — {defaultMeta.purposeLabel}
                </p>
              </div>
              <p className="text-[11px] text-brand-700 mb-3">
                تُملأ هذه المتغيرات تلقائياً من بيانات العميل والطلب قبل الإرسال.
              </p>
              <div className="space-y-1.5">
                {Object.entries(defaultMeta.varLabels).map(([varKey, label]) => (
                  <div key={varKey} className="flex items-center gap-2 text-xs">
                    <span className="font-mono bg-white border border-brand-200 text-brand-700 px-1.5 py-0.5 rounded text-[11px] w-12 text-center shrink-0">{varKey}</span>
                    <span className="text-slate-400">←</span>
                    <span className="text-slate-700 font-medium">{label}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Variable inputs for preview */}
          {varKeys.length > 0 && (
            <div className="space-y-2">
              <p className="text-xs font-medium text-slate-600">قيم المتغيرات (للمعاينة)</p>
              {varKeys.map(v => (
                <div key={v} className="flex items-center gap-2">
                  <span className="text-[11px] font-mono bg-amber-50 text-amber-700 px-1.5 py-0.5 rounded w-14 text-center shrink-0">{v}</span>
                  <input
                    className="input text-xs py-1.5 flex-1"
                    placeholder={getVarPlaceholder(v)}
                    value={vars[v] ?? ''}
                    onChange={e => setVars(p => ({ ...p, [v]: e.target.value }))}
                  />
                </div>
              ))}
            </div>
          )}

          <WaPreview
            header={renderBody(getHeader(tpl), vars)}
            body={renderBody(bodyRaw, vars)}
            footer={footer}
            buttons={buttons}
          />
        </div>
      </div>
    </div>
  )
}

// ── Create template wizard ────────────────────────────────────────────────────

const STEP_LABELS_CREATE = ['معلومات القالب', 'محتوى الرسالة', 'الأزرار', 'معاينة وإرسال']

interface WizardState {
  step: number
  name: string
  language: string
  category: TemplateCategory
  headerText: string
  bodyText: string
  footerText: string
  buttons: TemplateButton[]
}

const INIT_WIZARD: WizardState = {
  step: 1,
  name: '',
  language: 'ar',
  category: 'MARKETING',
  headerText: '',
  bodyText: '',
  footerText: '🐝 نحلة — مساعد متجرك',
  buttons: [],
}

function CreateWizard({ onClose, onCreated }: { onClose: () => void; onCreated: (t: WhatsAppTemplateRecord) => void }) {
  const [wiz, setWiz] = useState<WizardState>(INIT_WIZARD)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const canNext = (): boolean => {
    if (wiz.step === 1) return !!wiz.name.trim() && !!wiz.category
    if (wiz.step === 2) return !!wiz.bodyText.trim()
    return true
  }

  const next = () => setWiz(w => ({ ...w, step: Math.min(w.step + 1, 4) }))
  const prev = () => setWiz(w => ({ ...w, step: Math.max(w.step - 1, 1) }))

  const insertVar = (varNum: number) => {
    setWiz(w => ({ ...w, bodyText: w.bodyText + `{{${varNum}}}` }))
  }

  const addButton = (type: TemplateButton['type']) => {
    setWiz(w => ({
      ...w,
      buttons: [...w.buttons, { type, text: '', url: type === 'URL' ? '' : undefined, phone_number: type === 'PHONE_NUMBER' ? '' : undefined }],
    }))
  }

  const removeButton = (i: number) => {
    setWiz(w => ({ ...w, buttons: w.buttons.filter((_, idx) => idx !== i) }))
  }

  const updateButton = (i: number, patch: Partial<TemplateButton>) => {
    setWiz(w => ({
      ...w,
      buttons: w.buttons.map((b, idx) => idx === i ? { ...b, ...patch } : b),
    }))
  }

  const buildPayload = (): CreateTemplatePayload => {
    const components: TemplateComponent[] = []
    if (wiz.headerText.trim()) {
      components.push({ type: 'HEADER', format: 'TEXT', text: wiz.headerText.trim() })
    }
    components.push({ type: 'BODY', text: wiz.bodyText.trim() })
    if (wiz.footerText.trim()) {
      components.push({ type: 'FOOTER', text: wiz.footerText.trim() })
    }
    if (wiz.buttons.length > 0) {
      components.push({ type: 'BUTTONS', buttons: wiz.buttons })
    }
    return {
      name: wiz.name.toLowerCase().replace(/\s+/g, '_'),
      language: wiz.language,
      category: wiz.category,
      components,
      auto_submit: false,
    }
  }

  const handleSubmit = async () => {
    setSaving(true)
    setError('')
    try {
      const created = await templatesApi.create(buildPayload())
      onCreated(created)
      onClose()
    } catch {
      setError('حدث خطأ أثناء إنشاء القالب. تأكد من البيانات وحاول مجدداً.')
    } finally {
      setSaving(false)
    }
  }

  const previewComponents = buildPayload().components
  const previewHeader  = previewComponents.find(c => c.type === 'HEADER')?.text ?? ''
  const previewBody    = previewComponents.find(c => c.type === 'BODY')?.text ?? ''
  const previewFooter  = previewComponents.find(c => c.type === 'FOOTER')?.text ?? ''
  const previewButtons = previewComponents.find(c => c.type === 'BUTTONS')?.buttons ?? []

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div>
            <h2 className="text-sm font-bold text-slate-900">إنشاء قالب واتساب</h2>
            <p className="text-xs text-slate-400 mt-0.5">{STEP_LABELS_CREATE[wiz.step - 1]} — الخطوة {wiz.step} من 4</p>
          </div>
          <button onClick={onClose}><X className="w-5 h-5 text-slate-400 hover:text-slate-600" /></button>
        </div>

        {/* Progress */}
        <div className="px-6 pt-4">
          <div className="flex gap-1">
            {Array.from({ length: 4 }, (_, i) => (
              <div key={i} className={`h-1 flex-1 rounded-full transition-colors ${
                i + 1 < wiz.step ? 'bg-brand-500' : i + 1 === wiz.step ? 'bg-brand-300' : 'bg-slate-100'
              }`} />
            ))}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto px-6 py-5">

          {/* Step 1 — Template Info */}
          {wiz.step === 1 && (
            <div className="space-y-4">
              <p className="text-xs text-slate-500">أدخل معلومات القالب الأساسية. الاسم يجب أن يكون بالإنجليزية مع شرطات سفلية.</p>
              <div>
                <label className="label">اسم القالب</label>
                <input
                  className="input text-sm"
                  placeholder="مثال: cart_reminder أو special_offer"
                  dir="ltr"
                  value={wiz.name}
                  onChange={e => setWiz(w => ({ ...w, name: e.target.value.toLowerCase().replace(/\s+/g, '_') }))}
                />
                <p className="text-xs text-slate-400 mt-1">أحرف صغيرة وشرطات سفلية فقط — هذا هو اسم القالب في Meta</p>
              </div>
              <div className="grid sm:grid-cols-2 gap-4">
                <div>
                  <label className="label">اللغة</label>
                  <select className="input text-sm" value={wiz.language} onChange={e => setWiz(w => ({ ...w, language: e.target.value }))}>
                    <option value="ar">العربية</option>
                    <option value="en">English</option>
                    <option value="en_US">English (US)</option>
                  </select>
                </div>
                <div>
                  <label className="label">الفئة</label>
                  <select className="input text-sm" value={wiz.category} onChange={e => setWiz(w => ({ ...w, category: e.target.value as TemplateCategory }))}>
                    <option value="MARKETING">تسويق (Marketing)</option>
                    <option value="UTILITY">خدمة (Utility)</option>
                    <option value="AUTHENTICATION">مصادقة (Authentication)</option>
                  </select>
                </div>
              </div>
              <div className="bg-blue-50 border border-blue-200 rounded-xl p-3 text-xs text-blue-800 flex gap-2">
                <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-blue-500" />
                <span>قوالب <strong>التسويق</strong> تستخدم للعروض والحملات. قوالب <strong>الخدمة</strong> للإشعارات والمعاملات. قوالب <strong>المصادقة</strong> لكودات OTP.</span>
              </div>
            </div>
          )}

          {/* Step 2 — Message Content */}
          {wiz.step === 2 && (
            <div className="space-y-4">
              <p className="text-xs text-slate-500">أنشئ محتوى الرسالة. استخدم {`{{1}}`} {`{{2}}`} {`{{3}}`} للمتغيرات الديناميكية.</p>

              <div>
                <label className="label">نص الرأس (اختياري)</label>
                <input
                  className="input text-sm"
                  placeholder="مثال: عرض خاص لك 🎁"
                  value={wiz.headerText}
                  onChange={e => setWiz(w => ({ ...w, headerText: e.target.value }))}
                />
              </div>

              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="label mb-0">نص الرسالة *</label>
                  <div className="flex gap-1">
                    {[1, 2, 3].map(n => (
                      <button
                        key={n}
                        onClick={() => insertVar(n)}
                        className="text-[11px] bg-amber-50 text-amber-700 border border-amber-200 px-1.5 py-0.5 rounded hover:bg-amber-100 transition-colors font-mono"
                      >
                        {`{{${n}}}`}
                      </button>
                    ))}
                  </div>
                </div>
                <textarea
                  rows={5}
                  className="input text-sm"
                  placeholder={`مثال:\nمرحباً {{1}}،\nلديك عرض خاص — احصل على خصم {{2}} باستخدام كود: {{3}}`}
                  value={wiz.bodyText}
                  onChange={e => setWiz(w => ({ ...w, bodyText: e.target.value }))}
                />
                <p className="text-xs text-slate-400 mt-1">{wiz.bodyText.length}/1024 حرف</p>
              </div>

              <div>
                <label className="label">نص التذييل (اختياري)</label>
                <input
                  className="input text-sm"
                  value={wiz.footerText}
                  onChange={e => setWiz(w => ({ ...w, footerText: e.target.value }))}
                />
              </div>
            </div>
          )}

          {/* Step 3 — Buttons */}
          {wiz.step === 3 && (
            <div className="space-y-4">
              <p className="text-xs text-slate-500">أضف أزراراً تفاعلية اختيارية (حتى 3 أزرار).</p>

              {wiz.buttons.length < 3 && (
                <div className="flex flex-wrap gap-2">
                  <button onClick={() => addButton('URL')} className="btn-secondary text-xs py-1.5 flex items-center gap-1.5">
                    <Link2 className="w-3.5 h-3.5" /> رابط URL
                  </button>
                  <button onClick={() => addButton('PHONE_NUMBER')} className="btn-secondary text-xs py-1.5 flex items-center gap-1.5">
                    <Phone className="w-3.5 h-3.5" /> رقم هاتف
                  </button>
                  <button onClick={() => addButton('COPY_CODE')} className="btn-secondary text-xs py-1.5 flex items-center gap-1.5">
                    <CopyIcon className="w-3.5 h-3.5" /> نسخ كود
                  </button>
                </div>
              )}

              {wiz.buttons.length === 0 && (
                <div className="py-8 text-center text-sm text-slate-400">
                  <Type className="w-8 h-8 mx-auto mb-2 text-slate-200" />
                  لا توجد أزرار — الرسالة ستُرسل بدون أزرار تفاعلية.
                </div>
              )}

              <div className="space-y-3">
                {wiz.buttons.map((btn, i) => (
                  <div key={i} className="border border-slate-200 rounded-xl p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-medium text-slate-600">
                        {btn.type === 'URL' ? 'رابط URL' : btn.type === 'PHONE_NUMBER' ? 'رقم هاتف' : 'نسخ كود'}
                      </span>
                      <button onClick={() => removeButton(i)} className="text-slate-300 hover:text-red-500">
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </div>
                    <input
                      className="input text-sm"
                      placeholder="نص الزر"
                      value={btn.text}
                      onChange={e => updateButton(i, { text: e.target.value })}
                    />
                    {btn.type === 'URL' && (
                      <input
                        className="input text-sm"
                        placeholder="https://example.com أو {{1}}"
                        dir="ltr"
                        value={btn.url ?? ''}
                        onChange={e => updateButton(i, { url: e.target.value })}
                      />
                    )}
                    {btn.type === 'PHONE_NUMBER' && (
                      <input
                        className="input text-sm"
                        placeholder="+966 50 000 0000"
                        dir="ltr"
                        value={btn.phone_number ?? ''}
                        onChange={e => updateButton(i, { phone_number: e.target.value })}
                      />
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Step 4 — Preview + Submit */}
          {wiz.step === 4 && (
            <div className="space-y-5">
              <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl p-3">
                <AlertCircle className="w-4 h-4 text-amber-500 shrink-0 mt-0.5" />
                <p className="text-xs text-amber-800">
                  بعد الإرسال، سيدخل القالب حالة <strong>قيد المراجعة</strong> حتى تعتمده Meta (24–48 ساعة).
                  لن يمكن استخدامه في الحملات قبل الاعتماد.
                </p>
              </div>

              <div className="grid sm:grid-cols-2 gap-4">
                <div className="space-y-2">
                  {[
                    ['الاسم',    wiz.name || '—'],
                    ['اللغة',    LANGUAGE_LABELS[wiz.language] ?? wiz.language],
                    ['الفئة',    CATEGORY_LABELS[wiz.category]],
                    ['المتغيرات', `${extractVars(wiz.bodyText).length} متغير`],
                    ['الأزرار',  wiz.buttons.length > 0 ? `${wiz.buttons.length} زر` : 'بدون أزرار'],
                  ].map(([k, v]) => (
                    <div key={k} className="flex gap-2 bg-slate-50 rounded-lg px-3 py-2 text-xs">
                      <span className="text-slate-400 w-20 shrink-0">{k}</span>
                      <span className="font-medium text-slate-800 truncate">{v}</span>
                    </div>
                  ))}
                </div>
                <div>
                  <p className="text-xs text-slate-500 mb-2">معاينة الرسالة</p>
                  <WaPreview
                    header={previewHeader}
                    body={previewBody}
                    footer={previewFooter}
                    buttons={previewButtons as TemplateButton[]}
                  />
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Footer nav */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-slate-100">
          <button onClick={prev} disabled={wiz.step === 1} className="btn-ghost text-sm disabled:opacity-30">
            <ChevronRight className="w-4 h-4" /> السابق
          </button>

          {error && <p className="text-xs text-red-500 mx-4">{error}</p>}

          {wiz.step < 4 ? (
            <button onClick={next} disabled={!canNext()} className="btn-primary text-sm disabled:opacity-40">
              التالي <ChevronLeft className="w-4 h-4" />
            </button>
          ) : (
            <button onClick={handleSubmit} disabled={saving} className="btn-primary text-sm">
              {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
              {saving ? 'جارٍ الحفظ…' : 'حفظ كمسودة'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Filter tabs ───────────────────────────────────────────────────────────────

// ── Edit Template Modal ───────────────────────────────────────────────────────

function EditModal({
  tpl,
  onClose,
  onSaved,
}: {
  tpl: WhatsAppTemplateRecord
  onClose: () => void
  onSaved: (updated: WhatsAppTemplateRecord) => void
}) {
  const bodyComp   = tpl.components.find(c => c.type === 'BODY')
  const headerComp = tpl.components.find(c => c.type === 'HEADER')
  const footerComp = tpl.components.find(c => c.type === 'FOOTER')
  const btnsComp   = tpl.components.find(c => c.type === 'BUTTONS')

  const [headerText, setHeaderText] = useState(headerComp?.text ?? '')
  const [bodyText,   setBodyText]   = useState(bodyComp?.text ?? '')
  const [footerText, setFooterText] = useState(footerComp?.text ?? '')
  const [buttons, setButtons]       = useState<TemplateButton[]>(btnsComp?.buttons ?? [])
  const [saving, setSaving]         = useState(false)
  const [error, setError]           = useState('')

  const updateBtn = (i: number, patch: Partial<TemplateButton>) =>
    setButtons(bs => bs.map((b, idx) => idx === i ? { ...b, ...patch } : b))

  const buildComponents = (): TemplateComponent[] => {
    const out: TemplateComponent[] = []
    if (headerText.trim()) out.push({ type: 'HEADER', format: 'TEXT', text: headerText.trim() })
    out.push({ type: 'BODY', text: bodyText.trim() })
    if (footerText.trim()) out.push({ type: 'FOOTER', text: footerText.trim() })
    if (buttons.length > 0) out.push({ type: 'BUTTONS', buttons })
    return out
  }

  const handleSave = async () => {
    if (!bodyText.trim()) { setError('نص الرسالة مطلوب'); return }
    setSaving(true); setError('')
    try {
      const updated = await templatesApi.update(tpl.id, { components: buildComponents() })
      onSaved(updated)
      onClose()
    } catch (e: any) {
      setError(e?.message ?? 'حدث خطأ — تأكد من البيانات وحاول مجدداً')
    } finally {
      setSaving(false)
    }
  }

  const insertVar = (n: number) => setBodyText(t => t + `{{${n}}}`)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm p-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-xl flex flex-col max-h-[90vh]">

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div className="flex items-center gap-2">
            <Pencil className="w-4 h-4 text-brand-500" />
            <h2 className="text-sm font-bold text-slate-900">تعديل القالب</h2>
            <span className="text-[11px] text-slate-400 font-mono">{tpl.name}</span>
          </div>
          <button onClick={onClose}><X className="w-5 h-5 text-slate-400 hover:text-slate-600" /></button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
          {/* Info notice */}
          <div className="flex items-start gap-2 bg-amber-50 border border-amber-200 rounded-xl px-3 py-2.5 text-xs text-amber-800">
            <AlertCircle className="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-500" />
            أي تعديل يُعيد القالب لحالة <strong>مسودة</strong> — ستحتاج لإرساله لـ Meta من جديد للموافقة.
          </div>

          {/* Header text */}
          <div>
            <label className="label text-xs">نص الرأس (اختياري)</label>
            <input className="input text-sm" value={headerText}
              onChange={e => setHeaderText(e.target.value)}
              placeholder="عنوان الرسالة..." />
          </div>

          {/* Body */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="label mb-0 text-xs">نص الرسالة *</label>
              <div className="flex gap-1">
                {[1, 2, 3].map(n => (
                  <button key={n} onClick={() => insertVar(n)}
                    className="text-[11px] bg-amber-50 text-amber-700 border border-amber-200 px-1.5 py-0.5 rounded hover:bg-amber-100 font-mono">
                    {`{{${n}}}`}
                  </button>
                ))}
              </div>
            </div>
            <textarea rows={6} className="input text-sm" value={bodyText}
              onChange={e => setBodyText(e.target.value)}
              placeholder="نص الرسالة..." />
            <p className="text-xs text-slate-400 mt-1">{bodyText.length}/1024 حرف</p>
          </div>

          {/* Footer */}
          <div>
            <label className="label text-xs">نص التذييل (اختياري)</label>
            <input className="input text-sm" value={footerText}
              onChange={e => setFooterText(e.target.value)}
              placeholder="مثال: نحلة — مساعد متجرك 🐝" />
          </div>

          {/* Buttons */}
          {buttons.length > 0 && (
            <div className="space-y-2">
              <label className="label text-xs">الأزرار</label>
              {buttons.map((btn, i) => (
                <div key={i} className="border border-slate-200 rounded-xl p-3 space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-slate-600">
                      {btn.type === 'URL' ? '🔗 رابط' : btn.type === 'COPY_CODE' ? '📋 نسخ كود' : btn.type === 'PHONE_NUMBER' ? '📞 هاتف' : '💬 رد سريع'}
                    </span>
                    <button onClick={() => setButtons(bs => bs.filter((_, idx) => idx !== i))}
                      className="text-slate-300 hover:text-red-500"><X className="w-3.5 h-3.5" /></button>
                  </div>
                  <input className="input text-sm" placeholder="نص الزر"
                    value={btn.text} onChange={e => updateBtn(i, { text: e.target.value })} />
                  {btn.type === 'URL' && (
                    <>
                      <input className="input text-sm" placeholder="https://example.com/page/{{1}}" dir="ltr"
                        value={btn.url ?? ''} onChange={e => updateBtn(i, { url: e.target.value })} />
                      {btn.url && /^\{\{\d+\}\}$/.test(btn.url.trim()) && (
                        <p className="text-[11px] text-amber-600 flex items-center gap-1">
                          <AlertCircle className="w-3 h-3" />
                          الرابط يجب أن يبدأ بـ https:// — مثال: https://yourstore.com/cart/{'{{1}}'}
                        </p>
                      )}
                    </>
                  )}
                  {btn.type === 'COPY_CODE' && (
                    <input className="input text-sm font-mono" placeholder="PROMO2025" dir="ltr"
                      value={btn.example?.[0] ?? ''} onChange={e => updateBtn(i, { example: [e.target.value] })} />
                  )}
                  {btn.type === 'PHONE_NUMBER' && (
                    <input className="input text-sm" placeholder="+966..." dir="ltr"
                      value={btn.phone_number ?? ''} onChange={e => updateBtn(i, { phone_number: e.target.value })} />
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Live preview */}
          <div>
            <p className="text-xs text-slate-500 mb-2">معاينة</p>
            <WaPreview
              header={headerText}
              body={bodyText}
              footer={footerText}
              buttons={buttons}
            />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-4 border-t border-slate-100">
          <button onClick={onClose} className="btn-ghost text-sm">إلغاء</button>
          {error && <p className="text-xs text-red-500 flex-1 mx-4 text-center">{error}</p>}
          <button onClick={handleSave} disabled={saving || !bodyText.trim()}
            className="btn-primary text-sm disabled:opacity-40">
            {saving ? <RefreshCw className="w-4 h-4 animate-spin" /> : <CheckCheck className="w-4 h-4" />}
            حفظ التعديلات
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Nahla Library Modal ───────────────────────────────────────────────────────

const BUTTON_TYPE_ICON: Record<string, string> = {
  URL:         '🔗',
  COPY_CODE:   '📋',
  QUICK_REPLY: '💬',
  PHONE_NUMBER:'📞',
}

const TAG_LABELS: Record<string, string> = {
  all:       'الكل',
  marketing: 'التسويق',
  orders:    'الطلبات',
  shipping:  'الشحن',
  recovery:  'الاسترجاع',
  discounts: 'الخصومات',
  welcome:   'الترحيب',
}

function NahlaLibraryModal({ onClose, onImported }: {
  onClose: () => void
  onImported: (tpl: WhatsAppTemplateRecord) => void
}) {
  const [templates, setTemplates]   = useState<NahlaLibraryTemplate[]>([])
  const [loading, setLoading]       = useState(true)
  const [activeTag, setActiveTag]   = useState('all')
  const [search, setSearch]         = useState('')
  const [importing, setImporting]   = useState<string | null>(null)
  const [imported, setImported]     = useState<Set<string>>(new Set())
  const [preview, setPreview]       = useState<NahlaLibraryTemplate | null>(null)

  const load = useCallback(async (tag: string, q: string) => {
    setLoading(true)
    try {
      const res = await templatesApi.nahlaLibrary({ tag: tag !== 'all' ? tag : undefined, search: q || undefined })
      setTemplates(res.templates)
    } catch {
      setTemplates([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(activeTag, search) }, [activeTag, search, load])

  const handleImport = async (key: string) => {
    setImporting(key)
    try {
      const res = await templatesApi.importNahlaTemplate(key)
      setImported(prev => new Set(prev).add(key))
      onImported(res.template)
    } catch {
      /* ignore */
    } finally {
      setImporting(null)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-amber-50 border border-amber-200 flex items-center justify-center">
              <BookOpen className="w-5 h-5 text-amber-600" />
            </div>
            <div>
              <h2 className="font-bold text-slate-900 text-base">📚 مكتبة قوالب نحلة</h2>
              <p className="text-xs text-slate-500">قوالب عربية جاهزة — استوردها وعدّلها وأرسلها لـ Meta</p>
            </div>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-slate-100 rounded-lg transition-colors">
            <X className="w-4 h-4 text-slate-500" />
          </button>
        </div>

        {/* Search + Filter */}
        <div className="px-6 py-3 border-b border-slate-100 space-y-3">
          <div className="relative">
            <Search className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
            <input
              type="text"
              placeholder="ابحث عن قالب..."
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full pr-9 pl-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-amber-400"
              dir="rtl"
            />
          </div>
          <div className="flex gap-2 flex-wrap">
            {Object.entries(TAG_LABELS).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setActiveTag(key)}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                  activeTag === key
                    ? 'bg-amber-500 text-white'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {/* Body */}
        <div className="flex flex-1 overflow-hidden">
          {/* Template grid */}
          <div className="flex-1 overflow-y-auto p-4">
            {loading ? (
              <div className="flex items-center justify-center h-40">
                <RefreshCw className="w-6 h-6 text-amber-500 animate-spin" />
              </div>
            ) : templates.length === 0 ? (
              <p className="text-center text-slate-400 text-sm py-12">لا توجد قوالب مطابقة</p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {templates.map(tpl => {
                  const isImported = imported.has(tpl.key)
                  const isImporting = importing === tpl.key
                  return (
                    <div
                      key={tpl.key}
                      onClick={() => setPreview(tpl)}
                      className={`group relative border rounded-xl p-4 cursor-pointer transition-all hover:shadow-md hover:border-amber-300 ${
                        preview?.key === tpl.key ? 'border-amber-400 bg-amber-50' : 'border-slate-200 bg-white'
                      }`}
                    >
                      {/* Category badge */}
                      <div className="flex items-start justify-between gap-2 mb-2">
                        <span className="font-semibold text-slate-900 text-sm leading-tight">{tpl.name_ar}</span>
                        <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium shrink-0 ${
                          tpl.category === 'MARKETING'
                            ? 'bg-purple-100 text-purple-700'
                            : 'bg-blue-100 text-blue-700'
                        }`}>
                          {tpl.category === 'MARKETING' ? 'تسويق' : 'خدمة'}
                        </span>
                      </div>

                      {/* Description */}
                      {tpl.description_ar && (
                        <p className="text-xs text-slate-500 mb-2 leading-relaxed line-clamp-2">{tpl.description_ar}</p>
                      )}

                      {/* Smart trigger */}
                      {tpl.smart_label && (
                        <div className="flex items-center gap-1 mb-3">
                          <Bot className="w-3 h-3 text-emerald-600 shrink-0" />
                          <span className="text-[10px] text-emerald-700 font-medium">{tpl.smart_label}</span>
                        </div>
                      )}

                      {/* Buttons preview */}
                      {tpl.buttons.length > 0 && (
                        <div className="flex flex-wrap gap-1 mb-3">
                          {tpl.buttons.map((btn, i) => (
                            <span key={i} className="inline-flex items-center gap-1 text-[10px] bg-slate-100 text-slate-600 px-2 py-0.5 rounded-full">
                              <span>{BUTTON_TYPE_ICON[btn.type] || '▶'}</span>
                              <span>{btn.text || btn.type}</span>
                            </span>
                          ))}
                        </div>
                      )}

                      {/* Import button */}
                      <button
                        onClick={e => { e.stopPropagation(); if (!isImported) handleImport(tpl.key) }}
                        disabled={isImporting || isImported}
                        className={`w-full py-1.5 rounded-lg text-xs font-semibold transition-colors ${
                          isImported
                            ? 'bg-emerald-100 text-emerald-700 cursor-default'
                            : 'bg-amber-500 hover:bg-amber-600 text-white'
                        }`}
                      >
                        {isImporting ? (
                          <span className="flex items-center justify-center gap-1">
                            <RefreshCw className="w-3 h-3 animate-spin" /> جاري الاستيراد...
                          </span>
                        ) : isImported ? (
                          <span className="flex items-center justify-center gap-1">
                            <CheckCheck className="w-3 h-3" /> تم الاستيراد — يمكنك التعديل
                          </span>
                        ) : (
                          <span className="flex items-center justify-center gap-1">
                            <Download className="w-3 h-3" /> استيراد وتخصيص
                          </span>
                        )}
                      </button>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          {/* Preview panel */}
          {preview && (
            <div className="w-72 border-r border-slate-100 bg-slate-50 p-4 overflow-y-auto hidden lg:block">
              <p className="text-xs font-semibold text-slate-700 mb-3">معاينة الرسالة</p>
              {/* WhatsApp bubble */}
              <div className="bg-[#e5ddd5] rounded-xl p-3 mb-4">
                <div className="bg-white rounded-2xl rounded-bl-sm shadow-sm p-3 space-y-2" dir="rtl">
                  <p className="text-slate-800 text-xs leading-relaxed whitespace-pre-line">
                    {preview.preview_body}
                  </p>
                  {preview.preview_footer && (
                    <p className="text-[10px] text-slate-400">{preview.preview_footer}</p>
                  )}
                  {preview.buttons.length > 0 && (
                    <div className="border-t border-slate-100 pt-2 space-y-1">
                      {preview.buttons.map((btn, i) => (
                        <div key={i} className="text-center text-xs text-blue-600 font-medium py-0.5 flex items-center justify-center gap-1">
                          <span>{BUTTON_TYPE_ICON[btn.type]}</span>
                          <span>{btn.type === 'COPY_CODE' ? 'انسخ الكود' : btn.text}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  <p className="text-[10px] text-slate-300 text-end">✓✓ الآن</p>
                </div>
              </div>

              {/* Slots */}
              {preview.slots.length > 0 && (
                <div className="mb-4">
                  <p className="text-[10px] font-semibold text-slate-500 uppercase mb-2">المتغيرات ({preview.slot_count})</p>
                  <div className="space-y-1">
                    {preview.slots.map((slot, i) => (
                      <div key={slot} className="flex items-center gap-2 text-xs">
                        <span className="w-6 h-6 rounded-full bg-amber-100 text-amber-700 font-bold flex items-center justify-center text-[10px]">{i+1}</span>
                        <span className="text-slate-600 font-mono">{slot}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <button
                onClick={() => handleImport(preview.key)}
                disabled={importing === preview.key || imported.has(preview.key)}
                className={`w-full py-2 rounded-xl text-xs font-bold transition-colors ${
                  imported.has(preview.key)
                    ? 'bg-emerald-100 text-emerald-700'
                    : 'bg-amber-500 hover:bg-amber-600 text-white'
                }`}
              >
                {imported.has(preview.key) ? '✓ تم الاستيراد' : 'استيراد وتخصيص ←'}
              </button>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-slate-100 bg-slate-50 rounded-b-2xl">
          <p className="text-[11px] text-slate-400 text-center">
            بعد الاستيراد يصبح القالب مسودة قابلة للتعديل — عدّله ثم أرسله لـ Meta للموافقة ← استخدمه في حملاتك
          </p>
        </div>
      </div>
    </div>
  )
}

const FILTER_TABS: { key: TemplateStatus | 'all'; label: string }[] = [
  { key: 'all',      label: 'الكل' },
  { key: 'DRAFT',    label: 'مسودات' },
  { key: 'APPROVED', label: 'معتمدة' },
  { key: 'PENDING',  label: 'قيد المراجعة' },
  { key: 'REJECTED', label: 'مرفوضة' },
  { key: 'DISABLED', label: 'معطّلة' },
  { key: 'PAUSED',   label: 'موقوفة مؤقتًا' },
]

const TABLE_HEADERS = ['اسم القالب', 'اللغة', 'الفئة', 'الحالة', 'المتغيرات', 'آخر تحديث', '']

// ── Main page ─────────────────────────────────────────────────────────────────

export default function Templates() {
  const [templates, setTemplates] = useState<WhatsAppTemplateRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [filterTab, setFilterTab] = useState<TemplateStatus | 'all'>('all')
  const [showCreate, setShowCreate] = useState(false)
  const [showNahlaLibrary, setShowNahlaLibrary] = useState(false)
  const [preview, setPreview] = useState<WhatsAppTemplateRecord | null>(null)
  const [editTemplate, setEditTemplate] = useState<WhatsAppTemplateRecord | null>(null)
  const [submitError, setSubmitError] = useState<{id: number; msg: string} | null>(null)
  const [submitting, setSubmitting] = useState<number | null>(null)
  const { t } = useLanguage()

  const loadTemplates = useCallback(() => {
    setLoading(true)
    templatesApi.list()
      .then(r => setTemplates(r.templates))
      .catch(() => setTemplates([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { loadTemplates() }, [loadTemplates])

  const handleSync = async () => {
    setSyncing(true)
    try {
      await templatesApi.sync()
      loadTemplates()
    } finally {
      setSyncing(false)
    }
  }

  const handleDelete = async (id: number) => {
    try {
      await templatesApi.delete(id)
      setTemplates(ts => ts.filter(t => t.id !== id))
    } catch { /* ignore */ }
  }

  const handleSubmitTemplate = async (id: number) => {
    if (submitting !== null) return
    setSubmitError(null)
    setSubmitting(id)
    try {
      const res = await templatesApi.submit(id)
      setTemplates(ts => ts.map(t => (t.id === id ? res.template : t)))
    } catch (e: any) {
      const msg = e?.detail ?? e?.message ?? 'فشل إرسال القالب — تحقق من ربط واتساب'
      setSubmitError({ id, msg })
      setTimeout(() => setSubmitError(s => s?.id === id ? null : s), 10000)
    } finally {
      setSubmitting(null)
    }
  }

  const filtered = filterTab === 'all'
    ? templates
    : templates.filter(t => t.status === filterTab)

  const counts = {
    draft: templates.filter(t => t.status === 'DRAFT').length,
    approved: templates.filter(t => t.status === 'APPROVED').length,
    pending:  templates.filter(t => t.status === 'PENDING').length,
    rejected: templates.filter(t => t.status === 'REJECTED').length,
  }

  return (
    <div className="space-y-5">
      {showCreate && (
        <CreateWizard
          onClose={() => setShowCreate(false)}
          onCreated={tpl => setTemplates(ts => [tpl, ...ts])}
        />
      )}
      {preview && (
        <PreviewModal tpl={preview} onClose={() => setPreview(null)} />
      )}
      {editTemplate && (
        <EditModal
          tpl={editTemplate}
          onClose={() => setEditTemplate(null)}
          onSaved={updated => setTemplates(ts => ts.map(t => t.id === updated.id ? updated : t))}
        />
      )}
      {showNahlaLibrary && (
        <NahlaLibraryModal
          onClose={() => setShowNahlaLibrary(false)}
          onImported={tpl => {
            setTemplates(ts => [tpl, ...ts])
          }}
        />
      )}

      {/* Submit error toast */}
      {submitError && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-red-600 text-white px-5 py-3 rounded-xl shadow-xl text-sm">
          <XCircle className="w-4 h-4 shrink-0" />
          <span>{submitError.msg}</span>
          <button onClick={() => setSubmitError(null)} className="ml-2 opacity-70 hover:opacity-100">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      <PageHeader
        title={t(tr => tr.pages.templates.title)}
        subtitle={t(tr => tr.pages.templates.subtitle)}
        action={
          <div className="flex items-center gap-2">
            <button
              onClick={() => setShowNahlaLibrary(true)}
              className="btn-secondary text-sm border-amber-300 text-amber-700 hover:bg-amber-50"
            >
              <BookOpen className="w-4 h-4" />
              مكتبة نحلة
            </button>
            <button
              onClick={handleSync}
              disabled={syncing}
              className="btn-secondary text-sm"
            >
              <RefreshCw className={`w-4 h-4 ${syncing ? 'animate-spin' : ''}`} />
              {t(tr => tr.actions.syncTemplates)}
            </button>
            <button onClick={() => setShowCreate(true)} className="btn-primary text-sm">
              <Plus className="w-4 h-4" /> {t(tr => tr.actions.newTemplate)}
            </button>
          </div>
        }
      />

      {/* Stats */}
      <div className="grid grid-cols-3 gap-4">
        <StatCard label="مسودات"         value={String(counts.draft)}    change={0}  icon={Type}         iconColor="text-slate-600"    iconBg="bg-slate-100" />
        <StatCard label="معتمدة"         value={String(counts.approved)} change={0}  icon={CheckCircle}  iconColor="text-emerald-600" iconBg="bg-emerald-50" />
        <StatCard label="قيد المراجعة"  value={String(counts.pending)}  change={0}  icon={Clock}        iconColor="text-amber-600"   iconBg="bg-amber-50" />
      </div>

      {/* Compliance notice */}
      <div className="flex items-start gap-3 bg-blue-50 border border-blue-200 rounded-xl px-4 py-3">
        <MessageSquare className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" />
        <p className="text-xs text-blue-800">
          <span className="font-semibold">سياسة Meta: </span>
          جميع القوالب تخضع لمراجعة Meta قبل الاستخدام. لا يمكن استخدام أي قالب في حملة قبل الحصول على حالة
          <strong> APPROVED</strong>. مدة المراجعة عادةً 24–48 ساعة.
        </p>
      </div>

      {/* Table */}
      <div className="card">
        {/* Filter tabs */}
        <div className="flex items-center gap-1 px-5 py-4 border-b border-slate-100">
          {FILTER_TABS.map(tab => (
            <button
              key={tab.key}
              onClick={() => setFilterTab(tab.key)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                filterTab === tab.key
                  ? 'bg-brand-500 text-white'
                  : 'text-slate-500 hover:bg-slate-100'
              }`}
            >
              {tab.label}
              {tab.key !== 'all' && (
                <span className={`ms-1.5 text-[10px] px-1 rounded-full ${
                  filterTab === tab.key ? 'bg-white/20' : 'bg-slate-100'
                }`}>
                  {templates.filter(t => t.status === tab.key).length}
                </span>
              )}
            </button>
          ))}
          <button onClick={loadTemplates} className="ms-auto text-slate-400 hover:text-slate-600">
            <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>

        {loading ? (
          <div className="py-16 text-center text-sm text-slate-400 flex items-center justify-center gap-2">
            <RefreshCw className="w-4 h-4 animate-spin" /> جارٍ التحميل…
          </div>
        ) : filtered.length === 0 ? (
          <div className="py-16 text-center space-y-3">
            <MessageSquare className="w-10 h-10 text-slate-200 mx-auto" />
            <p className="text-sm text-slate-400">لا توجد قوالب في هذه الفئة.</p>
            {filterTab === 'all' && (
              <button onClick={() => setShowCreate(true)} className="btn-primary text-sm mx-auto">
                <Plus className="w-4 h-4" /> أنشئ قالبك الأول
              </button>
            )}
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
                {filtered.map(tpl => (
                  <TemplateRow
                    key={tpl.id}
                    tpl={tpl}
                    onPreview={() => setPreview(tpl)}
                    onDelete={() => handleDelete(tpl.id)}
                    onSubmit={() => handleSubmitTemplate(tpl.id)}
                    onEdit={() => setEditTemplate(tpl)}
                    isSubmitting={submitting === tpl.id}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
