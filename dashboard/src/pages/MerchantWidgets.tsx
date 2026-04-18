/**
 * MerchantWidgets — Conversion Widgets System
 * أدوات زيادة المبيعات — نظام الويدجتات التحويلية
 */
import { useState, useEffect, useCallback } from 'react'
import {
  ToggleLeft, ToggleRight, Settings2, CheckCircle, AlertCircle,
  Loader2, Copy, X, MessageCircle, Gift, Tag, Zap,
  ExternalLink, Rocket, ChevronRight, TrendingUp, LayoutGrid,
  Clock, MousePointerClick, Bell, Save, Eye, EyeOff,
} from 'lucide-react'
import { widgetsApi, type WidgetItem, type DisplayRules, type SallaInstallResult } from '../api/widgets'
import { apiCall } from '../api/client'

// ── Constants ─────────────────────────────────────────────────────────────────
const API_BASE = (import.meta.env.VITE_API_URL as string) || 'https://api.nahlah.ai'

function getTenantId(): string {
  return localStorage.getItem('nahla_tenant_id') || ''
}

// ── Icon map ──────────────────────────────────────────────────────────────────
const ICON_MAP: Record<string, React.ReactNode> = {
  MessageCircle: <MessageCircle className="w-5 h-5" />,
  Gift:          <Gift          className="w-5 h-5" />,
  Tag:           <Tag           className="w-5 h-5" />,
}

const CATEGORY_COLOR: Record<string, string> = {
  conversion:    'text-purple-600 bg-purple-100',
  communication: 'text-emerald-600 bg-emerald-100',
  general:       'text-slate-600 bg-slate-100',
}

const CATEGORY_LABEL: Record<string, string> = {
  conversion:    'تحويل مبيعات',
  communication: 'تواصل',
  general:       'عام',
}

// ── Quick Install Panel ───────────────────────────────────────────────────────

type InstallState = 'idle' | 'trying' | 'success' | 'manual'

function QuickInstallPanel({ tenantId }: { tenantId: string }) {
  const [state,    setState]    = useState<InstallState>('idle')
  const [result,   setResult]   = useState<SallaInstallResult | null>(null)
  const [tagCopied, setTagCopied] = useState(false)

  const embedUrl  = `${API_BASE}/merchant/widgets/${tenantId}/nahla-widgets.js`
  const scriptTag = `<script src="${embedUrl}" defer></script>`

  const copyTag = () => {
    navigator.clipboard.writeText(scriptTag)
    setTagCopied(true)
    setTimeout(() => setTagCopied(false), 2500)
  }

  const tryAutoInstall = async () => {
    setState('trying')
    try {
      const res = await widgetsApi.sallaInstall()
      setResult(res)
      setState(res.success ? 'success' : 'manual')
      if (!res.success) {
        navigator.clipboard.writeText(res.script_tag || scriptTag).catch(() => {})
        setTagCopied(true)
        setTimeout(() => setTagCopied(false), 3000)
      }
    } catch {
      setState('manual')
    }
  }

  if (state === 'idle') return (
    <div className="p-4 bg-gradient-to-br from-brand-50 to-purple-50 border border-brand-200 rounded-2xl space-y-3">
      <div className="flex items-center gap-2 text-brand-700">
        <Rocket className="w-4 h-4 shrink-0" />
        <p className="text-sm font-bold">تثبيت الويدجتات في متجرك</p>
      </div>
      <p className="text-xs text-slate-500 leading-relaxed">
        نحلة يحاول التثبيت تلقائياً — أو أضف سطر واحد في إعدادات سلة مرة واحدة فقط.
      </p>
      <button onClick={tryAutoInstall}
        className="w-full flex items-center justify-center gap-2 py-2.5 rounded-xl bg-brand-500 hover:bg-brand-600 text-white text-sm font-semibold transition-colors shadow-sm shadow-brand-500/30">
        <Rocket className="w-4 h-4" />
        تثبيت في سلة
      </button>
    </div>
  )

  if (state === 'trying') return (
    <div className="flex items-center justify-center gap-3 p-5 bg-slate-50 border border-slate-200 rounded-2xl text-sm text-slate-600">
      <Loader2 className="w-5 h-5 animate-spin text-brand-500" />
      جاري التثبيت...
    </div>
  )

  if (state === 'success') return (
    <div className="p-4 bg-emerald-50 border border-emerald-300 rounded-2xl space-y-2">
      <div className="flex items-center gap-2 text-emerald-700">
        <CheckCircle className="w-5 h-5 shrink-0" />
        <p className="text-sm font-bold">تم تثبيت الويدجتات تلقائياً ✓</p>
      </div>
      <p className="text-xs text-emerald-600">التفعيل والتعطيل يعمل من نحلة مباشرة.</p>
    </div>
  )

  const adminUrl = result?.salla_admin_url || 'https://s.salla.sa/settings/scripts'
  const tag      = result?.script_tag || scriptTag

  return (
    <div className="rounded-2xl border border-amber-200 bg-amber-50 overflow-hidden">
      <div className="flex items-center gap-2 px-4 py-3 bg-amber-100 border-b border-amber-200">
        <Zap className="w-4 h-4 text-amber-600 shrink-0" />
        <p className="text-sm font-semibold text-amber-800">خطوة واحدة في سلة — مرة واحدة فقط</p>
      </div>
      <div className="p-4 space-y-4">
        {/* Step 1 */}
        <div className="flex gap-3">
          <div className="w-6 h-6 rounded-full bg-brand-500 text-white text-xs font-bold flex items-center justify-center shrink-0 mt-0.5">١</div>
          <div className="flex-1 space-y-2">
            <p className="text-sm font-medium text-slate-700">انسخ كود التثبيت</p>
            <div className="relative">
              <code dir="ltr" className="block w-full bg-white border border-slate-200 rounded-lg px-3 py-2.5 text-xs font-mono text-slate-700 overflow-x-auto whitespace-nowrap pe-20">
                {tag}
              </code>
              <button onClick={copyTag}
                className={`absolute top-1.5 left-1.5 flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs font-medium transition-all ${tagCopied ? 'bg-emerald-500 text-white' : 'bg-slate-700 hover:bg-slate-600 text-white'}`}>
                {tagCopied ? <><CheckCircle className="w-3 h-3" />تم</> : <><Copy className="w-3 h-3" />نسخ</>}
              </button>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2"><div className="flex-1 h-px bg-amber-200" /><ChevronRight className="w-3 h-3 text-amber-400" /><div className="flex-1 h-px bg-amber-200" /></div>
        {/* Step 2 */}
        <div className="flex gap-3">
          <div className="w-6 h-6 rounded-full bg-brand-500 text-white text-xs font-bold flex items-center justify-center shrink-0 mt-0.5">٢</div>
          <div className="flex-1 space-y-2">
            <p className="text-sm font-medium text-slate-700">افتح إعدادات سلة</p>
            <a href={adminUrl} target="_blank" rel="noopener noreferrer"
              className="flex items-center justify-between gap-2 w-full px-4 py-2.5 rounded-xl bg-[#3D5AFE] hover:bg-[#3451E0] text-white text-sm font-semibold transition-colors">
              <span>افتح لوحة سلة ← السكريبت المخصص</span>
              <ExternalLink className="w-4 h-4 shrink-0" />
            </a>
            <p className="text-xs text-slate-500">الإعدادات ← المظهر ← <strong>JavaScript مخصص</strong> ← الصق ← حفظ</p>
          </div>
        </div>
        <div className="flex items-center gap-2"><div className="flex-1 h-px bg-amber-200" /><ChevronRight className="w-3 h-3 text-amber-400" /><div className="flex-1 h-px bg-amber-200" /></div>
        {/* Done */}
        <div className="flex gap-3">
          <div className="w-6 h-6 rounded-full bg-emerald-500 text-white text-xs font-bold flex items-center justify-center shrink-0 mt-0.5">✓</div>
          <div className="flex-1">
            <p className="text-sm font-medium text-slate-700">جاهز! — تحكم كامل من نحلة</p>
            <p className="text-xs text-slate-500 mt-0.5">بعدها التفعيل والتعطيل من نحلة يعمل فوراً بدون لمس سلة.</p>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Display Rules Editor ──────────────────────────────────────────────────────

function RulesEditor({
  rules, onChange,
}: { rules: DisplayRules; onChange: (r: Partial<DisplayRules>) => void }) {
  const pages = ['all', 'home', 'product', 'cart', 'checkout']
  const pageLabels: Record<string, string> = {
    all: 'كل الصفحات', home: 'الرئيسية', product: 'المنتج', cart: 'السلة', checkout: 'الدفع',
  }
  const current = rules.show_on_pages || ['all']

  function togglePage(p: string) {
    let next: string[]
    if (p === 'all') {
      next = ['all']
    } else {
      next = current.filter(x => x !== 'all')
      if (next.includes(p)) next = next.filter(x => x !== p)
      else next = [...next, p]
      if (next.length === 0) next = ['all']
    }
    onChange({ show_on_pages: next })
  }

  return (
    <div className="space-y-4 pt-4 border-t border-slate-100">
      <p className="text-xs font-semibold text-slate-500 uppercase tracking-wider">قواعد الظهور</p>

      {/* Trigger */}
      <div>
        <label className="label">متى يظهر؟</label>
        <div className="grid grid-cols-2 gap-2 mt-1">
          {[
            { v: 'entry',       l: 'عند الدخول',    icon: <Bell className="w-3.5 h-3.5" /> },
            { v: 'scroll',      l: 'عند التمرير',   icon: <MousePointerClick className="w-3.5 h-3.5" /> },
          ].map(({ v, l, icon }) => (
            <button key={v} onClick={() => onChange({ trigger: v as DisplayRules['trigger'] })}
              className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-colors ${
                rules.trigger === v ? 'bg-brand-500 border-brand-500 text-white' : 'border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}>
              {icon}{l}
            </button>
          ))}
        </div>
      </div>

      {/* Delay */}
      <div className="flex items-center gap-4">
        <div className="flex-1">
          <label className="label flex items-center gap-1"><Clock className="w-3.5 h-3.5" />تأخير الظهور (ثانية)</label>
          <input type="number" min={0} max={60} className="input" dir="ltr"
            value={rules.show_after_seconds ?? 0}
            onChange={e => onChange({ show_after_seconds: Number(e.target.value) })} />
        </div>
        {rules.trigger === 'scroll' && (
          <div className="flex-1">
            <label className="label">نسبة التمرير (%)</label>
            <input type="number" min={0} max={100} className="input" dir="ltr"
              value={rules.scroll_percent ?? 50}
              onChange={e => onChange({ scroll_percent: Number(e.target.value) })} />
          </div>
        )}
      </div>

      {/* Pages */}
      <div>
        <label className="label">صفحات العرض</label>
        <div className="flex flex-wrap gap-2 mt-1">
          {pages.map(p => (
            <button key={p} onClick={() => togglePage(p)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium border transition-colors ${
                current.includes(p) ? 'bg-brand-500 border-brand-500 text-white' : 'border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}>
              {pageLabels[p]}
            </button>
          ))}
        </div>
      </div>

      {/* Show once */}
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-slate-700">إظهار مرة واحدة للمستخدم</p>
          <p className="text-xs text-slate-400">يتذكر نحلة إذا رآه الزائر من قبل</p>
        </div>
        <button onClick={() => onChange({ show_once_per_user: !rules.show_once_per_user })}
          className={`relative w-11 h-6 rounded-full transition-colors ${rules.show_once_per_user ? 'bg-brand-500' : 'bg-slate-200'}`}>
          <span className={`absolute top-1 w-4 h-4 rounded-full bg-white shadow transition-all ${rules.show_once_per_user ? 'left-6' : 'left-1'}`} />
        </button>
      </div>
    </div>
  )
}

// ── Settings forms per widget type ────────────────────────────────────────────

function WhatsAppSettingsForm({
  settings, rules, onChange, onRulesChange,
}: {
  settings: Record<string, unknown>
  rules: DisplayRules
  onChange: (k: string, v: unknown) => void
  onRulesChange: (r: Partial<DisplayRules>) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <label className="label">رقم واتساب <span className="text-red-400">*</span></label>
        <input className="input" dir="ltr" placeholder="966555906901"
          value={String(settings.phone ?? '')}
          onChange={e => onChange('phone', e.target.value.replace(/\D/g, ''))} />
        <p className="text-xs text-slate-400 mt-1">الرقم الدولي بدون + أو مسافات</p>
      </div>
      <div>
        <label className="label">رسالة الترحيب</label>
        <input className="input" value={String(settings.message ?? '')}
          onChange={e => onChange('message', e.target.value)} />
      </div>
      <div>
        <label className="label">رابط الشعار (اختياري)</label>
        <input className="input" dir="ltr" placeholder="https://… — اتركه فارغاً لشعار نحلة 🐝"
          value={String(settings.logo_url ?? '')}
          onChange={e => onChange('logo_url', e.target.value)} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">موضع الزر</label>
          <div className="flex gap-2 mt-1">
            {(['left', 'right'] as const).map(p => (
              <button key={p} onClick={() => onChange('position', p)}
                className={`flex-1 py-2 text-sm rounded-lg border transition-colors ${
                  settings.position === p ? 'bg-brand-500 border-brand-500 text-white font-medium' : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                }`}>
                {p === 'right' ? 'يمين' : 'يسار'}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">لون الزر</label>
          <div className="flex gap-2 mt-1 items-center">
            <input type="color" className="w-10 h-10 rounded-lg border border-slate-200 cursor-pointer p-0.5"
              value={String(settings.theme_color ?? '#25D366')}
              onChange={e => onChange('theme_color', e.target.value)} />
            <input className="input flex-1" dir="ltr" value={String(settings.theme_color ?? '#25D366')}
              onChange={e => onChange('theme_color', e.target.value)} />
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-4">
        {[{ k: 'show_on_mobile', l: 'إظهار في الجوال' }, { k: 'show_on_desktop', l: 'إظهار في الحاسب' }].map(({ k, l }) => (
          <div key={k} className="flex items-center justify-between p-3 bg-slate-50 rounded-xl">
            <span className="text-sm text-slate-700">{l}</span>
            <button onClick={() => onChange(k, !settings[k])}
              className={`relative w-10 h-5.5 h-[22px] rounded-full transition-colors ${settings[k] !== false ? 'bg-brand-500' : 'bg-slate-200'}`}>
              <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${settings[k] !== false ? 'left-5' : 'left-0.5'}`} />
            </button>
          </div>
        ))}
      </div>
      <RulesEditor rules={rules} onChange={onRulesChange} />
    </div>
  )
}

function DiscountPopupSettingsForm({
  settings, rules, onChange, onRulesChange,
}: {
  settings: Record<string, unknown>
  rules: DisplayRules
  onChange: (k: string, v: unknown) => void
  onRulesChange: (r: Partial<DisplayRules>) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <label className="label">عنوان النافذة</label>
        <input className="input" value={String(settings.title ?? '')}
          onChange={e => onChange('title', e.target.value)} />
      </div>
      <div>
        <label className="label">وصف العرض</label>
        <input className="input" value={String(settings.description ?? '')}
          onChange={e => onChange('description', e.target.value)} />
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">نوع الخصم</label>
          <select className="input" value={String(settings.discount_type ?? 'percentage')}
            onChange={e => onChange('discount_type', e.target.value)}>
            <option value="percentage">نسبة مئوية (%)</option>
            <option value="fixed">مبلغ ثابت</option>
            <option value="text">نص حر</option>
          </select>
        </div>
        <div>
          <label className="label">قيمة الخصم</label>
          <input type="number" min={0} className="input" dir="ltr"
            value={Number(settings.discount_value ?? 10)}
            onChange={e => onChange('discount_value', Number(e.target.value))} />
        </div>
      </div>
      <div>
        <label className="label">نوع حقل الإدخال</label>
        <div className="flex gap-2 mt-1">
          {[
            { v: 'none',      l: 'بدون حقل' },
            { v: 'email',     l: 'بريد إلكتروني' },
            { v: 'whatsapp',  l: 'واتساب' },
          ].map(({ v, l }) => (
            <button key={v} onClick={() => onChange('input_type', v)}
              className={`flex-1 py-2 text-xs rounded-lg border transition-colors ${
                settings.input_type === v ? 'bg-brand-500 border-brand-500 text-white font-medium' : 'border-slate-200 text-slate-600 hover:bg-slate-50'
              }`}>
              {l}
            </button>
          ))}
        </div>
      </div>
      {!!settings.input_type && settings.input_type !== 'none' && (
        <div>
          <label className="label">نص placeholder الحقل</label>
          <input className="input" value={String(settings.input_placeholder ?? '')}
            onChange={e => onChange('input_placeholder', e.target.value)} />
        </div>
      )}
      {/* Coupon code */}
      <div className="p-4 bg-amber-50 border border-amber-200 rounded-xl space-y-2">
        <label className="label flex items-center gap-1.5">
          <Tag className="w-3.5 h-3.5 text-amber-600" />
          كود الكوبون (اختياري — يظهر مع زر نسخ)
        </label>
        <input className="input font-mono tracking-widest uppercase"
          dir="ltr" placeholder="مثال: HONEY10"
          value={String(settings.coupon_code ?? '')}
          onChange={e => onChange('coupon_code', e.target.value.toUpperCase())} />
        <p className="text-xs text-amber-700">
          أنشئ الكوبون في سلة أولاً ← الإعدادات ← الخصومات والكوبونات ← ثم ضع الكود هنا
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">نص الزر</label>
          <input className="input" value={String(settings.button_text ?? 'احصل على الخصم')}
            onChange={e => onChange('button_text', e.target.value)} />
        </div>
        <div>
          <label className="label">لون الزر</label>
          <div className="flex gap-2 items-center">
            <input type="color" className="w-10 h-10 rounded-lg border border-slate-200 cursor-pointer p-0.5"
              value={String(settings.button_color ?? '#6366F1')}
              onChange={e => onChange('button_color', e.target.value)} />
            <input className="input flex-1" dir="ltr" value={String(settings.button_color ?? '#6366F1')}
              onChange={e => onChange('button_color', e.target.value)} />
          </div>
        </div>
      </div>
      <RulesEditor rules={rules} onChange={onRulesChange} />
    </div>
  )
}

function SlideOfferSettingsForm({
  settings, rules, onChange, onRulesChange,
}: {
  settings: Record<string, unknown>
  rules: DisplayRules
  onChange: (k: string, v: unknown) => void
  onRulesChange: (r: Partial<DisplayRules>) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <label className="label">نص الشريط</label>
        <input className="input" value={String(settings.text ?? '')}
          onChange={e => onChange('text', e.target.value)} />
        <p className="text-xs text-slate-400 mt-1">مثال: احصل على خصم 10% 🏷️</p>
      </div>
      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="label">الموضع</label>
          <div className="flex gap-2 mt-1">
            {(['left', 'right'] as const).map(p => (
              <button key={p} onClick={() => onChange('position', p)}
                className={`flex-1 py-2 text-sm rounded-lg border transition-colors ${
                  settings.position === p ? 'bg-brand-500 border-brand-500 text-white font-medium' : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                }`}>
                {p === 'right' ? 'يمين' : 'يسار'}
              </button>
            ))}
          </div>
        </div>
        <div>
          <label className="label">لون الخلفية</label>
          <div className="flex gap-2 items-center">
            <input type="color" className="w-10 h-10 rounded-lg border border-slate-200 cursor-pointer p-0.5"
              value={String(settings.bg_color ?? '#6366F1')}
              onChange={e => onChange('bg_color', e.target.value)} />
            <input className="input flex-1" dir="ltr" value={String(settings.bg_color ?? '#6366F1')}
              onChange={e => onChange('bg_color', e.target.value)} />
          </div>
        </div>
      </div>
      <div className="flex items-center justify-between p-3 bg-slate-50 rounded-xl">
        <div>
          <p className="text-sm font-medium text-slate-700">فتح نافذة الخصم عند الضغط</p>
          <p className="text-xs text-slate-400">الشريط يفتح Discount Popup تلقائياً</p>
        </div>
        <button onClick={() => onChange('trigger_popup', !settings.trigger_popup)}
          className={`relative w-10 h-[22px] rounded-full transition-colors ${settings.trigger_popup !== false ? 'bg-brand-500' : 'bg-slate-200'}`}>
          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${settings.trigger_popup !== false ? 'left-5' : 'left-0.5'}`} />
        </button>
      </div>
      <RulesEditor rules={rules} onChange={onRulesChange} />
    </div>
  )
}

// ── Settings Modal ────────────────────────────────────────────────────────────

function SettingsModal({
  widget, onClose, onSave,
}: {
  widget: WidgetItem
  onClose: () => void
  onSave: (w: WidgetItem) => void
}) {
  const [settings, setSettings] = useState<Record<string, unknown>>({ ...widget.settings })
  const [rules,    setRules]    = useState<DisplayRules>({ ...widget.display_rules })
  const [saving,   setSaving]   = useState(false)
  const [saved,    setSaved]    = useState(false)
  const [error,    setError]    = useState('')

  const handleChange = useCallback((k: string, v: unknown) => {
    setSettings(prev => ({ ...prev, [k]: v }))
    setSaved(false)
  }, [])

  const handleRulesChange = useCallback((r: Partial<DisplayRules>) => {
    setRules(prev => ({ ...prev, ...r }))
    setSaved(false)
  }, [])

  const handleSave = async () => {
    setSaving(true); setError('')
    try {
      await widgetsApi.updateSettings(widget.key, settings)
      const updated = await widgetsApi.updateRules(widget.key, rules)
      setSaved(true)
      onSave(updated)
      setTimeout(() => setSaved(false), 2000)
    } catch {
      setError('حدث خطأ أثناء الحفظ — حاول مرة أخرى')
    } finally {
      setSaving(false)
    }
  }

  const formProps = { settings, rules, onChange: handleChange, onRulesChange: handleRulesChange }

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-lg bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-100 shrink-0">
          <div className="flex items-center gap-2.5">
            <div className={`p-2 rounded-xl ${CATEGORY_COLOR[widget.category] || 'bg-slate-100 text-slate-600'}`}>
              {ICON_MAP[widget.icon] || <LayoutGrid className="w-5 h-5" />}
            </div>
            <div>
              <h3 className="font-bold text-slate-900 text-sm">{widget.name}</h3>
              <p className="text-xs text-slate-400">إعدادات الويدجت</p>
            </div>
          </div>
          <button onClick={onClose} className="p-2 rounded-xl hover:bg-slate-100 transition-colors text-slate-400">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body (scrollable) */}
        <div className="flex-1 overflow-y-auto px-6 py-5">
          {widget.key === 'whatsapp_widget' && <WhatsAppSettingsForm  {...formProps} />}
          {widget.key === 'discount_popup'  && <DiscountPopupSettingsForm {...formProps} />}
          {widget.key === 'slide_offer'     && <SlideOfferSettingsForm    {...formProps} />}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-slate-100 flex items-center justify-between gap-3 shrink-0">
          {error && <p className="text-xs text-red-500 flex-1">{error}</p>}
          {saved && !error && (
            <p className="text-xs text-emerald-600 flex items-center gap-1 flex-1">
              <CheckCircle className="w-3.5 h-3.5" /> تم الحفظ
            </p>
          )}
          {!error && !saved && <div className="flex-1" />}
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-xl transition-colors">
              إغلاق
            </button>
            <button onClick={handleSave} disabled={saving}
              className="flex items-center gap-2 px-5 py-2 text-sm font-semibold text-white bg-brand-500 hover:bg-brand-600 rounded-xl transition-colors disabled:opacity-60">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />}
              {saving ? 'جاري الحفظ…' : 'حفظ'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Widget Card ───────────────────────────────────────────────────────────────

const BADGE_STYLES: Record<string, string> = {
  free:        'bg-emerald-100 text-emerald-700',
  paid:        'bg-amber-100 text-amber-700',
  coming_soon: 'bg-slate-100 text-slate-500',
}
const BADGE_LABELS: Record<string, string> = {
  free:        'مجاني',
  paid:        'مدفوع',
  coming_soon: 'قريباً',
}

function WidgetCard({
  widget, onToggle, onOpenSettings,
}: {
  widget: WidgetItem
  onToggle: (key: string, enabled: boolean) => void
  onOpenSettings: (widget: WidgetItem) => void
}) {
  const [toggling, setToggling] = useState(false)

  const handleToggle = async () => {
    if (toggling || widget.badge === 'coming_soon') return
    setToggling(true)
    try {
      await onToggle(widget.key, !widget.is_enabled)
    } finally {
      setToggling(false)
    }
  }

  const catStyle = CATEGORY_COLOR[widget.category] || 'text-slate-600 bg-slate-100'

  return (
    <div className={`relative bg-white rounded-2xl border p-5 flex flex-col gap-4 transition-all hover:shadow-md ${
      widget.is_enabled ? 'border-brand-200 shadow-sm shadow-brand-100' : 'border-slate-200'
    }`}>
      {/* Badge */}
      <span className={`absolute top-4 left-4 text-[10px] font-bold px-2 py-0.5 rounded-full ${BADGE_STYLES[widget.badge]}`}>
        {BADGE_LABELS[widget.badge]}
      </span>

      {/* Icon + name */}
      <div className="flex items-start gap-3 pt-2">
        <div className={`p-2.5 rounded-xl shrink-0 ${catStyle}`}>
          {ICON_MAP[widget.icon] || <LayoutGrid className="w-5 h-5" />}
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-bold text-slate-900 text-base">{widget.name}</h3>
          <p className="text-xs text-slate-500 mt-0.5 leading-relaxed line-clamp-2">{widget.description}</p>
          <span className={`inline-block mt-1.5 text-[10px] font-semibold px-2 py-0.5 rounded-full ${catStyle}`}>
            {CATEGORY_LABEL[widget.category]}
          </span>
        </div>
      </div>

      {/* Status */}
      <div className={`flex items-center gap-2 text-xs font-medium px-3 py-2 rounded-xl ${
        widget.is_enabled
          ? 'bg-emerald-50 text-emerald-700'
          : 'bg-slate-50 text-slate-500'
      }`}>
        <span className={`w-2 h-2 rounded-full ${widget.is_enabled ? 'bg-emerald-500 animate-pulse' : 'bg-slate-300'}`} />
        {widget.is_enabled ? 'مُفعَّل — يظهر في متجرك' : 'معطّل'}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2 pt-1">
        {widget.has_settings && (
          <button
            onClick={() => onOpenSettings(widget)}
            disabled={widget.badge === 'coming_soon'}
            className="flex items-center gap-1.5 px-3 py-2 text-xs font-medium text-slate-600 bg-slate-100 hover:bg-slate-200 rounded-xl transition-colors disabled:opacity-40">
            <Settings2 className="w-3.5 h-3.5" />
            إعدادات
          </button>
        )}
        <button
          onClick={handleToggle}
          disabled={toggling || widget.badge === 'coming_soon'}
          className={`flex-1 flex items-center justify-center gap-2 py-2 text-sm font-semibold rounded-xl transition-colors disabled:opacity-60 ${
            widget.is_enabled
              ? 'bg-red-50 text-red-600 hover:bg-red-100'
              : 'bg-brand-500 text-white hover:bg-brand-600'
          }`}>
          {toggling
            ? <Loader2 className="w-4 h-4 animate-spin" />
            : widget.is_enabled
              ? <><ToggleRight className="w-4 h-4" />تعطيل</>
              : <><ToggleLeft  className="w-4 h-4" />تفعيل</>
          }
        </button>
      </div>
    </div>
  )
}

// ── WhatsApp Floating Button Widget ──────────────────────────────────────────

const NAHLA_CDN_LOGO = 'https://cdn.salla.sa/XVEDq/b1ec4359-6895-49dc-80e7-06fd33b75df8-1000x666.66666666667-xMM28RbT68xVWoSgEtzBgpW1w4cDN7sEQAhQmLwD.jpg'

interface WaBubbleCfg {
  enabled: boolean
  phone: string
  message: string
  logo_url: string
  position: 'left' | 'right'
  scroll_threshold: number
}

function generateBubbleCode(cfg: WaBubbleCfg): string {
  const logo   = cfg.logo_url || NAHLA_CDN_LOGO
  const posX   = cfg.position === 'right' ? 'right:40px' : 'left:40px'
  const posXMo = cfg.position === 'right' ? 'right:20px' : 'left:20px'
  return `/* ====================================================
   Nahla WhatsApp Widget — نحلة
   انسخ هذا الكود كاملاً في حقل الجافاسكريبت بمتجرك
   ==================================================== */
(function(){
  var WA_NUMBER  = '${cfg.phone}';
  var WA_MESSAGE = '${cfg.message}';
  var LOGO = '${logo}';

  var btn = document.createElement('a');
  btn.href   = 'https://wa.me/' + WA_NUMBER + '?text=' + encodeURIComponent(WA_MESSAGE);
  btn.target = '_blank';
  btn.rel    = 'noopener noreferrer';
  btn.id     = 'nahla-whatsapp';
  btn.innerHTML =
    '<img src="' + LOGO + '" class="nahla-bee" alt="نحلة">' +
    '<div class="circle">' +
      '<span class="orbit o1"></span>' +
      '<span class="orbit o2"></span>' +
      '<span class="orbit o3"></span>' +
      '<span class="orbit o4"></span>' +
      '<img src="https://upload.wikimedia.org/wikipedia/commons/6/6b/WhatsApp.svg" class="icon" alt="واتساب">' +
    '</div>';
  document.body.appendChild(btn);

  function checkShow() {
    if (window.scrollY > ${cfg.scroll_threshold} || document.body.scrollHeight <= window.innerHeight + 300) {
      btn.classList.add('show');
    }
  }
  window.addEventListener('scroll', checkShow, { passive: true });
  checkShow();

  var s = document.createElement('style');
  s.innerHTML = [
    '#nahla-whatsapp{position:fixed;bottom:55px;${posX};z-index:9999;opacity:0;transform:scale(.8);transition:opacity .4s,transform .4s;display:flex;flex-direction:column;align-items:center;gap:6px;text-decoration:none;}',
    '#nahla-whatsapp.show{opacity:1;transform:scale(1);}',
    '.nahla-bee{width:110px;height:110px;object-fit:contain;animation:bee-float 3s ease-in-out infinite;}',
    '@keyframes bee-float{0%,100%{transform:translateY(0) rotate(-4deg);}50%{transform:translateY(-7px) rotate(4deg);}}',
    '.circle{position:relative;width:65px;height:65px;background:#25D366;border-radius:50%;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 18px rgba(37,211,102,.45);}',
    '.icon{width:30px;height:30px;z-index:2;position:relative;}',
    '.orbit{position:absolute;inset:0;border-radius:50%;border:2.5px solid rgba(37,211,102,.65);animation:apple-wave 2.8s cubic-bezier(.4,0,.2,1) infinite;}',
    '.o1{animation-delay:0s;}.o2{animation-delay:.7s;}.o3{animation-delay:1.4s;}.o4{animation-delay:2.1s;}',
    '@keyframes apple-wave{0%{transform:scale(.92);opacity:.85;}30%{transform:scale(1.25);opacity:.55;}60%{transform:scale(1.65);opacity:.22;}85%{transform:scale(1.95);opacity:.05;}100%{transform:scale(2.05);opacity:0;}}',
    '@media(max-width:600px){.circle{width:58px;height:58px;}.icon{width:26px;height:26px;}.nahla-bee{width:90px;height:90px;}#nahla-whatsapp{bottom:50px;${posXMo};}}'
  ].join('');
  document.head.appendChild(s);
})();`
}

function WhatsAppBubbleWidget() {
  const [cfg, setCfg] = useState<WaBubbleCfg>({
    enabled: false, phone: '', message: 'السلام عليكم، أبغى الاستفسار',
    logo_url: '', position: 'left', scroll_threshold: 250,
  })
  const [loading,  setLoading]  = useState(true)
  const [saving,   setSaving]   = useState(false)
  const [saved,    setSaved]    = useState(false)
  const [copied,   setCopied]   = useState(false)
  const [saveErr,  setSaveErr]  = useState<string | null>(null)
  const [open,     setOpen]     = useState(false)

  useEffect(() => {
    apiCall<WaBubbleCfg>('/settings/widget')
      .then(d => setCfg({ ...d, position: d.position === 'right' ? 'right' : 'left' }))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const patch = (partial: Partial<WaBubbleCfg>) => setCfg(prev => ({ ...prev, ...partial }))

  const handleSave = async () => {
    setSaving(true); setSaveErr(null)
    try {
      const saved_ = await apiCall<WaBubbleCfg>('/settings/widget', {
        method: 'PUT', body: JSON.stringify(cfg),
      })
      setCfg({ ...saved_, position: saved_.position === 'right' ? 'right' : 'left' })
      setSaved(true); setTimeout(() => setSaved(false), 3000)
    } catch { setSaveErr('فشل الحفظ — حاول مرة أخرى') }
    finally { setSaving(false) }
  }

  const code = generateBubbleCode(cfg)
  const copyCode = () => {
    navigator.clipboard.writeText(code)
    setCopied(true); setTimeout(() => setCopied(false), 2500)
  }

  return (
    <div className="rounded-2xl border border-emerald-200 bg-white overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between gap-3 px-5 py-4 hover:bg-slate-50 transition-colors"
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-emerald-50 flex items-center justify-center shrink-0">
            <MessageCircle className="w-5 h-5 text-emerald-600" />
          </div>
          <div className="text-start">
            <div className="flex items-center gap-2">
              <p className="text-sm font-bold text-slate-900">زر واتساب العائم</p>
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${
                cfg.enabled ? 'bg-emerald-100 text-emerald-700' : 'bg-slate-100 text-slate-500'
              }`}>
                {loading ? '...' : cfg.enabled ? 'مُفعّل' : 'معطّل'}
              </span>
            </div>
            <p className="text-xs text-slate-500 mt-0.5">
              زر واتساب متحرك يظهر في متجرك — يُحوّل الزوار لمحادثات مباشرة
            </p>
          </div>
        </div>
        <ChevronRight className={`w-4 h-4 text-slate-400 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`} />
      </button>

      {open && (
        <div className="border-t border-slate-100 p-5 space-y-5">

          {/* Enable toggle */}
          <div className="flex items-center justify-between py-2">
            <div>
              <p className="text-sm font-medium text-slate-800">تفعيل الويدجت</p>
              <p className="text-xs text-slate-400 mt-0.5">عند التفعيل يظهر زر واتساب في الزاوية السفلية للمتجر</p>
            </div>
            <button onClick={() => patch({ enabled: !cfg.enabled })}>
              {cfg.enabled
                ? <ToggleRight className="w-7 h-7 text-brand-500" />
                : <ToggleLeft  className="w-7 h-7 text-slate-300" />}
            </button>
          </div>

          {/* Config fields */}
          <div className="space-y-4">
            <div>
              <label className="label">رقم واتساب التاجر <span className="text-red-400">*</span></label>
              <input
                className="input" dir="ltr"
                placeholder="966555906901"
                value={cfg.phone}
                onChange={e => patch({ phone: e.target.value.replace(/\D/g, '') })}
              />
              <p className="text-xs text-slate-400 mt-1">الرقم الدولي بدون + ومسافات (مثال: 966555906901)</p>
            </div>
            <div>
              <label className="label">رسالة الترحيب الافتراضية</label>
              <input
                className="input"
                value={cfg.message}
                onChange={e => patch({ message: e.target.value })}
              />
            </div>
            <div>
              <label className="label">رابط الشعار (اختياري)</label>
              <input
                className="input" dir="ltr"
                placeholder="https://cdn.example.com/logo.png — اتركه فارغاً لشعار نحلة"
                value={cfg.logo_url}
                onChange={e => patch({ logo_url: e.target.value })}
              />
              <p className="text-xs text-slate-400 mt-1">اتركه فارغاً لاستخدام شعار نحلة الافتراضي 🐝</p>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="label">موضع الزر</label>
                <div className="flex gap-2 mt-1">
                  {(['right', 'left'] as const).map(p => (
                    <button
                      key={p}
                      onClick={() => patch({ position: p })}
                      className={`flex-1 py-2 text-sm rounded-lg border transition-colors ${
                        cfg.position === p
                          ? 'bg-brand-500 border-brand-500 text-white font-medium'
                          : 'border-slate-200 text-slate-600 hover:bg-slate-50'
                      }`}
                    >
                      {p === 'right' ? '⬅ يمين' : 'يسار ➡'}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="label">إظهار بعد تمرير (px)</label>
                <input
                  type="number" min={0} max={2000}
                  className="input" dir="ltr"
                  value={cfg.scroll_threshold}
                  onChange={e => patch({ scroll_threshold: Number(e.target.value) })}
                />
                <p className="text-xs text-slate-400 mt-1">0 = يظهر فوراً</p>
              </div>
            </div>
          </div>

          {/* Save button */}
          <div className="flex items-center gap-3">
            <button
              onClick={handleSave}
              disabled={saving || !cfg.phone}
              className="btn-primary disabled:opacity-50"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              حفظ الإعدادات
            </button>
            {saved   && <span className="text-sm text-emerald-600 flex items-center gap-1"><CheckCircle className="w-4 h-4" /> تم الحفظ</span>}
            {saveErr && <span className="text-sm text-red-500">{saveErr}</span>}
          </div>

          {/* Generated code */}
          <div className="space-y-3">
            <div>
              <p className="text-sm font-semibold text-slate-800 mb-1">كود التضمين</p>
              <p className="text-xs text-slate-400">انسخ الكود وضعه في حقل الجافاسكريبت المخصص في متجرك (سلة / زد / غيرها)</p>
            </div>
            {!cfg.phone ? (
              <div className="flex items-center gap-2 p-4 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-700">
                <AlertCircle className="w-4 h-4 shrink-0" />
                أدخل رقم واتساب أولاً لتوليد الكود
              </div>
            ) : (
              <div className="space-y-3">
                <div className="relative">
                  <textarea
                    readOnly dir="ltr" rows={10}
                    className="w-full font-mono text-xs bg-slate-900 text-slate-100 rounded-xl p-4 resize-none border-0 outline-none leading-relaxed"
                    value={code}
                  />
                  <button
                    onClick={copyCode}
                    className="absolute top-3 left-3 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-700 hover:bg-slate-600 text-white text-xs font-medium transition-colors"
                  >
                    {copied ? <CheckCircle className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                    {copied ? 'تم النسخ!' : 'نسخ الكود'}
                  </button>
                </div>
                <div className="flex items-start gap-2 p-3 bg-blue-50 border border-blue-200 rounded-xl text-xs text-blue-700">
                  <AlertCircle className="w-4 h-4 shrink-0 mt-0.5 text-blue-500" />
                  <div>
                    <p className="font-semibold mb-0.5">كيفية إضافة الكود في متجر سلة:</p>
                    <p>اذهب إلى <strong>المتجر ← الإعدادات ← سكريبت مخصص</strong> والصق الكود في حقل JavaScript</p>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function MerchantWidgets() {
  const [widgets,    setWidgets]    = useState<WidgetItem[]>([])
  const [loading,    setLoading]    = useState(true)
  const [error,      setError]      = useState('')
  const [activeModal, setActiveModal] = useState<WidgetItem | null>(null)
  const [showInstall, setShowInstall] = useState(false)

  const tenantId = getTenantId()

  const load = useCallback(async () => {
    try {
      const res = await widgetsApi.list()
      setWidgets(res.widgets)
    } catch {
      setError('تعذّر تحميل الويدجتات — تحقق من اتصالك بالإنترنت')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const handleToggle = async (key: string, enabled: boolean) => {
    const updated = await widgetsApi.toggle(key, enabled)
    setWidgets(prev => prev.map(w => w.key === key ? updated : w))
    // Open install panel when first widget is enabled
    if (enabled && !showInstall) setShowInstall(true)
  }

  const handleSave = (updated: WidgetItem) => {
    setWidgets(prev => prev.map(w => w.key === updated.key ? updated : w))
  }

  const enabledCount = widgets.filter(w => w.is_enabled).length

  // ── Loading ──────────────────────────────────────────────────────────────
  if (loading) return (
    <div className="flex items-center justify-center h-64">
      <div className="flex items-center gap-3 text-slate-500">
        <Loader2 className="w-6 h-6 animate-spin text-brand-500" />
        <span>جاري التحميل…</span>
      </div>
    </div>
  )

  if (error) return (
    <div className="flex items-center justify-center h-64">
      <div className="flex flex-col items-center gap-3 text-center">
        <AlertCircle className="w-10 h-10 text-red-400" />
        <p className="text-slate-600">{error}</p>
        <button onClick={load} className="px-4 py-2 text-sm text-brand-600 hover:text-brand-700 font-medium">
          إعادة المحاولة
        </button>
      </div>
    </div>
  )

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6 max-w-4xl mx-auto">

      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-slate-900 flex items-center gap-2">
            <TrendingUp className="w-6 h-6 text-brand-500" />
            أدوات زيادة المبيعات
          </h1>
          <p className="text-sm text-slate-500 mt-1">
            ويدجتات تحويلية تظهر مباشرة في متجرك — فعّلها من هنا وتحكم فيها بالكامل
          </p>
        </div>
        {enabledCount > 0 && (
          <div className="flex items-center gap-2 px-3 py-1.5 bg-emerald-50 border border-emerald-200 rounded-xl text-xs font-semibold text-emerald-700 shrink-0">
            <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" />
            {enabledCount} {enabledCount === 1 ? 'ويدجت نشط' : 'ويدجتات نشطة'}
          </div>
        )}
      </div>

      {/* Install Panel */}
      <QuickInstallPanel tenantId={tenantId} />

      {/* Info banner */}
      <div className="flex items-start gap-3 p-4 bg-blue-50 border border-blue-200 rounded-2xl">
        <Zap className="w-5 h-5 text-blue-500 shrink-0 mt-0.5" />
        <div className="text-sm text-blue-700 space-y-0.5">
          <p className="font-semibold">كيف يعمل النظام؟</p>
          <p className="text-xs text-blue-600 leading-relaxed">
            أضف رابط السكريبت مرة واحدة في متجرك — بعدها كل ما تفعّله أو تعطّله من هنا يظهر في متجرك
            خلال دقيقة تلقائياً بدون الرجوع لسلة مجدداً.
          </p>
        </div>
      </div>

      {/* Widget cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {widgets.map(widget => (
          <WidgetCard
            key={widget.key}
            widget={widget}
            onToggle={handleToggle}
            onOpenSettings={setActiveModal}
          />
        ))}
      </div>

      {/* WhatsApp Floating Button Widget */}
      <WhatsAppBubbleWidget />

      {/* Future widgets teaser */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {[
          { name: 'نافذة Exit Intent', desc: 'احتجز الزائر قبل مغادرته', icon: <MousePointerClick className="w-4 h-4" /> },
          { name: 'شريط الإعلانات', desc: 'شريط علوي لعروضك الخاصة', icon: <Bell className="w-4 h-4" /> },
          { name: 'مؤقت العد التنازلي', desc: 'أنشئ شعور الإلحاح بوقت محدود', icon: <Clock className="w-4 h-4" /> },
        ].map(item => (
          <div key={item.name}
            className="bg-slate-50 border border-dashed border-slate-200 rounded-2xl p-5 flex items-center gap-4 opacity-60">
            <div className="p-2.5 rounded-xl bg-slate-200 text-slate-500 shrink-0">{item.icon}</div>
            <div>
              <h3 className="font-semibold text-slate-600 text-sm">{item.name}</h3>
              <p className="text-xs text-slate-400">{item.desc}</p>
              <span className="inline-block mt-1 text-[10px] font-bold text-slate-500 bg-slate-200 px-2 py-0.5 rounded-full">قريباً</span>
            </div>
          </div>
        ))}
      </div>

      {/* Settings Modal */}
      {activeModal && (
        <SettingsModal
          widget={activeModal}
          onClose={() => setActiveModal(null)}
          onSave={updated => { handleSave(updated); setActiveModal(null) }}
        />
      )}
    </div>
  )
}
